/**
 * Compile-time mirror of the E0 half of `../tests/fixtures/bridge_protocol/frames.json`
 * — the repo-root golden file shared with the Python suite (§7 testing contract).
 *
 * `JSON.parse` erases types, so the golden file alone cannot fail `tsc` when a
 * shape in `protocol.ts` drifts. Each payload is restated here bound with
 * `satisfies` to its protocol type; `fixtures.test.ts` then deep-equals mirror
 * against JSON. Between the two, a shape change has to be made in three places
 * on purpose or something goes red:
 *
 *   - rename a field in `protocol.ts`  → this module fails to compile
 *   - edit the JSON without the mirror → `fixtures.test.ts` fails
 *   - edit the mirror without the JSON → `fixtures.test.ts` fails
 */

import type {
    CommissioningWindowResult,
    ErrorFrame,
    EventFrame,
    HandshakeFrame,
    PairingReport,
    StatusReport,
} from "../src/protocol.js";

/** Versions are placeholders: the real ones track package.json / matter.js. */
export const handshake = {
    protocolVersion: 1,
    bridgeVersion: "0.0.0-test",
    matterJsVersion: "0.0.0-test",
} satisfies HandshakeFrame;

/** What the E0 node actually serves: one hard-coded endpoint, ignoring `attach`. */
export const status = {
    commissioned: false,
    fabrics: [],
    endpointCount: 1,
    endpoints: [{ indigoDeviceId: 999001, endpointNumber: 2, role: "onOffPlugInUnit" }],
    drift: [],
} satisfies StatusReport;

/**
 * The lawful §3.1 answer to the golden `attach` REQUEST, which carries
 * `endpoints: []` — an empty desired set reconciles to an empty live set.
 *
 * The E0 node does not reconcile yet (it ignores the requested set and serves
 * {@link status}), so `protocol.test.ts` asserts attach against the live status
 * instead. The two converge when E2 makes `attach` do the reconcile for real.
 */
export const statusEmpty = {
    commissioned: false,
    fabrics: [],
    endpointCount: 0,
    endpoints: [],
    drift: [],
} satisfies StatusReport;

/** §3.7 state 1: never commissioned — the basic window with the persisted codes. */
export const pairingUncommissioned = {
    commissioned: false,
    windowOpen: true,
    windowExpiresAt: null,
    manualPairingCode: "34970112332",
    qrPairingCode: "MT:-24J0AFN00KA0648G00",
    fabrics: [],
} satisfies PairingReport;

/** §3.7 state 2: commissioned, no window — codes are gone and stay gone. */
export const pairingCommissioned = {
    commissioned: true,
    windowOpen: false,
    windowExpiresAt: null,
    manualPairingCode: null,
    qrPairingCode: null,
    fabrics: [{ fabricIndex: 1, label: "Apple Home", vendorId: 4937 }],
} satisfies PairingReport;

/** §3.7 state 3: commissioned *and* an enhanced window open — non-null expiry. */
export const pairingCommissionedWindowOpen = {
    commissioned: true,
    windowOpen: true,
    windowExpiresAt: "2026-08-04T12:15:00.000Z",
    manualPairingCode: "34970112332",
    qrPairingCode: "MT:-24J0AFN00KA0648G00",
    fabrics: [{ fabricIndex: 1, label: "Apple Home", vendorId: 4937 }],
} satisfies PairingReport;

export const commissioningWindow = {
    manualPairingCode: "34970112332",
    qrPairingCode: "MT:-24J0AFN00KA0648G00",
    windowExpiresAt: "2026-08-04T12:15:00.000Z",
} satisfies CommissioningWindowResult;

export const windowClosedExpired = {
    event: "window_closed",
    data: { reason: "expired" },
} satisfies EventFrame;

export const windowClosedCommissioned = {
    event: "window_closed",
    data: { reason: "commissioned" },
} satisfies EventFrame;

export const versionMismatch = {
    message_id: "m2",
    error_code: "version_mismatch",
    details: "Node speaks protocol version 1, client sent 2",
} satisfies ErrorFrame;

export const notAttached = {
    message_id: "m3",
    error_code: "not_attached",
    details: "get_status requires a successful attach first",
} satisfies ErrorFrame;

export const unknownCommand = {
    message_id: "m4",
    error_code: "unknown_command",
    details: "Unknown command no_such_command",
} satisfies ErrorFrame;

/** §3.8 rejects a duration outside the Matter 180-900s bounds. */
export const openWindowMalformedArgs = {
    message_id: "m25",
    error_code: "malformed_args",
    details: "durationSeconds must be an integer 180-900",
} satisfies ErrorFrame;

/** §3.8: the Matter stack refused — a ProtocolError the facade threw. */
export const openWindowFailed = {
    message_id: "m26",
    error_code: "commissioning_window_failed",
    details: "A commissioning window is already open",
} satisfies ErrorFrame;

/** §1.1 `internal`: any other facade failure, with its message as `details`. */
export const openWindowInternal = {
    message_id: "m27",
    error_code: "internal",
    details: "mDNS advertiser is down",
} satisfies ErrorFrame;
