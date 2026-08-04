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

/** node → plugin, unsolicited (§1). */
export interface EventFrame {
    event: string;
    data: Record<string, unknown>;
}

/** §4.3 */
export interface FabricInfo {
    fabricIndex: number;
    label: string;
    vendorId: number;
}

/** §4.3 — the E0 subset; `drift` is always empty until E6 adds the allocator. */
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
    role: string;
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
