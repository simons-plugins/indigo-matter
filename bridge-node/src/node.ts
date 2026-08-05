/**
 * The Matter side of the bridge: a ServerNode with an Aggregator at endpoint 1
 * and one bridged child endpoint per exported Indigo device.
 *
 * The child set is entirely protocol-driven (§3.1-§3.3) and lives in
 * {@link EndpointRegistry}; a node that has never been attached to serves an
 * empty aggregator. Endpoint *numbers* are persisted by matter.js against
 * `Endpoint.id`, so a device that comes back with the same id comes back with
 * the same number — presence in the running set is not what preserves identity.
 *
 * All matter.js coupling lives here, in `endpoints.ts`/`registry.ts` and in
 * main.ts — ADR-0006's binding constraint keeps it out of the Indigo plugin
 * entirely, and this module keeps it out of the protocol layer so the protocol
 * is testable on its own.
 */

import { Endpoint, Environment, ServerNode, VendorId, version as matterJsVersion } from "@matter/main";
import { BasicInformationServer } from "@matter/main/behaviors/basic-information";
import { AggregatorEndpoint } from "@matter/main/endpoints/aggregator";
import { Crypto } from "@matter/main";
import { DeviceCommissioner, FabricManager, PaseClient, PaseServer, SessionManager } from "@matter/main/protocol";
import {
    CommissioningFlowType,
    FabricIndex,
    ManualPairingCodeCodec,
    QrPairingCodeCodec,
} from "@matter/main/types";

import type { BridgeConfig } from "./config.js";
import { EndpointMapStore, type LiveEndpointNumber, refuseReasonFor } from "./endpoint-map.js";
import { uniqueIdFor } from "./endpoints.js";
import {
    type BridgeFacade,
    type CommandEventData,
    type CommissioningWindowResult,
    describeError,
    describeErrorWithStack,
    type DriftEntry,
    type EndpointSpec,
    ErrorCode,
    type FabricInfo,
    type PairingReport,
    ProtocolError,
    RefuseReason,
    type RemoveResult,
    type StatusReport,
    type UpsertResult,
    type WindowClosedReason,
} from "./protocol.js";
import { EndpointRegistry } from "./registry.js";
import {
    type BridgeIdentity,
    clearCommissioned,
    markCommissioned,
    nodeUniqueIdFor,
    serialNumberFor,
} from "./storage.js";
import { CommissioningWindow } from "./window.js";

/** Test vendor id. Uncertified by design — see ADR-0006 "Attestation cuts both ways". */
export const VENDOR_ID = 0xfff1;
export const PRODUCT_ID = 0x8000;
export const VENDOR_NAME = "simons-plugins";
export const PRODUCT_NAME = "Indigo Matter Bridge";

/** PRD §5.3: no hard cap, but past this many exports the log says so. */
export const ENDPOINT_COUNT_WARNING = 100;

/** PBKDF iteration count for enhanced-window verifiers. Spec floor is 1000. */
const PBKDF_ITERATIONS = 1000;
const PBKDF_SALT_BYTES = 32;

export { matterJsVersion };

export class BridgeNode implements BridgeFacade {
    #server?: ServerNode;
    #registry?: EndpointRegistry;
    #command?: (data: CommandEventData) => void;
    #driftListener?: (drift: DriftEntry[]) => void;
    readonly #window: CommissioningWindow;
    readonly #endpointMap: EndpointMapStore;
    /** The identity, rebound by {@link markCommissioned}/{@link clearCommissioned}. */
    #identity: BridgeIdentity;
    /** Why we are refusing everything but the §1.1 recovery trio, if we are. */
    #refusal: string | undefined;
    /** The drift the last {@link checkDrift} found; what §4.3 reports. */
    #drift: DriftEntry[] = [];

    /**
     * @param initialRefusal a refuse-to-start reason decided before the node was
     *   built — today only `main.ts`'s unusable-`identity.json` check, which has
     *   to happen before the identity is loaded (and therefore before this
     *   object can exist) because loading it is what would overwrite it.
     */
    constructor(
        private readonly config: BridgeConfig,
        identity: BridgeIdentity,
        private readonly bridgeVersion: string,
        private readonly log: (message: string) => void = console.log,
        initialRefusal?: string,
    ) {
        this.#identity = identity;
        this.#refusal = initialRefusal;
        this.#endpointMap = new EndpointMapStore(config.storagePath, message => this.log(message));
        this.#window = new CommissioningWindow({
            log: message => this.log(message),
            onExpire: () => this.endMatterCommissioning(),
        });
    }

    /** The identity in force — rebound whenever its commissioning witness moves. */
    private get identity(): BridgeIdentity {
        return this.#identity;
    }

    /** Matter wants an integer software version; derive it from the npm semver. */
    private get softwareVersion(): number {
        const [major = 0, minor = 0, patch = 0] = this.bridgeVersion.split(".").map(part => Number.parseInt(part, 10));
        return (major || 0) * 10000 + (minor || 0) * 100 + (patch || 0);
    }

    /**
     * Build the node and bring it online. The aggregator is added first so it
     * takes endpoint 1; bridged children land at 2 and up, in the order the
     * first `attach` creates them (and thereafter at whatever number matter.js
     * has persisted against their id).
     */
    async start(): Promise<void> {
        const environment = Environment.default;
        // matter.js resolves all of these lazily through its VariableService,
        // so setting them before `ServerNode.create` is what keeps storage out
        // of ~/.matter and pins mDNS when asked.
        environment.vars.set("storage.path", this.config.storagePath);
        if (this.config.mdnsInterface !== undefined) {
            environment.vars.set("mdns.networkInterface", this.config.mdnsInterface);
        }
        // We own SIGTERM/SIGINT outright. matter.js's ProcessManager installs its
        // own interrupt handlers when the runtime starts, and being registered
        // first they run first — tearing the ServerNode down concurrently with
        // main.ts's ordered shutdown. `runtime.signals` is the documented opt-out
        // (`ProcessManager.hasSignalSupport` reads it).
        environment.vars.set("runtime.signals", false);

        const server = await ServerNode.create({
            id: "indigo-matter-bridge",
            environment,
            network: { port: this.config.matterPort },
            commissioning: {
                passcode: this.identity.passcode,
                discriminator: this.identity.discriminator,
            },
            productDescription: {
                name: PRODUCT_NAME,
                deviceType: AggregatorEndpoint.deviceType,
            },
            basicInformation: {
                vendorName: VENDOR_NAME,
                vendorId: VendorId(VENDOR_ID),
                productName: PRODUCT_NAME,
                productId: PRODUCT_ID,
                serialNumber: serialNumberFor(this.identity),
                uniqueId: nodeUniqueIdFor(this.identity),
                hardwareVersion: 1,
                hardwareVersionString: "1",
                softwareVersion: this.softwareVersion,
                softwareVersionString: this.bridgeVersion,
            },
        });
        this.#server = server;

        const aggregator = new Endpoint(AggregatorEndpoint, { id: "aggregator" });
        await server.add(aggregator);

        this.#registry = new EndpointRegistry({
            aggregator,
            productName: PRODUCT_NAME,
            log: message => this.log(message),
            emit: data => this.#command?.(data),
            onConfigurationChange: () => this.bumpConfigurationVersion(),
        });

        server.events.commissioning.fabricsChanged.on((fabricIndex, action) => {
            // A throw here propagates straight into matter.js's observable, and
            // `fabrics()` reads server state that is legitimately gone once
            // close() is under way. Log-only, never rethrow.
            try {
                this.log(`Fabric ${action}: index ${fabricIndex} (${this.fabrics().length} total)`);
                this.noteCommissioningWitness();
            } catch (error) {
                try {
                    this.log(`Fabric ${action}: index ${fabricIndex} (count unavailable: ${describeError(error)})`);
                } catch {
                    // The logger itself failed; there is nowhere left to report.
                }
            }
        });

        // Before the stack goes online, so the answer is available the moment a
        // client can ask — and so the log reads in the order things happened.
        this.#endpointMap.load();

        await server.start();

        this.assertEndpointIdentity();

        this.log(`Matter node online on port ${this.config.matterPort}, storage ${this.config.storagePath}`);
        this.logPairing();
    }

    /**
     * The PRD §7 refuse-to-start decision, taken once the stack is up and the
     * fabric table is readable.
     *
     * Three conditions, and the asymmetry between them is the whole design.
     * Endpoint numbers only matter to somebody who is *paired*: on a bridge
     * nothing has ever commissioned, a missing or unreadable map costs nothing,
     * so a fresh install must never be blocked by one. Once a fabric exists,
     * both directions of the loss are fatal to identity and neither may be
     * papered over —
     *
     * * fabrics but no usable map: the numbers being served cannot be checked
     *   against anything, so a silent re-record would bless whatever matter.js
     *   happens to be handing out this boot;
     * * the commissioning witness but no fabrics (PRD §7's "storage missing but
     *   previously commissioned"): matter.js's storage has gone, which means its
     *   own `Endpoint.id → number` allocation went with it, and every accessory
     *   is about to be re-created in every ecosystem the user paired.
     *
     * Refusing is *not* a process exit. The Matter stack stays up so `get_pairing`
     * still answers and the user can see the state, but `attach` is refused, so
     * no endpoint is ever created — which is what actually prevents the
     * duplication. §3.11 is the way out, and it is explicit because it is a
     * decision only the user can take.
     */
    private assertEndpointIdentity(): void {
        if (this.#refusal !== undefined) {
            this.refuse(this.#refusal);
            return;
        }
        const reason = refuseReasonFor({
            commissioned: this.server.lifecycle.isCommissioned,
            mapPresent: this.#endpointMap.present,
            mapProblem: this.#endpointMap.problem,
            commissionedAt: this.identity.commissionedAt,
        });
        if (reason === undefined) {
            this.noteCommissioningWitness();
            return;
        }
        if (reason === RefuseReason.fabricStorageLost) {
            this.log(
                `Identity records commissioning at ${this.identity.commissionedAt}, but the ` +
                    "Matter stack reports no fabrics",
            );
        }
        this.refuse(reason);
    }

    private refuse(reason: string): void {
        this.#refusal = reason;
        this.log(
            `REFUSING to serve endpoints — ${reason}. Nothing will be exported until the endpoint ` +
                "map is rebuilt (BRIDGE_PROTOCOL §3.11), which WILL duplicate accessories in " +
                "already-paired ecosystems.",
        );
    }

    /** §1.1 — the reason we are refusing, or `undefined` while serving. */
    endpointMapRefusal(): string | undefined {
        return this.#refusal;
    }

    /**
     * Stamp `commissionedAt` the first time a fabric is seen.
     *
     * Written on the *observation* rather than on the commissioning command,
     * because the thing being witnessed is "endpoint numbers now matter to
     * somebody", and that is true of a fabric restored from a backup exactly as
     * it is of one just paired.
     */
    private noteCommissioningWitness(): void {
        if (this.identity.commissionedAt !== undefined || this.fabrics().length === 0) {
            return;
        }
        this.#identity = markCommissioned(this.config.storagePath, this.identity, message =>
            this.log(message),
        );
    }

    /** Print the pairing codes the way an operator (and the plugin log) needs them. */
    logPairing(): void {
        const pairing = this.getPairing();
        if (pairing.commissioned && !pairing.windowOpen) {
            this.log(`Commissioned into ${pairing.fabrics.length} fabric(s); no commissioning window open`);
            return;
        }
        this.log(`Manual pairing code: ${pairing.manualPairingCode}`);
        this.log(`QR pairing code: ${pairing.qrPairingCode}`);
    }

    get server(): ServerNode {
        if (this.#server === undefined) {
            throw new ProtocolError(ErrorCode.internal, "Matter node not started");
        }
        return this.#server;
    }

    private fabrics(): FabricInfo[] {
        const fabrics = this.server.state.commissioning.fabrics;
        return Object.values(fabrics).map(fabric => ({
            fabricIndex: Number(fabric.fabricIndex),
            label: fabric.label,
            vendorId: Number(fabric.rootVendorId),
        }));
    }

    private get registry(): EndpointRegistry {
        if (this.#registry === undefined) {
            throw new ProtocolError(ErrorCode.internal, "Matter node not started");
        }
        return this.#registry;
    }

    getStatus(): StatusReport {
        const endpoints = this.#registry?.summaries() ?? [];
        return {
            commissioned: this.server.lifecycle.isCommissioned,
            fabrics: this.fabrics(),
            endpointCount: endpoints.length,
            endpoints,
            // Whatever the last check found. NOT recomputed here: `get_status`
            // is the plugin's watchdog tick, and a detector that ran per tick
            // would re-emit `drift_detected` every 15s for one unchanged fault.
            drift: [...this.#drift],
            driftChecked: this.#endpointMap.checked,
        };
    }

    /**
     * Compare the live endpoint numbers against the persisted map (PRD §4.3).
     *
     * Run at the end of every operation that can create an endpoint, which is
     * where a number can first be wrong. Report-only by contract: a mismatch
     * populates `StatusReport.drift`, emits §5 `drift_detected` and changes
     * nothing — the map keeps its baseline so the fault keeps being reported
     * rather than being blessed into the truth on the next pass.
     */
    private checkDrift(): void {
        const live: LiveEndpointNumber[] = (this.#registry?.summaries() ?? []).map(summary => ({
            uniqueId: uniqueIdFor(summary.indigoDeviceId),
            endpointNumber: summary.endpointNumber,
        }));
        const drift = this.#endpointMap.check(live);
        this.#drift = drift;
        if (drift.length === 0) {
            return;
        }
        this.log(
            `ENDPOINT-NUMBER DRIFT: ${drift
                .map(entry => `${entry.uniqueId} expected ${entry.expected}, got ${entry.actual}`)
                .join("; ")}. Exported accessories may have swapped identities in paired ` +
                "ecosystems; this is never repaired automatically.",
        );
        this.#driftListener?.(drift);
    }

    /** §3.1 — reconcile the live endpoint set, then answer with the new status. */
    async reconcile(endpoints: readonly EndpointSpec[], replaceAll: boolean): Promise<StatusReport> {
        try {
            await this.registry.reconcile(endpoints, replaceAll);
        } finally {
            // In a `finally` because a part-applied reconcile (registry.ts is
            // explicit that there is no transaction across several `add`s) has
            // still created endpoints, and those are exactly the numbers worth
            // checking. Skipping the check on the failure path would leave the
            // one case where drift is most likely un-looked-at.
            this.checkDrift();
        }
        if (this.registry.size > ENDPOINT_COUNT_WARNING) {
            this.log(
                `${this.registry.size} exported endpoints exceeds the ${ENDPOINT_COUNT_WARNING} advisory limit; ` +
                    "ecosystem per-home accessory caps will bite before memory does",
            );
        }
        return this.getStatus();
    }

    /** §3.2 */
    async upsertEndpoint(spec: EndpointSpec): Promise<UpsertResult> {
        try {
            return await this.registry.upsert(spec);
        } finally {
            this.checkDrift();
        }
    }

    /** §3.3 */
    async removeEndpoint(indigoDeviceId: number): Promise<RemoveResult> {
        return this.registry.remove(indigoDeviceId);
    }

    /** §3.4 */
    async setState(indigoDeviceId: number, states: Record<string, unknown>): Promise<void> {
        await this.registry.setState(indigoDeviceId, states);
    }

    /** §3.5 */
    async setReachable(indigoDeviceId: number, reachable: boolean): Promise<void> {
        await this.registry.setReachable(indigoDeviceId, reachable);
    }

    /**
     * PRD §5.3 / Matter 1.5: a changed bridged-node set is a configuration
     * change of the bridge. Bumping the root's `ConfigurationVersion` also
     * covers the children — matter.js's own
     * `BridgedDeviceBasicInformationServer.increaseConfigurationVersion`
     * increments the root as well, so doing it once per batch is both cheaper
     * and truer to "one logical change, one increment".
     */
    private async bumpConfigurationVersion(): Promise<void> {
        const server = this.#server;
        if (server === undefined) {
            return;
        }
        await server.act(agent => agent.get(BasicInformationServer).increaseConfigurationVersion());
    }

    /** §5: the sink for `window_closed`, wired up by the protocol server. */
    onWindowClosed(listener: (reason: WindowClosedReason) => void): void {
        this.#window.onClosed(listener);
    }

    /** §5: the sink for `command`. One listener, last registration wins. */
    onCommand(listener: (data: CommandEventData) => void): void {
        this.#command = listener;
    }

    /** §5: the sink for `drift_detected`. */
    onDriftDetected(listener: (drift: DriftEntry[]) => void): void {
        this.#driftListener = listener;
    }

    /**
     * §3.9 — drop one ecosystem's fabric.
     *
     * Driven through `FabricManager` rather than the `OperationalCredentials`
     * cluster command for the same reason §3.8 avoids `AdministratorCommissioning`:
     * `OperationalCredentialsServer.removeFabric` opens with `assertRemoteActor`,
     * so it cannot be invoked from an offline agent at all.
     *
     * An index with no fabric behind it succeeds. The plugin picks an index out
     * of a `get_pairing` readout that is by definition a moment old, and the way
     * it goes stale is the ecosystem unpairing itself — so "it is already gone"
     * is the request being granted, not refused. Said out loud rather than
     * silently, because the other way to get here is a typo.
     */
    async removeFabric(fabricIndex: number): Promise<void> {
        const fabrics = this.server.env.get(FabricManager);
        const fabric = fabrics.maybeFor(FabricIndex(fabricIndex));
        if (fabric === undefined) {
            this.log(`No fabric at index ${fabricIndex}; nothing to remove`);
            return;
        }
        // `leave` rather than `delete`: it flushes subscriptions and emits the
        // leave event first, which is how a controller learns it was removed
        // instead of simply losing the node.
        await fabric.leave();
        this.log(`Removed fabric ${fabricIndex} (${this.fabrics().length} remaining)`);
    }

    /**
     * §3.10 — wipe commissioning credentials and start advertising fresh.
     *
     * `ServerNode.erase()` is matter.js 0.17.8's factory reset: it takes the node
     * offline, clears sessions, fabrics, events and the node's storage, then
     * brings it back up if it was up — so re-advertising is not something we
     * have to arrange.
     *
     * The endpoint map is **preserved by default**, which is the whole reason it
     * lives outside the storage context `erase()` wipes: a user who resets to
     * re-pair the same ecosystems should not have their accessory identities
     * scrambled as a side effect. `preserveEndpointNumbers: false` is the
     * explicit "the map itself is what is corrupt" path (PRD §7).
     *
     * The commissioning witness is cleared either way. Leaving it set would make
     * the very next start refuse: witness says paired, `erase()` says no
     * fabrics, which is exactly the fabric-storage-lost signature — and it would
     * be the reset doing it to itself.
     *
     * The live endpoints are deliberately left alone. They are still the set the
     * plugin asked for; what changed is who is allowed to see them.
     */
    async factoryReset(preserveEndpointNumbers: boolean): Promise<void> {
        this.#window.clear();
        this.log(
            `Factory reset: wiping commissioning credentials (endpoint numbers ` +
                `${preserveEndpointNumbers ? "preserved" : "DISCARDED"})`,
        );
        await this.server.erase();
        this.#identity = clearCommissioned(this.config.storagePath, this.identity, message =>
            this.log(message),
        );
        if (!preserveEndpointNumbers) {
            this.#endpointMap.discard();
        }
        this.#drift = [];
        // A reset node is un-commissioned, so nothing it could have refused for
        // is still true — and refusing after the user has just accepted the
        // reset would be an outage with no remaining cause.
        this.#refusal = undefined;
        this.log("Factory reset complete; advertising for commissioning again");
        this.logPairing();
    }

    /**
     * §3.11 — adopt the live endpoint numbers as the new persisted map.
     *
     * The way out of the refuse-to-start state, and the only operation that
     * discards a baseline. It renumbers nothing: by the time a user is asked to
     * confirm this, whatever duplication a lost map implies has already happened
     * in the ecosystems. What the rebuild changes is that the node stops
     * refusing and starts telling the truth about the numbers that now exist.
     *
     * When the refusal was `fabricStorageLost` the witness is cleared too —
     * confirming the rebuild is the user accepting that those pairings are gone,
     * and leaving the witness set would refuse again on the next start for a
     * loss they have already acknowledged.
     */
    async rebuildEndpointMap(): Promise<StatusReport> {
        const live: LiveEndpointNumber[] = (this.#registry?.summaries() ?? []).map(summary => ({
            uniqueId: uniqueIdFor(summary.indigoDeviceId),
            endpointNumber: summary.endpointNumber,
        }));
        this.#endpointMap.rebuild(live);
        this.#drift = [];
        this.#refusal = undefined;
        if (!this.server.lifecycle.isCommissioned) {
            this.#identity = clearCommissioned(this.config.storagePath, this.identity, message =>
                this.log(message),
            );
        }
        this.log(`Endpoint map rebuilt from ${live.length} live endpoint(s); serving normally again`);
        return this.getStatus();
    }

    getPairing(): PairingReport {
        const commissioned = this.server.lifecycle.isCommissioned;
        const window = this.#window.current;

        if (window !== undefined) {
            return {
                commissioned,
                windowOpen: true,
                windowExpiresAt: window.expiresAt.toISOString(),
                manualPairingCode: window.manualPairingCode,
                qrPairingCode: window.qrPairingCode,
                fabrics: this.fabrics(),
            };
        }

        if (!commissioned) {
            // The never-commissioned initial state: the basic window is open and
            // its codes are the persisted originals (§3.7).
            const codes = this.server.state.commissioning.pairingCodes;
            return {
                commissioned: false,
                windowOpen: true,
                windowExpiresAt: null,
                manualPairingCode: codes.manualPairingCode,
                qrPairingCode: codes.qrPairingCode,
                fabrics: this.fabrics(),
            };
        }

        return {
            commissioned: true,
            windowOpen: false,
            windowExpiresAt: null,
            manualPairingCode: null,
            qrPairingCode: null,
            fabrics: this.fabrics(),
        };
    }

    /**
     * Open an enhanced commissioning window so a further ecosystem can pair.
     * The passcode is ephemeral: matter.js holds only the PAKE verifier, so we
     * derive and keep the display codes ourselves for the window's lifetime.
     *
     * This drives `DeviceCommissioner` rather than the `AdministratorCommissioning`
     * cluster command, because in matter.js 0.17.8 that command asserts a remote
     * authenticated session (it records the requesting fabric in
     * `adminFabricIndex`) and therefore cannot be invoked from an offline agent.
     * The consequence is that the cluster's `windowStatus`/`adminFabricIndex`
     * attributes do not reflect a locally-opened window — a conformance gap for
     * E7 to close, not something the pairing flow depends on.
     */
    async openCommissioningWindow(durationSeconds: number): Promise<CommissioningWindowResult> {
        // Before anything Matter-side: `allowEnhancedCommissioning` swaps the PASE
        // commissioner and only then throws on a double-open, which would kill the
        // code the user is already holding. See CommissioningWindow.assertClosed.
        this.#window.assertClosed();

        const server = this.server;

        let discriminator: number;
        let passcode: number;
        try {
            const crypto = server.env.get(Crypto);
            passcode = PaseClient.generateRandomPasscode(crypto);
            discriminator = PaseClient.generateRandomDiscriminator(crypto);
            const salt = crypto.randomBytes(PBKDF_SALT_BYTES);
            const verifier = await PaseClient.generatePakePasscodeVerifier(crypto, passcode, {
                iterations: PBKDF_ITERATIONS,
                salt,
            });
            const paseServer = PaseServer.fromVerificationValue(server.env.get(SessionManager), verifier, {
                iterations: PBKDF_ITERATIONS,
                salt,
            });
            await server.env.get(DeviceCommissioner).allowEnhancedCommissioning(discriminator, paseServer, () => {
                this.#window.noteEnded();
            });
        } catch (error) {
            this.log(`Failed to open commissioning window: ${describeErrorWithStack(error)}`);
            throw new ProtocolError(ErrorCode.commissioningWindowFailed, describeError(error));
        }

        const manualPairingCode = ManualPairingCodeCodec.encode({ discriminator, passcode });
        const qrPairingCode = QrPairingCodeCodec.encode([
            {
                version: 0,
                vendorId: VENDOR_ID,
                productId: PRODUCT_ID,
                flowType: CommissioningFlowType.Standard,
                discriminator,
                passcode,
                discoveryCapabilities: 0b100, // on IP network
            },
        ]);

        // DeviceCommissioner *does* build a STANDARD_COMMISSIONING_TIMEOUT timer
        // in `#enterCommissioningMode`, but never calls `.start()` on it — a
        // matter.js 0.17.8 bug — so nothing on the Matter side would ever close
        // this window. Ours is the only timer. If upstream fixes that, the two
        // race, which is why we cap durationSeconds at Matter's own 900s maximum:
        // whichever fires first, the outcome is the same.
        const expiresAt = this.#window.open(durationSeconds, manualPairingCode, qrPairingCode);
        return { manualPairingCode, qrPairingCode, windowExpiresAt: expiresAt.toISOString() };
    }

    /** End the Matter-side window after ours expired. Never throws. */
    private endMatterCommissioning(): void {
        const server = this.#server;
        if (server === undefined) {
            return;
        }
        void server.env
            .get(DeviceCommissioner)
            .endCommissioning()
            .catch((error: unknown) => this.log(`Failed to end commissioning: ${describeError(error)}`));
    }

    async close(): Promise<void> {
        this.#window.clear();
        const server = this.#server;
        this.#server = undefined;
        this.#registry?.close();
        this.#registry = undefined;
        if (server !== undefined) {
            await server.close();
        }
    }
}
