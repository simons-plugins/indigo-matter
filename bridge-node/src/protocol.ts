/**
 * Wire types for the plugin ⇄ bridge-node local protocol (BRIDGE_PROTOCOL.md).
 *
 * This module is deliberately free of matter.js imports: it is the contract,
 * and both the WebSocket server and its tests depend on it without dragging in
 * a Matter stack.
 */

/** Protocol version this node speaks. Skew fails closed on both peers (§2). */
export const PROTOCOL_VERSION = 1;

/** Seconds a connection may sit past the handshake without attaching (§2). */
export const UNATTACHED_TIMEOUT_MS = 10_000;

/**
 * Enhanced commissioning window bounds, in seconds (§3.8).
 *
 * Matter caps `CommissioningTimeout` at 900s and floors it at 180s. The cap is
 * ours to enforce: matter.js 0.17.8's `DeviceCommissioner` builds a
 * `STANDARD_COMMISSIONING_TIMEOUT` timer but never starts it, so nothing on the
 * Matter side would close a longer window.
 */
export const WINDOW_DURATION_MIN_SECONDS = 180;
export const WINDOW_DURATION_MAX_SECONDS = 900;
export const WINDOW_DURATION_DEFAULT_SECONDS = 900;

/** The complete `error_code` domain for protocol version 1 (§1.1). */
export const ErrorCode = {
    unknownCommand: "unknown_command",
    malformedArgs: "malformed_args",
    versionMismatch: "version_mismatch",
    notAttached: "not_attached",
    unknownDevice: "unknown_device",
    unknownRole: "unknown_role",
    roleChange: "role_change",
    massRemovalRefused: "mass_removal_refused",
    endpointMapInvalid: "endpoint_map_invalid",
    commissioningWindowFailed: "commissioning_window_failed",
    internal: "internal",
} as const;

export type ErrorCodeValue = (typeof ErrorCode)[keyof typeof ErrorCode];

/** The bare frame the node sends on every new connection, before `attach` (§2). */
export interface HandshakeFrame {
    protocolVersion: number;
    bridgeVersion: string;
    matterJsVersion: string;
}

/** plugin → node (§1). */
export interface RequestFrame {
    message_id: string;
    command: string;
    args?: Record<string, unknown>;
}

/** node → plugin, success (§1). */
export interface SuccessFrame {
    message_id: string;
    result: unknown;
}

/** node → plugin, failure (§1). */
export interface ErrorFrame {
    message_id: string;
    error_code: ErrorCodeValue;
    details: string;
}

/** §5 `window_closed` — why the enhanced commissioning window ended. */
export type WindowClosedReason = "expired" | "commissioned";

/**
 * The complete §5 event name domain. `window_closed` and `command` are emitted
 * today; the rest arrive with fabric/drift reporting. The whole set is declared
 * here so the fixture mirror can catch a misspelt name at compile time rather
 * than shipping an event the plugin logs as unknown and drops.
 */
export const EventName = {
    command: "command",
    fabricsChanged: "fabrics_changed",
    commissioned: "commissioned",
    decommissioned: "decommissioned",
    windowClosed: "window_closed",
    driftDetected: "drift_detected",
} as const;

export type EventNameValue = (typeof EventName)[keyof typeof EventName];

/** node → plugin, unsolicited (§1). */
export interface EventFrame {
    event: EventNameValue;
    data: Record<string, unknown>;
}

/**
 * The v1 role enum (§4.2) — the TypeScript mirror of `bridge_protocol.ROLES`.
 * Typing `EndpointSummary.role` as this rather than `string` is what makes a
 * typo in a golden frame or a summary a compile error on this side.
 */
export const Role = {
    onOffPlugInUnit: "onOffPlugInUnit",
    onOffLight: "onOffLight",
    dimmableLight: "dimmableLight",
    colorTemperatureLight: "colorTemperatureLight",
    extendedColorLight: "extendedColorLight",
    windowCovering: "windowCovering",
    doorLock: "doorLock",
    occupancySensor: "occupancySensor",
    contactSensor: "contactSensor",
    temperatureSensor: "temperatureSensor",
    humiditySensor: "humiditySensor",
    lightSensor: "lightSensor",
    pressureSensor: "pressureSensor",
    flowSensor: "flowSensor",
    thermostat: "thermostat",
} as const;

export type RoleValue = (typeof Role)[keyof typeof Role];

/** Runtime membership test for the §4.2 role enum — the `unknown_role` gate. */
export function isRole(value: unknown): value is RoleValue {
    return typeof value === "string" && Object.prototype.hasOwnProperty.call(Role, value);
}

/**
 * §3.1's opt-in for a reconcile that would empty the live endpoint set. The
 * literal is named because both the guard and its refusal message quote it.
 */
export const INTENT_REPLACE_ALL = "replace_all";

/**
 * The only commands accepted while the node is in the `endpoint_map_invalid`
 * refuse-to-start state (§1.1).
 *
 * `attach` is deliberately *not* here. A node that cannot vouch for its own
 * endpoint numbers must not create endpoints — creating them is precisely how a
 * lost map duplicates every accessory in every paired ecosystem — so the
 * reconcile that would create them is refused like everything else. The three
 * that remain are the ones a user needs to see the damage (`get_status`), keep
 * pairing readable (`get_pairing`), and choose the way out (§3.11).
 *
 * **`factory_reset` is not here either, and that is a decision rather than an
 * oversight.** It is arguably a legitimate exit — `preserveEndpointNumbers:
 * false` would discard the corrupt map along with everything else — but it is
 * strictly the bigger hammer: it destroys every ecosystem pairing, which §3.11
 * does not, and §3.11 already exits every refusal state there is. Admitting a
 * pairing-wiping command *alongside* a non-destructive one that solves the same
 * problem invites a user staring at a scary error to reach for the wrong one.
 * It is not a dead end: once §3.11 has run the node serves normally and
 * `factory_reset` is available like any other command, so the sequence costs
 * one extra step and nothing else.
 */
export const RECOVERY_COMMANDS: ReadonlySet<string> = new Set([
    "get_status",
    "get_pairing",
    "rebuild_endpoint_map",
]);

/** The `details` tail every `endpoint_map_invalid` refusal carries (§1.1). */
export const ENDPOINT_MAP_INVALID_SUFFIX =
    "; only get_status, get_pairing and rebuild_endpoint_map are accepted";

/**
 * Why the node is refusing. Kept as distinct reasons rather than one string
 * because they need different remedies: an unreadable map is fixed by §3.11,
 * while lost fabric storage is fixed by restoring a backup — and §3.11 there
 * means "accept that the pairings are gone".
 */
export const RefuseReason = {
    /**
     * `endpoint-map.json` is present but not usable.
     *
     * There is deliberately **no** reason for a map that is merely *absent* on a
     * commissioned bridge: matter.js owns the numbers and this file is only the
     * witness, so a missing witness renumbers nothing — it is bootstrapped from
     * matter.js's own persisted allocation instead (see `refuseReasonFor`).
     * Every pre-E5 install is in that state, and refusing there would have taken
     * working exports offline on upgrade to fix a file that never existed.
     */
    mapUnreadable: "endpoint map is unreadable",
    /** The commissioning witness says paired; matter.js says no fabrics (PRD §7). */
    fabricStorageLost:
        "this bridge was commissioned but its Matter fabric storage is gone, so every endpoint " +
        "number would be reallocated",
    /** `identity.json` is present but unusable — a new one would change our serial. */
    identityUnreadable: "the bridge identity file is present but unreadable",
} as const;

export type RefuseReasonValue = (typeof RefuseReason)[keyof typeof RefuseReason];

/** The full `details` for an `endpoint_map_invalid` refusal. */
export function endpointMapInvalidDetails(reason: string): string {
    return `${reason}${ENDPOINT_MAP_INVALID_SUFFIX}`;
}

/** §4.1 — the desired state of one exported device, as the plugin declares it. */
export interface EndpointSpec {
    indigoDeviceId: number;
    role: RoleValue;
    label: string;
    reachable: boolean;
    /** Role-specific state keys (§4.2). Values are Indigo-natural units. */
    states: Record<string, unknown>;
    /** Role-specific extras (e.g. window-covering polarity). Unused by E3 roles. */
    options: Record<string, unknown>;
}

/** §3.2 */
export interface UpsertResult {
    endpointNumber: number;
}

/** §3.3 */
export interface RemoveResult {
    removed: boolean;
}

/** §5 `command` — the `data` of an ecosystem-originated action. */
export interface CommandEventData extends Record<string, unknown> {
    indigoDeviceId: number;
    command: string;
    args: Record<string, unknown>;
}

/** §4.3 */
export interface FabricInfo {
    fabricIndex: number;
    label: string;
    vendorId: number;
}

/**
 * §3.9's answer — what the removal actually DID, not merely that it returned.
 *
 * `remove_fabric` used to answer `{}` whether it dropped a fabric or found
 * nothing at the index, and the plugin then told the user "that ecosystem has
 * been unpaired. Every accessory has been removed" over a node-side no-op. The
 * stale-index case is not an edge: the plugin's picker is built from a CACHED
 * fabric list, so an ecosystem that unpaired *us* between the last event and
 * the dialog opening is the designed way to land here.
 *
 * `remaining` is `null` — never a number — when the count could not be read.
 * The fabric set is legitimately mid-rebuild after a last-fabric leave
 * (matter.js factory-resets itself), and reporting a made-up `0` there would
 * be the same class of lie this type exists to end.
 */
export interface RemoveFabricResult {
    /** False when there was no fabric at that index: the request was already true. */
    removed: boolean;
    /** Fabrics the node holds now, or `null` when the count could not be read. */
    remaining: number | null;
}

/** §4.3 */
export interface StatusReport {
    commissioned: boolean;
    fabrics: FabricInfo[];
    endpointCount: number;
    endpoints: EndpointSummary[];
    drift: DriftEntry[];
    /**
     * Whether `drift` is an answer or an absence.
     *
     * `drift: []` alone is ambiguous, and the two readings could not be further
     * apart: "checked, nothing has moved" versus "there is no persisted map to
     * check against yet". Since E5 this is `true` once the detector has run
     * against a persisted baseline, and `false` before — on a fresh install,
     * and on any status read before the first reconcile.
     */
    driftChecked: boolean;
    /**
     * Persistence failures the node has hit and cannot fix on its own.
     *
     * The node's only other channel is stdout, and in this milestone it is
     * **started by hand** — so stdout is a terminal that was closed hours ago.
     * A map that could not be written, a commissioning witness that could not
     * be cleared, an identity that could not be saved: each of those is exactly
     * the class of fault this milestone exists to make visible, and each would
     * otherwise be visible nowhere at all. `get_status` is polled by the
     * plugin's watchdog, so this is the one path to a user's log.
     *
     * Empty is the normal state. Entries are current, not historical: a warning
     * disappears the moment the operation it describes succeeds.
     */
    warnings: string[];
}

export interface EndpointSummary {
    indigoDeviceId: number;
    endpointNumber: number;
    role: RoleValue;
}

export interface DriftEntry {
    uniqueId: string;
    expected: number;
    actual: number;
}

/** §3.7 */
export interface PairingReport {
    commissioned: boolean;
    windowOpen: boolean;
    windowExpiresAt: string | null;
    manualPairingCode: string | null;
    qrPairingCode: string | null;
    fabrics: FabricInfo[];
}

/** §3.8 */
export interface CommissioningWindowResult {
    manualPairingCode: string;
    qrPairingCode: string;
    windowExpiresAt: string;
}

/**
 * The slice of bridge behaviour the protocol server needs. Keeping it an
 * interface is what lets the protocol tests run without a live Matter stack.
 */
export interface BridgeFacade {
    getStatus(): StatusReport;
    getPairing(): PairingReport;
    openCommissioningWindow(durationSeconds: number): Promise<CommissioningWindowResult>;
    /**
     * §3.1's reconcile. `replaceAll` is the parsed `intent: "replace_all"`; the
     * mass-removal guard lives behind this seam because only the implementation
     * knows the live set.
     */
    reconcile(endpoints: readonly EndpointSpec[], replaceAll: boolean): Promise<StatusReport>;
    /** §3.2 — create-or-update. Rejects a role change with `role_change` (§4.1). */
    upsertEndpoint(spec: EndpointSpec): Promise<UpsertResult>;
    /** §3.3 — idempotent; `{removed: false}` for a device with no live endpoint. */
    removeEndpoint(indigoDeviceId: number): Promise<RemoveResult>;
    /** §3.4 — local (offline-context) writes, so they do not echo as `command`. */
    setState(indigoDeviceId: number, states: Record<string, unknown>): Promise<void>;
    /** §3.5 — Bridged Device Basic Information `Reachable`. */
    setReachable(indigoDeviceId: number, reachable: boolean): Promise<void>;
    /**
     * Register the sink for `window_closed` (§3.8/§5). One listener, last
     * registration wins — this seam is what lets the protocol server emit the
     * event without importing the Matter stack, and lets tests fire it.
     */
    onWindowClosed(listener: (reason: WindowClosedReason) => void): void;
    /** The same seam for §5 `command`, emitted when an ecosystem acts. */
    onCommand(listener: (data: CommandEventData) => void): void;
    /** The same seam for §5 `drift_detected` (§4.3). */
    onDriftDetected(listener: (drift: DriftEntry[]) => void): void;
    /**
     * The same seam for §5 `fabrics_changed`. Fires on every pairing or
     * unpairing the Matter stack observes, including the ones §3.9 and §3.10
     * cause themselves — a fabric the *plugin* removed still changed the set,
     * and the plugin's readout has no other way to learn it landed.
     */
    onFabricsChanged(listener: (fabrics: FabricInfo[], change: string) => void): void;
    /** §5 `commissioned` — the fabric count went from zero to non-zero. */
    onCommissioned(listener: () => void): void;
    /** §5 `decommissioned` — the last fabric went (§3.9's final leave, §3.10). */
    onDecommissioned(listener: () => void): void;
    /**
     * The reason this node is refusing everything outside
     * {@link RECOVERY_COMMANDS}, or `undefined` when it is serving normally.
     *
     * A getter rather than a constructor flag because §3.11 clears it: the
     * protocol server reads it per frame, so the command that fixes the state
     * takes effect on the very next one.
     */
    endpointMapRefusal(): string | undefined;
    /** §3.9 — drop one ecosystem's fabric. */
    removeFabric(fabricIndex: number): Promise<RemoveFabricResult>;
    /** §3.10 — wipe commissioning credentials and advertise fresh. */
    factoryReset(preserveEndpointNumbers: boolean): Promise<void>;
    /** §3.11 — adopt the live endpoint numbers as the new persisted map. */
    rebuildEndpointMap(): Promise<StatusReport>;
}

/** A protocol-level failure a command handler can throw to shape its response. */
export class ProtocolError extends Error {
    constructor(
        readonly code: ErrorCodeValue,
        details: string,
    ) {
        super(details);
        this.name = "ProtocolError";
    }
}

/** The human-readable message for an unknown throwable — what `details` carries. */
export function describeError(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

/** The same, with a stack when there is one — for the log, never for the wire. */
export function describeErrorWithStack(error: unknown): string {
    return error instanceof Error ? (error.stack ?? error.message) : String(error);
}
