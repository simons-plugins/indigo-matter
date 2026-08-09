/**
 * The Matter side of the bridge: a ServerNode with an Aggregator at endpoint 1
 * and one bridged child endpoint per exported Indigo device.
 *
 * The child set is owned by {@link EndpointRegistry} and is authoritative from
 * the plugin (§3.1-§3.3) — but it is **rebuilt from `endpoint-map.json` before
 * the stack goes online** (issue #141), so only a bridge that has never
 * exported anything serves an empty aggregator. A node that came online empty
 * and waited for the plugin told every paired ecosystem that every accessory
 * had gone, and Apple re-created them as new ones in its own room.
 *
 * Endpoint *numbers* are persisted by matter.js against `Endpoint.id`, so a
 * device that comes back with the same id comes back with the same number —
 * presence in the running set is not what preserves identity, and the restore
 * therefore renumbers nothing.
 *
 * All matter.js coupling lives here, in `endpoints.ts`/`registry.ts` and in
 * main.ts — ADR-0006's binding constraint keeps it out of the Indigo plugin
 * entirely, and this module keeps it out of the protocol layer so the protocol
 * is testable on its own.
 */

import { join } from "node:path";

import {
    Endpoint,
    Environment,
    ServerNode,
    UninitializedDependencyError,
    VendorId,
    version as matterJsVersion,
} from "@matter/main";
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
import { ENDPOINT_MAP_FILE, EndpointMapStore, type LiveEndpointNumber, refuseReasonFor } from "./endpoint-map.js";
import { type BridgedIdentity, indigoDeviceIdFrom, isSupportedRole, uniqueIdFor } from "./endpoints.js";
import {
    type BridgeFacade,
    type CommandEventData,
    type CommissioningWindowResult,
    describeError,
    describeErrorWithStack,
    type DriftEntry,
    endpointMapInvalidDetails,
    type EndpointSpec,
    ErrorCode,
    type FabricInfo,
    isRole,
    type PairingReport,
    ProtocolError,
    RefuseReason,
    type RemoveFabricResult,
    type RemoveResult,
    type StatusReport,
    type UpsertResult,
    type WindowClosedReason,
} from "./protocol.js";
import { EndpointRegistry } from "./registry.js";
import {
    type BridgeIdentity,
    clearCommissioned,
    identityFileProblem,
    markCommissioned,
    nodeUniqueIdFor,
    readIdentity,
    serialNumberFor,
    type WitnessWrite,
} from "./storage.js";
import { CommissioningWindow } from "./window.js";

/** Test vendor id. Uncertified by design — see ADR-0006 "Attestation cuts both ways". */
export const VENDOR_ID = 0xfff1;
export const PRODUCT_ID = 0x8000;
export const VENDOR_NAME = "simons-plugins";
export const PRODUCT_NAME = "Indigo Matter Bridge";

/** A bridge has no hardware. Constant rather than invented, but present rather
 * than absent: ecosystems render the attribute, and an absent one reads blank. */
export const HARDWARE_VERSION = 1;
export const HARDWARE_VERSION_STRING = "1";

/** PRD §5.3: no hard cap, but past this many exports the log says so. */
export const ENDPOINT_COUNT_WARNING = 100;

/** §4.3 `warnings` key for identity-file writes. Stable, so it replaces itself. */
const WARN_IDENTITY_WRITE = "identity-write";

/** PBKDF iteration count for enhanced-window verifiers. Spec floor is 1000. */
const PBKDF_ITERATIONS = 1000;
const PBKDF_SALT_BYTES = 32;

/**
 * §133 — how long {@link settlePairingRead} keeps retrying. The observed
 * transient window (`noteLastFabricGone` re-initialising matter.js after the
 * last fabric leaves) was ~200ms live on 2026-08-06; 3s is ~15x headroom for
 * a slower box. The number that actually pins it at 3s and not higher: it
 * must stay comfortably under `bridge_client.py`'s 10s `DEFAULT_TIMEOUT` for
 * `get_pairing`, so a genuinely broken node fails on OUR deadline and still
 * says so, rather than the plugin's socket read timing out first and hiding
 * which error was real.
 */
const PAIRING_SETTLE_DEADLINE_MS = 3000;
/** ~50-100ms per retry: fine-grained enough to catch the ~200ms window
 * within a couple of attempts, coarse enough not to spin. */
const PAIRING_SETTLE_INTERVAL_MS = 75;

/**
 * matter.js 0.17.8 throws this (`Construction#assert` →
 * `Lifecycle.assertActive`, in `@matter/general/util/Lifecycle`, re-exported
 * by `@matter/main`'s `export * from "@matter/general"`) for a behavior read
 * against a subject whose status is `Inactive` — exactly the state
 * `getPairing()`'s `this.server.state...`/`this.fabrics()` reads land in
 * during the re-init window. The class is reachable via a normal import, so
 * `instanceof` is used directly; there is no message-text fallback to keep
 * in sync.
 */
function isTransientUninitialized(error: unknown): boolean {
    return error instanceof UninitializedDependencyError;
}

/** Real timer, unref'd: a pending retry must never keep the process alive
 * past shutdown (same discipline as `window.ts`'s `defaultScheduler`). */
function defaultPairingSettleSleep(ms: number): Promise<void> {
    return new Promise(resolve => {
        const timer = setTimeout(resolve, ms);
        timer.unref?.();
    });
}

/**
 * §133 — bounded retry around a synchronous `read` (in practice
 * {@link BridgeNode.getPairing}) that may throw matter.js's transient
 * "not initialized" error while `noteLastFabricGone` is re-initialising the
 * stack after the last fabric left. Unpairing the last fabric already
 * succeeded by the time this races; without the retry the plugin's very next
 * `get_pairing` read — confirming that success — logs a scary error instead.
 *
 * Retries only on {@link isTransientUninitialized}; anything else is
 * rethrown immediately, no retry, no delay — a different failure is not this
 * one. Past {@link PAIRING_SETTLE_DEADLINE_MS} the LAST error is rethrown
 * unchanged (not wrapped): a node that is genuinely broken must still say so.
 *
 * `read`/`sleep`/`now`/the deadline and interval are all overridable so the
 * retry/backoff/deadline behaviour is unit-testable with no live Matter
 * stack — the same reason `CommissioningWindow` (`window.ts`) injects its
 * scheduler.
 */
export async function settlePairingRead(
    read: () => PairingReport,
    options: {
        deadlineMs?: number;
        intervalMs?: number;
        sleep?: (ms: number) => Promise<void>;
        now?: () => number;
    } = {},
): Promise<PairingReport> {
    const deadlineMs = options.deadlineMs ?? PAIRING_SETTLE_DEADLINE_MS;
    const intervalMs = options.intervalMs ?? PAIRING_SETTLE_INTERVAL_MS;
    const sleep = options.sleep ?? defaultPairingSettleSleep;
    const now = options.now ?? Date.now;
    const deadline = now() + deadlineMs;
    for (;;) {
        try {
            return read();
        } catch (error) {
            if (!isTransientUninitialized(error) || now() >= deadline) {
                throw error;
            }
            await sleep(intervalMs);
        }
    }
}

export { matterJsVersion };

export class BridgeNode implements BridgeFacade {
    #server?: ServerNode;
    #registry?: EndpointRegistry;
    #command?: (data: CommandEventData) => void;
    #driftListener?: (drift: DriftEntry[]) => void;
    #fabricsListener?: (fabrics: FabricInfo[], change: string) => void;
    #commissionedListener?: () => void;
    #decommissionedListener?: () => void;
    /** Last observed fabric count — what turns a change into a §5 transition. */
    #fabricCount = 0;
    readonly #window: CommissioningWindow;
    readonly #endpointMap: EndpointMapStore;
    /** The identity, rebound by {@link markCommissioned}/{@link clearCommissioned}. */
    #identity: BridgeIdentity;
    /** Why we are refusing everything but the §1.1 recovery trio, if we are. */
    #refusal: string | undefined;
    /** The drift the last {@link checkDrift} found; what §4.3 reports. */
    #drift: DriftEntry[] = [];
    /**
     * §4.3 `warnings` this object owns — the identity-file ones. The endpoint
     * map keeps its own and {@link getStatus} merges them, so each warning is
     * cleared by whatever succeeded rather than by a central bookkeeper that
     * would have to know when that happened.
     */
    readonly #warnings = new Map<string, string>();

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
     * The identity every bridged child publishes on its Bridged Device Basic
     * Information. Derived here, from the same constants and the same
     * `softwareVersion` the root node uses, so the two can never drift.
     */
    private get bridgedIdentity(): BridgedIdentity {
        return {
            vendorName: VENDOR_NAME,
            vendorId: VENDOR_ID,
            productName: PRODUCT_NAME,
            hardwareVersion: HARDWARE_VERSION,
            hardwareVersionString: HARDWARE_VERSION_STRING,
            softwareVersion: this.softwareVersion,
            softwareVersionString: this.bridgeVersion,
        };
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
                hardwareVersion: HARDWARE_VERSION,
                hardwareVersionString: HARDWARE_VERSION_STRING,
                softwareVersion: this.softwareVersion,
                softwareVersionString: this.bridgeVersion,
            },
        });
        this.#server = server;

        const aggregator = new Endpoint(AggregatorEndpoint, { id: "aggregator" });
        await server.add(aggregator);

        this.#registry = new EndpointRegistry({
            aggregator,
            // Deliberately the SAME values the root node's BasicInformation
            // carries above: an ecosystem that shows the bridge's manufacturer
            // and firmware alongside a child's should not be shown two answers.
            bridgeIdentity: this.bridgedIdentity,
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
                this.noteFabrics(String(action));
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

        // …and before `server.start()`, which is the whole of issue #141: the
        // aggregator must already have its children the first time a controller
        // can read `PartsList`.
        await this.restoreEndpoints();

        await server.start();

        // The §5 transition baseline. Taken before the identity assertion so a
        // fabric that arrives during it is a transition rather than the start.
        this.#fabricCount = this.fabrics().length;

        this.assertEndpointIdentity();
        await this.withdrawRestoredIfRefusing();

        this.log(`Matter node online on port ${this.config.matterPort}, storage ${this.config.storagePath}`);
        this.logPairing();
    }

    /**
     * Issue #141 — rebuild the last-known endpoint set from `endpoint-map.json`
     * **before** the Matter stack goes online.
     *
     * The defect this closes was measured on real hardware: after any restart
     * the node called `server.start()` with a childless aggregator and stayed
     * that way until the plugin connected and attached — 23 seconds on the
     * reference server after a reboot. Apple reconnects inside that window,
     * reads an empty `PartsList`, concludes every accessory has gone, and when
     * they reappear treats them as NEW accessories: dumped in the bridge's own
     * room with metadata the user can no longer edit. Every restart therefore
     * destroyed the user's room assignments for every exported device.
     *
     * Everything restored comes up **unreachable** (§3.5, PRD XAC8). The plugin
     * has confirmed nothing yet, and "present but not currently driven" is
     * exactly what `Reachable: false` means — an ecosystem greys the accessory
     * out, which is honest, and the first `attach`/`set_state` supplies the
     * truth. No state value is invented: each endpoint is built with its role's
     * defaults.
     *
     * **`attach` stays authoritative.** This restores a *set*, not a decision:
     * the plugin's next attach reconciles against it exactly as it always has,
     * creating what is missing, updating labels and state, and removing
     * anything genuinely un-exported while the plugin was away.
     *
     * Nothing is restored while refusing (PRD §7): a node that cannot trust its
     * map, or its identity, must not create endpoints at all — that refusal is
     * the only thing standing between a lost `Endpoint.id → number` allocation
     * and every accessory being duplicated in every paired ecosystem.
     */
    private async restoreEndpoints(): Promise<void> {
        if (this.#refusal !== undefined) {
            this.log(`Not restoring any endpoints from the map — ${this.#refusal}`);
            return;
        }
        if (this.#endpointMap.problem !== undefined) {
            // `load()` has already said what is wrong with the file; the point
            // worth adding is what that costs, which is this.
            this.log("Not restoring any endpoints from the map — it could not be read");
            return;
        }
        const restorable = this.#endpointMap.restorable();
        if (restorable.length === 0) {
            if (this.#endpointMap.size > 0) {
                // A v1 map, or one written before an endpoint was ever created.
                // Not a fault, but it IS the reason the bridge is about to come
                // up empty, and that is worth saying once rather than leaving
                // the user to infer it from silence.
                this.log(
                    `The endpoint map holds ${this.#endpointMap.size} number(s) but no role/label yet, so ` +
                        "nothing can be rebuilt before the plugin attaches — this start is the one that " +
                        "records them, and the next restart will restore.",
                );
            }
            return;
        }

        const specs: EndpointSpec[] = [];
        for (const entry of restorable) {
            const indigoDeviceId = indigoDeviceIdFrom(entry.uniqueId);
            if (indigoDeviceId === undefined || !isRole(entry.role) || !isSupportedRole(entry.role)) {
                // A map written by a NEWER node, or hand-edited. Skipped rather
                // than fatal: the plugin's attach still knows how to create it
                // if this build can, and refusing to start over one entry would
                // take every other accessory down with it.
                this.log(
                    `Endpoint map entry ${entry.uniqueId} (role ${entry.role}) cannot be rebuilt by this ` +
                        "bridge version; leaving it to the plugin's attach",
                );
                continue;
            }
            specs.push({
                indigoDeviceId,
                role: entry.role,
                label: entry.label,
                // §3.5/XAC8: nothing has confirmed this device's state yet.
                reachable: false,
                // Deliberately empty. The role's own defaults are the honest
                // starting point; inventing an `onOff` here would have the
                // ecosystem show a value nobody has read.
                states: {},
                options: {},
            });
        }
        if (specs.length === 0) {
            return;
        }

        const restored = await this.registry.restore(specs);
        this.log(
            `Restored ${restored} of ${specs.length} endpoint(s) from the endpoint map before going ` +
                "online, all unreachable until the plugin attaches — the bridge is never online with an " +
                "empty accessory list (issue #141)",
        );
    }

    /**
     * Take the restored endpoints back out if the post-start identity check
     * refused (issue #141 meeting PRD §7).
     *
     * The refusal decision needs `lifecycle.isCommissioned` and the fabric
     * table, so it cannot be taken before the stack is up — but the restore has
     * to happen before it, or there is no point to the restore at all. The one
     * refusal that can be raised *after* a restore is `fabricStorageLost`, and
     * it is by definition a node with **no fabrics**: there is no controller
     * attached to see the endpoints that briefly existed, and the invariant the
     * refusal actually protects ("nothing is created while refusing") is
     * restored here.
     *
     * A `mapUnreadable` refusal cannot reach this: an unreadable map restores
     * nothing in the first place.
     */
    private async withdrawRestoredIfRefusing(): Promise<void> {
        if (this.#refusal === undefined || this.registry.size === 0) {
            return;
        }
        const count = this.registry.size;
        this.log(
            `Withdrawing the ${count} endpoint(s) restored from the map — the node is refusing to serve ` +
                `(${this.#refusal}), and nothing may be created while it does`,
        );
        try {
            await this.registry.reconcile([], true);
        } catch (error) {
            this.log(
                `Could not withdraw the restored endpoints (${describeError(error)}); ${this.registry.size} ` +
                    "remain in the Matter tree even though the node is refusing to serve",
            );
        }
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
        const commissioned = this.server.lifecycle.isCommissioned;
        const reason = refuseReasonFor({
            commissioned,
            mapProblem: this.#endpointMap.problem,
            commissionedAt: this.identity.commissionedAt,
        });
        if (reason === undefined) {
            if (commissioned && !this.#endpointMap.present) {
                this.bootstrapEndpointMap();
            }
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

    /**
     * A commissioned bridge with no `endpoint-map.json` at all: adopt the
     * numbers matter.js has already been persisting, and serve.
     *
     * This is the upgrade path, and it is the common case rather than an edge
     * one — **every** bridge commissioned before E5 shipped has fabrics,
     * endpoints and no map, because the file did not exist. The first cut of E5
     * refused there, which would have stopped serving every already-paired
     * accessory the moment the node was updated.
     *
     * Seeding is legitimate precisely because our map does not allocate
     * anything: matter.js keys the real numbers on `Endpoint.id` in its own
     * store, this file is the independent witness, and writing down numbers
     * that are already true is not blessing anything — the number a
     * not-yet-created endpoint will get is already determined. That is the
     * difference from a **rebuild**, which discards a baseline that disagreed.
     *
     * The baseline starts empty and is filled by the first attach's `check`
     * from the live set. Those live numbers ARE matter.js's persisted ones —
     * it restores each `Endpoint.id`'s number from its own store as the
     * endpoint is created — so the witness this records is the pre-upgrade
     * truth, not a fresh allocation.
     *
     * An earlier cut read matter.js's store directly here, to also cover
     * endpoints it knows about but this attach does not export. It was deleted
     * after verification against a real pre-E5 storage directory: the chain
     * (`root`→`parts`→`aggregator`→`parts`, `contexts()`) enumerates only
     * subcontexts already materialised in memory, and at bootstrap time none
     * are, so it returned an empty list while reporting that it had adopted
     * numbers — a false success in a migration path. The numbers live in flat
     * on-disk keys whose layout is matter.js-internal. Not worth re-deriving:
     * a not-yet-exported endpoint's number is decided by `Endpoint.id`
     * whenever it IS created, so an entry for it buys nothing.
     */
    private bootstrapEndpointMap(): void {
        const source = "the first reconcile records the live set, which is matter.js's own persisted numbers";
        this.log(
            "MIGRATION: this bridge is commissioned but has no endpoint-number map — one is being " +
                `created now rather than refused. Nothing is renumbered: matter.js owns the numbers ` +
                `and this file only witnesses them (${source}).`,
        );
        // The line above is written before the answer is known, because it
        // explains what is being attempted — but it must not be left standing
        // as the last word when the attempt failed. A migration that never
        // reached disk has not happened: `present` is still false at the next
        // start, which bootstraps again, and saying otherwise would have the
        // log claiming a baseline that no restart can find.
        if (!this.#endpointMap.seed([], source)) {
            this.log(
                "MIGRATION NOT RECORDED: the endpoint-map baseline could not be written, so no " +
                    "baseline exists yet — the next start will bootstrap again. Drift cannot be " +
                    "detected until it lands.",
            );
        }
    }

    private refuse(reason: string): void {
        this.#refusal = reason;
        // The remedy is not the same for every reason, and printing the map one
        // for all of them sent the identity case somewhere that cannot help:
        // `rebuildEndpointMap` explicitly THROWS for `identityUnreadable` (see
        // §3.11), so a user following that sentence would run the one command
        // guaranteed to refuse them, and still be no closer to the file that
        // actually needs restoring.
        const remedy =
            reason === RefuseReason.identityUnreadable
                ? "Nothing will be exported until the quarantined identity.json.unreadable-<stamp> is " +
                  "restored or repaired as identity.json and the bridge restarted; rebuilding the " +
                  "endpoint map CANNOT fix this."
                : "Nothing will be exported until the endpoint map is rebuilt (BRIDGE_PROTOCOL §3.11). " +
                  "The rebuild adopts the numbers that exist now and renumbers nothing — any " +
                  "duplicate accessories were caused by the storage loss itself, not the rebuild.";
        this.log(`REFUSING to serve endpoints — ${reason}. ${remedy}`);
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
        this.applyWitness(
            WARN_IDENTITY_WRITE,
            markCommissioned(this.config.storagePath, this.identity, message => this.log(message)),
        );
    }

    /**
     * Adopt a {@link WitnessWrite}'s identity and record (or clear) its warning.
     *
     * The whole point of B1: these writes used to return only the identity, so
     * a caller could not tell a witness that reached disk from one that did not
     * — and both `factory_reset` and the drift baseline report success in a
     * voice that assumes it did.
     */
    private applyWitness(key: string, write: WitnessWrite): boolean {
        this.#identity = write.identity;
        if (write.persisted) {
            this.#warnings.delete(key);
        } else {
            this.#warnings.set(key, write.problem ?? "identity.json could not be written");
        }
        return write.persisted;
    }

    /**
     * §5 `fabrics_changed` / `commissioned` / `decommissioned`.
     *
     * One place, because the three are one fact seen at three grains and
     * deriving them separately is how they drift apart. `change` is `undefined`
     * for the callers that only want the transition re-checked — §3.9's last
     * leave and §3.10's erase both move the count without necessarily producing
     * an observable we saw.
     *
     * Never throws: it is called from inside matter.js's own observable and
     * from teardown paths where `fabrics()` legitimately fails.
     */
    private noteFabrics(change?: string): void {
        let fabrics: FabricInfo[];
        try {
            fabrics = this.fabrics();
        } catch (error) {
            // One `catch` used to swallow three separate things, and the worst
            // of them was silent and delayed: `commissionedAt` is cleared from
            // here (via noteLastFabricGone), so a read that fails on the way
            // out of a deliberate last-fabric unpair strands the witness — and
            // the NEXT start refuses to serve anything, blaming lost fabric
            // storage for something the user did on purpose. The §5 events go
            // with it, so an ecosystem pairing or unpairing lands in the plugin
            // as nothing at all. Log-only: this runs inside matter.js's own
            // observable and on teardown paths where the read legitimately
            // fails, and a throw there takes the stack down with it.
            try {
                this.log(
                    `Fabric list unavailable (${describeError(error)}); no fabrics_changed / ` +
                        "commissioned / decommissioned event will be raised for this change, and if " +
                        "this was the last fabric leaving, the commissioning witness has NOT been " +
                        "cleared — the next start would then refuse to serve endpoints, reporting " +
                        "lost fabric storage.",
                );
            } catch {
                // The logger itself failed; there is nowhere left to report.
            }
            return;
        }
        if (change !== undefined) {
            this.#fabricsListener?.(fabrics, change);
        }
        const was = this.#fabricCount;
        this.#fabricCount = fabrics.length;
        if (was === 0 && fabrics.length > 0) {
            this.#commissionedListener?.();
        } else if (was > 0 && fabrics.length === 0) {
            this.noteLastFabricGone();
            this.#decommissionedListener?.();
        }
    }

    /**
     * The fabric set has just emptied — which means matter.js has already
     * factory-reset itself, whoever caused it.
     *
     * `CommissioningServer` watches `commissioned` and calls `doFactoryReset()`
     * → `erase()` when the last fabric leaves. Our commissioning witness knows
     * nothing about that, so leaving it set makes the very NEXT start see
     * "witness says paired, stack says no fabrics" — the `fabricStorageLost`
     * signature — and refuse to serve anything, telling the user their Matter
     * storage was lost when in fact they deliberately unpaired their last
     * ecosystem.
     *
     * Deliberately here rather than in {@link removeFabric}: §3.9 is only one of
     * the ways to get here. An ecosystem that removes *us* from its side does it
     * too, and that route never touches our command handler at all.
     *
     * **The endpoint map's numbers are voided here too (issue #140), for the
     * same reason as §3.10's preserving reset.** matter.js has just erased
     * itself, so its own number allocation is gone with the fabrics — the map's
     * numbers are about to disagree with whatever gets re-created, and with no
     * fabric left there is no paired ecosystem that could still be holding the
     * old numbers to disagree with. {@link EndpointMapStore.voidNumbers} is a
     * no-op on an empty map, so this costs nothing on a bridge that had never
     * exported anything.
     */
    private noteLastFabricGone(): void {
        if (this.identity.commissionedAt === undefined) {
            return;
        }
        this.log(
            "The last fabric has gone — matter.js factory-resets itself when the fabric set empties, " +
                "so the commissioning witness is being cleared to match. Without this the next start " +
                "would refuse to serve endpoints, blaming lost storage for a deliberate unpairing.",
        );
        this.applyWitness(
            WARN_IDENTITY_WRITE,
            clearCommissioned(this.config.storagePath, this.identity, message => this.log(message)),
        );
        // `size > 0` first: `voidNumbers` returns `false` for an empty map too
        // (nothing to void is not a failure), and this bridge may never have
        // exported anything.
        if (
            this.#endpointMap.size > 0 &&
            !this.#endpointMap.voidNumbers(
                "the last fabric left — matter.js factory-reset itself, wiping its own endpoint-number " +
                    "allocation with it",
            )
        ) {
            // Same risk as the factory-reset branch above: the VOID markers
            // are RAM-only until a later persist succeeds (`#dirty` makes that
            // retry automatic), and a restart before then brings back #140's
            // forever-drift report for this unpairing.
            this.log(
                "The endpoint map's VOID markers could NOT be written to disk after the last fabric " +
                    "left — they exist in memory only for now and will be retried on the next " +
                    "successful persist; a restart before then will report the old §4.3 drift again.",
            );
        }
        this.#drift = [];
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
            warnings: [...this.#warnings.values(), ...this.#endpointMap.warnings],
        };
    }

    /**
     * The live set as `endpoint-map.json` records it: the number, plus the
     * `role`/`label` that let it be rebuilt at the next start (issue #141).
     */
    private liveIdentities(): LiveEndpointNumber[] {
        return (this.#registry?.identities() ?? []).map(identity => ({
            uniqueId: uniqueIdFor(identity.indigoDeviceId),
            endpointNumber: identity.endpointNumber,
            role: identity.role,
            label: identity.label,
        }));
    }

    /** The live set as the map keys it, for the before/after diff below. */
    private liveUniqueIds(): Set<string> {
        return new Set(
            (this.#registry?.identities() ?? []).map(identity => uniqueIdFor(identity.indigoDeviceId)),
        );
    }

    /**
     * Tell the map about endpoints this operation actually took away, so they
     * stop being restored on every boot (issue #141 follow-up).
     *
     * `check` is add-and-refresh only, so without this a device the user
     * un-exported kept its `role`/`label` for ever, was rebuilt as a child
     * endpoint before every `server.start()`, and was removed again by the next
     * attach — the exact appear-then-vanish churn the restore exists to stop,
     * pointed at the devices the user had already removed. It also cost the
     * plugin `REMOVAL_PACING_MS` per ghost on every attach while counting
     * towards neither the desired set nor the un-export debt, so a handful of
     * accumulated ghosts ate the attach deadline for a set nobody asked for.
     *
     * **Measured, not inferred.** The removals are the difference between the
     * live set before the operation and after it, which is the only honest
     * source: `attach`'s desired set is a request (a refused or part-applied
     * reconcile removed less than it asked to), and an empty live set proves
     * nothing at all. A recreate — removed and re-created in one reconcile — is
     * in both snapshots, so it is not a removal; and even if it were, the
     * {@link checkDrift} that follows re-records its role and label.
     */
    private forgetRemoved(before: ReadonlySet<string>): void {
        const after = this.liveUniqueIds();
        const removed = [...before].filter(uniqueId => !after.has(uniqueId));
        if (removed.length === 0) {
            return;
        }
        const forgotten = this.#endpointMap.forget(removed);
        if (forgotten > 0) {
            this.log(
                `${forgotten} un-exported endpoint(s) will no longer be restored at startup; their ` +
                    "endpoint numbers are kept (§3.3) so a re-export returns the same accessory",
            );
        }
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
        const drift = this.#endpointMap.check(this.liveIdentities());
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
        const before = this.liveUniqueIds();
        try {
            await this.registry.reconcile(endpoints, replaceAll);
        } finally {
            // Before the drift check, so a recreate — which is a remove and a
            // create in one plan — has its role and label put straight back by
            // the `check` below rather than being left as a bare number.
            this.forgetRemoved(before);
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

    /**
     * §3.3
     *
     * The drift check runs here too, even though a removal creates nothing.
     * matter.js re-allocates while endpoints come and go, and a removal is one
     * of the moments a *surviving* endpoint's number could differ from what the
     * map says — leaving the one operation that reshapes the live set as the
     * only one that never looked would have made that invisible until the next
     * upsert happened to notice.
     */
    async removeEndpoint(indigoDeviceId: number): Promise<RemoveResult> {
        const before = this.liveUniqueIds();
        try {
            return await this.registry.remove(indigoDeviceId);
        } finally {
            this.forgetRemoved(before);
            this.checkDrift();
        }
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

    /** §5: the sink for `fabrics_changed`. */
    onFabricsChanged(listener: (fabrics: FabricInfo[], change: string) => void): void {
        this.#fabricsListener = listener;
    }

    /** §5: the sink for `commissioned`. */
    onCommissioned(listener: () => void): void {
        this.#commissionedListener = listener;
    }

    /** §5: the sink for `decommissioned`. */
    onDecommissioned(listener: () => void): void {
        this.#decommissionedListener = listener;
    }

    /**
     * §3.9 — drop one ecosystem's fabric.
     *
     * Driven through `FabricManager` rather than the `OperationalCredentials`
     * cluster command for the same reason §3.8 avoids `AdministratorCommissioning`:
     * `OperationalCredentialsServer.removeFabric` opens with `assertRemoteActor`,
     * so it cannot be invoked from an offline agent at all.
     *
     * An index with no fabric behind it succeeds — but it succeeds as
     * `{removed: false}`, and that distinction is the whole reason this returns
     * anything. The plugin picks an index out of a `get_pairing` readout that is
     * by definition a moment old, and the way it goes stale is the ecosystem
     * unpairing itself, so "it is already gone" is the request being granted,
     * not refused. Answering `{}` for both made the plugin tell the user "that
     * ecosystem has been unpaired, every accessory has been removed" over a
     * no-op — and, because the early return also skipped {@link noteFabrics},
     * no `fabrics_changed` followed and the ghost row stayed in the plugin's
     * cached list (which is what its picker is built from) forever. Both halves
     * are fixed here: the outcome is reported, and the fabric set is
     * re-published either way so a stale cache is corrected by the very request
     * that tripped over it.
     *
     * **Removing the LAST fabric is a factory reset that we did not perform.**
     * matter.js's `CommissioningServer` watches `commissioned` and, when the
     * final fabric leaves, calls `doFactoryReset()` → `erase()` on its own. Our
     * commissioning witness knows nothing about that, so leaving it set made
     * the very next start see "witness says paired, no fabrics" — the
     * `fabricStorageLost` signature — and refuse to serve anything, telling the
     * user their Matter storage had been lost when in fact they had just
     * unpaired their last ecosystem on purpose. {@link noteLastFabricGone}
     * handles it, from {@link noteFabrics}, so the route where an ecosystem
     * unpairs *us* is covered too.
     */
    async removeFabric(fabricIndex: number): Promise<RemoveFabricResult> {
        const fabrics = this.server.env.get(FabricManager);
        const fabric = fabrics.maybeFor(FabricIndex(fabricIndex));
        if (fabric === undefined) {
            this.log(`No fabric at index ${fabricIndex}; nothing to remove`);
            // Re-publish the set the caller's index came from. Nothing changed
            // HERE, but the asker demonstrably holds a list that says otherwise,
            // and this is the only moment we know that. `"unchanged"` names the
            // §5 `change` for what it is rather than borrowing an action word.
            this.noteFabrics("unchanged");
            return { removed: false, remaining: this.fabricCountOrNull() };
        }
        // `leave` rather than `delete`: it flushes subscriptions and emits the
        // leave event first, which is how a controller learns it was removed
        // instead of simply losing the node.
        await fabric.leave();

        // Inside a try: the leave has already happened and succeeded, and a
        // read of server state that a self-reset is concurrently rebuilding
        // must not turn a completed removal into a failed command.
        let remaining: number | null;
        try {
            remaining = this.fabrics().length;
            this.log(`Removed fabric ${fabricIndex} (${remaining} remaining)`);
        } catch (error) {
            remaining = null;
            this.log(
                `Removed fabric ${fabricIndex}; the remaining fabric count is unavailable ` +
                    `(${describeError(error)})`,
            );
        }
        // Re-checked rather than assumed: `fabric.leave()` does not necessarily
        // produce a `fabricsChanged` observation we saw, and the empty-set case
        // is the one that matters (see noteLastFabricGone). Runs on the
        // count-unavailable path too — it is precisely the last-fabric leave
        // that makes the count unreadable, and that is the case whose witness
        // must be cleared.
        this.noteFabrics();
        return { removed: true, remaining };
    }

    /** The fabric count, or `null` if server state cannot be read right now. */
    private fabricCountOrNull(): number | null {
        try {
            return this.fabrics().length;
        } catch {
            return null;
        }
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
     *
     * **A preserved map has its numbers VOIDED, not drift-checked (issue #140).**
     * `erase()` wipes matter.js's own allocation, so every endpoint re-created
     * after this is renumbered from scratch — and the map disagreeing with that
     * on every future attach is not a real anomaly, it is the reset's own,
     * expected renumbering. `voidNumbers()` marks every entry so the next
     * `check()` silently adopts whatever number each `UniqueID` comes back with
     * instead of reporting it as drift forever; see its doc comment for why that
     * is safe (the fabric set is empty here, so no paired ecosystem can still be
     * holding the old numbers to disagree with).
     */
    async factoryReset(preserveEndpointNumbers: boolean): Promise<void> {
        this.#window.clear();
        this.log(
            `Factory reset: wiping commissioning credentials (endpoint numbers ` +
                `${preserveEndpointNumbers ? "preserved" : "DISCARDED"})`,
        );
        await this.server.erase();
        this.applyWitness(
            WARN_IDENTITY_WRITE,
            clearCommissioned(this.config.storagePath, this.identity, message => this.log(message)),
        );
        if (!preserveEndpointNumbers && !this.#endpointMap.discard()) {
            // The in-memory map is empty either way, so the node behaves as
            // asked — but the file the user asked to be rid of is still there,
            // and the next start would load it as a baseline for numbers that
            // no longer mean anything. `discard()` has always reported this;
            // nobody was reading it, under a "Factory reset complete" line that
            // said the opposite.
            this.log(
                `The endpoint map file survived the reset — ${join(this.config.storagePath, ENDPOINT_MAP_FILE)} ` +
                    "could not be deleted, so the next start will load it as a baseline. Delete it by hand.",
            );
        }
        this.#drift = [];
        // A reset node is un-commissioned, so nothing it could have refused for
        // is still true. Belt and braces rather than the main path: §1.1 keeps
        // `factory_reset` OUT of the recovery commands (see RECOVERY_COMMANDS —
        // §3.11 is the non-destructive exit and it already covers every refusal
        // state), so a reset cannot normally be reached while refusing. It can
        // still be reached in the other order — a refusal raised by a check
        // between the erase and here — and leaving one set would be an outage
        // with no remaining cause.
        this.#refusal = undefined;
        this.noteFabrics();

        // §3.10 promises the witness is gone. A write can report success and
        // still not be what the next start reads, and the failure mode is
        // specific and nasty: a `commissionedAt` that survived an erase is the
        // exact `fabricStorageLost` signature, so the next boot refuses to
        // serve anything and blames lost storage for the reset the user asked
        // for. Verified by reading it back rather than asserted.
        //
        // `readIdentity` alone cannot do the verifying: it answers `undefined`
        // both for "the file is gone, so the witness certainly is" and for "the
        // file is there and I cannot parse it", and reading the second as the
        // first reports a verification that never happened over the one file
        // that might still be carrying the marker.
        const unverifiable = identityFileProblem(this.config.storagePath);
        const onDisk = readIdentity(this.config.storagePath);
        if (unverifiable !== undefined) {
            const message =
                "Factory reset could NOT verify that the commissioning marker is gone, because " +
                `${unverifiable}. If that file still carries "commissionedAt", the next start will ` +
                "refuse to serve endpoints and report lost fabric storage — check it by hand.";
            this.log(message);
            this.#warnings.set(WARN_IDENTITY_WRITE, message);
        } else if (onDisk?.commissionedAt !== undefined) {
            const message =
                "Factory reset could NOT clear the commissioning marker in identity.json — the next " +
                "start will refuse to serve endpoints, reporting lost fabric storage. Delete " +
                `"commissionedAt" from ${this.config.storagePath}/identity.json by hand.`;
            this.log(message);
            this.#warnings.set(WARN_IDENTITY_WRITE, message);
        }

        // What preservation actually bought, checked rather than claimed.
        // `erase()` wipes matter.js's OWN allocation, so a preserved map is a
        // baseline for numbers that are about to be handed out afresh — it
        // preserves the ability to NOTICE, not the numbers (see §3.10). Issue
        // #140: the numbers this map already holds are about to disagree with
        // matter.js on every single re-created endpoint, and that is not a real
        // anomaly — it is the reset's own renumbering, guaranteed by the fact
        // that `erase()` just ran. Voiding them means the next `check()` (the
        // first reconcile after re-pairing) adopts the fresh numbers silently
        // instead of reporting the same "drift" on every attach forever, which
        // is #140's complaint: the plugin refuses the §3.11 rebuild the old log
        // line pointed at, because the node is otherwise perfectly healthy.
        if (preserveEndpointNumbers && this.#endpointMap.size > 0) {
            // `size > 0` gates both the call and the log: `voidNumbers` returns
            // `false` for an empty map too (nothing to void is not a failure),
            // and a bridge that had nothing preserved must not be told its
            // numbers are VOID, nor warned about a persist that never
            // happened.
            if (this.#endpointMap.voidNumbers(
                "factory reset (preserveEndpointNumbers: true) wiped matter.js's own allocation",
            )) {
                this.log(
                    "Endpoint-number map preserved but its numbers are now VOID: matter.js's own " +
                        "allocation was wiped by the reset, so they will be silently adopted as endpoints " +
                        "are re-created rather than reported as drift — no fabric survived the reset to " +
                        "still be holding the old numbers, so nothing outside this node can observe the " +
                        "difference. A §3.11 rebuild is not needed for this.",
                );
            } else {
                // The void markers exist in memory only — this write failed the
                // same way `discard()`'s can, and the risk is the mirror image:
                // a crash before the next successful persist loses the markers,
                // and the next start reports #140's forever-drift again as if
                // this reset had never happened. `persist()` already leaves
                // `#dirty` set on this failure, so the retry is automatic: the
                // very next successful `check()` or write re-attempts it.
                this.log(
                    "Endpoint-number map preserved but its VOID markers could NOT be written to disk " +
                        "— they exist in memory only for now. The next successful persist will retry " +
                        "and write them; if the node restarts before then, the old §4.3 drift report " +
                        "will return once, until this reset's renumbering is adopted again.",
                );
            }
        }

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
        // §3.11 is the way out of a MAP problem and nothing else. An unusable
        // `identity.json` is a different loss with a different remedy (restore
        // the file), and clearing the refusal for it would put a bridge serving
        // endpoints under a `SerialNumber` nobody has ever seen — which is the
        // exact harm the refusal exists to prevent.
        if (this.#refusal === RefuseReason.identityUnreadable) {
            throw new ProtocolError(
                ErrorCode.endpointMapInvalid,
                endpointMapInvalidDetails(
                    `${RefuseReason.identityUnreadable}, and rebuilding the endpoint map cannot fix ` +
                        "that — the unusable file was moved aside as identity.json.unreadable-<stamp>; " +
                        "restore or repair it and restart the bridge",
                ),
            );
        }

        const live = this.liveIdentities();
        if (!this.#endpointMap.rebuild(live)) {
            // The refusal deliberately stays. Answering with a StatusReport here
            // told the user "serving normally again" over a map that never
            // reached the disk, so the next start would refuse for the same
            // reason and the rebuild they confirmed would have to be confirmed
            // again — after they had been told it worked.
            throw new ProtocolError(
                ErrorCode.internal,
                "the rebuilt endpoint map could not be written, so nothing has changed — free some " +
                    `space in ${this.config.storagePath} and try again`,
            );
        }
        this.#drift = [];
        this.#refusal = undefined;
        if (!this.server.lifecycle.isCommissioned) {
            this.applyWitness(
                WARN_IDENTITY_WRITE,
                clearCommissioned(this.config.storagePath, this.identity, message => this.log(message)),
            );
        }
        this.log(
            live.length === 0
                ? "Endpoint map rebuilt from 0 live endpoints — the node was refusing, and §1.1 " +
                      "excludes attach from the recovery commands, so there were none to read. The " +
                      "baseline is now empty and the reconcile that follows records the real numbers; " +
                      "serving normally again"
                : `Endpoint map rebuilt from ${live.length} live endpoint(s); serving normally again`,
        );
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
     * §133 — the wire handler's `get_pairing` read. Bounded-retries the
     * transient "not initialized" window {@link noteLastFabricGone} leaves
     * behind (see {@link settlePairingRead}), so a `get_pairing` that lands
     * moments after a successful last-fabric unpair does not log a spurious
     * failure. Internal callers ({@link logPairing}) stay on the sync
     * {@link getPairing} — they run either after `server.start()` or at the
     * tail of a completed `factoryReset()`, both points where the stack is
     * already up and the race cannot occur.
     */
    async getPairingSettled(): Promise<PairingReport> {
        return settlePairingRead(() => this.getPairing());
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
