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
import { DeviceCommissioner, PaseClient, PaseServer, SessionManager } from "@matter/main/protocol";
import { CommissioningFlowType, ManualPairingCodeCodec, QrPairingCodeCodec } from "@matter/main/types";

import type { BridgeConfig } from "./config.js";
import {
    type BridgeFacade,
    type CommandEventData,
    type CommissioningWindowResult,
    describeError,
    describeErrorWithStack,
    type EndpointSpec,
    ErrorCode,
    type FabricInfo,
    type PairingReport,
    ProtocolError,
    type RemoveResult,
    type StatusReport,
    type UpsertResult,
    type WindowClosedReason,
} from "./protocol.js";
import { EndpointRegistry } from "./registry.js";
import { type BridgeIdentity, nodeUniqueIdFor, serialNumberFor } from "./storage.js";
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
    readonly #window: CommissioningWindow;

    constructor(
        private readonly config: BridgeConfig,
        private readonly identity: BridgeIdentity,
        private readonly bridgeVersion: string,
        private readonly log: (message: string) => void = console.log,
    ) {
        this.#window = new CommissioningWindow({
            log: message => this.log(message),
            onExpire: () => this.endMatterCommissioning(),
        });
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
            } catch (error) {
                try {
                    this.log(`Fabric ${action}: index ${fabricIndex} (count unavailable: ${describeError(error)})`);
                } catch {
                    // The logger itself failed; there is nowhere left to report.
                }
            }
        });

        await server.start();

        this.log(`Matter node online on port ${this.config.matterPort}, storage ${this.config.storagePath}`);
        this.logPairing();
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
            // E5 introduces the persisted endpoint-number map and its drift
            // detector; until then there is no baseline to drift from, which is
            // what `driftChecked: false` says out loud (§4.3). An empty `drift`
            // here means "not looked", never "looked and found nothing".
            drift: [],
            driftChecked: false,
        };
    }

    /** §3.1 — reconcile the live endpoint set, then answer with the new status. */
    async reconcile(endpoints: readonly EndpointSpec[], replaceAll: boolean): Promise<StatusReport> {
        await this.registry.reconcile(endpoints, replaceAll);
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
        return this.registry.upsert(spec);
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
