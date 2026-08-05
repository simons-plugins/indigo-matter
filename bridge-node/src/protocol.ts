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

/** §4.3. `drift` is always empty until E5 adds the persisted endpoint-number map. */
export interface StatusReport {
    commissioned: boolean;
    fabrics: FabricInfo[];
    endpointCount: number;
    endpoints: EndpointSummary[];
    drift: DriftEntry[];
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
