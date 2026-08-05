/**
 * Compile-time mirror of the implemented half of
 * `../tests/fixtures/bridge_protocol/frames.json` — the repo-root golden file
 * shared with the Python suite (§7 testing contract).
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
    FabricInfo,
    HandshakeFrame,
    PairingReport,
    RemoveResult,
    StatusReport,
    UpsertResult,
} from "../src/protocol.js";

/** The one paired ecosystem the populated fixtures assume. */
const APPLE_HOME = { fabricIndex: 1, label: "Apple Home", vendorId: 4937 } satisfies FabricInfo;

/** Versions are placeholders: the real ones track package.json / matter.js. */
export const handshake = {
    protocolVersion: 1,
    bridgeVersion: "0.0.0-test",
    matterJsVersion: "0.0.0-test",
} satisfies HandshakeFrame;

/**
 * The status of a bridge that has reconciled the {@link attachWithEndpoints}
 * set — what `get_status` answers once `attach` has run. Both frames share it,
 * which is exactly the §6.2 invariant: `attach` returns the same StatusReport
 * `get_status` would.
 */
export const status = {
    commissioned: true,
    fabrics: [APPLE_HOME],
    endpointCount: 2,
    endpoints: [
        { indigoDeviceId: 123456789, endpointNumber: 2, role: "onOffLight" },
        { indigoDeviceId: 123456790, endpointNumber: 3, role: "dimmableLight" },
    ],
    drift: [],
} satisfies StatusReport;

/**
 * The §3.1 answer to an `attach` carrying `endpoints: []` against a node with
 * nothing live — an empty desired set reconciles to an empty live set. The
 * mass-removal guard does not fire: there was nothing to remove.
 */
export const statusEmpty = {
    commissioned: false,
    fabrics: [],
    endpointCount: 0,
    endpoints: [],
    drift: [],
} satisfies StatusReport;

/** §3.1 with `intent: "replace_all"`: the live set is emptied deliberately. */
export const statusReplaceAll = {
    commissioned: true,
    fabrics: [APPLE_HOME],
    endpointCount: 0,
    endpoints: [],
    drift: [],
} satisfies StatusReport;

/** §3.2 — the live endpoint's Matter number, for the plugin's own records. */
export const upsertResult = { endpointNumber: 2 } satisfies UpsertResult;

/** §3.3 — the two idempotent outcomes. */
export const removeResult = { removed: true } satisfies RemoveResult;
export const removeAbsentResult = { removed: false } satisfies RemoveResult;

/** §3.4/§3.5 both answer with an empty result on success. */
export const emptyResult = {};

/** §3.1: emptying a non-empty live set needs `intent: "replace_all"`. */
export const massRemovalRefused = {
    message_id: "m12",
    error_code: "mass_removal_refused",
    details: "attach would remove all 2 live endpoints without intent: replace_all",
} satisfies ErrorFrame;

/** §4.1: ecosystems cache device types per endpoint, so a role change is a refusal. */
export const roleChange = {
    message_id: "m14",
    error_code: "role_change",
    details: "endpoint 123456789 is onOffLight; remove and re-add to change role",
} satisfies ErrorFrame;

/** §3.4 against a device with no live endpoint. */
export const setStateUnknownDevice = {
    message_id: "m18",
    error_code: "unknown_device",
    details: "no live endpoint for indigoDeviceId 123456791",
} satisfies ErrorFrame;

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
