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
        { indigoDeviceId: 123456789, endpointNumber: 2, publishedAs: "indigo-123456789", role: "onOffLight" },
        { indigoDeviceId: 123456790, endpointNumber: 3, publishedAs: "indigo-123456790", role: "dimmableLight" },
    ],
    drift: [],
    // §4.3: false until E5 persists the endpoint-number map — an empty `drift`
    // on its own would read as an all-clear nobody has actually checked.
    driftChecked: false,
    // §4.3: empty is the healthy state. Non-empty means a persistence failure
    // the node could not fix, surfaced here because the node's only other
    // channel is a stdout nobody is watching.
    warnings: [],
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
    driftChecked: false,
    warnings: [],
} satisfies StatusReport;

/** §3.1 with `intent: "replace_all"`: the live set is emptied deliberately. */
export const statusReplaceAll = {
    commissioned: true,
    fabrics: [APPLE_HOME],
    endpointCount: 0,
    endpoints: [],
    drift: [],
    driftChecked: false,
    warnings: [],
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

/**
 * §3.4 against a live device, with keys its role does not speak.
 *
 * The sibling of {@link setStateUnknownDevice}, and the more dangerous of the
 * two: the device exists, so nothing looks wrong. Answering `{}` here would
 * report success for a write that produced no patch at all, and the plugin —
 * which does not await `set_state` — would never find out. `level` on an
 * `onOffLight` is the shape of it that actually happens: a role edited in the
 * export dialog while the plugin keeps pushing the old role's states.
 */
export const setStateBadKeys = {
    message_id: "m44",
    error_code: "malformed_args",
    details: "role onOffLight consumed none of the states given; rejected key(s): level (§4.2)",
} satisfies ErrorFrame;

/** §4.2/§1.1: a lawful frame naming a role outside the v1 enum. */
export const upsertUnknownRole = {
    message_id: "m29",
    error_code: "unknown_role",
    details: "role airPurifier is not in the v1 role enum (§4.2)",
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

/**
 * §3.9's two outcomes. It stopped answering `{}` when it turned out that "there
 * was no fabric at that index" and "the ecosystem has been unpaired" were
 * reaching the user as the same sentence — over a picker built from a cached
 * fabric list, which makes the stale index the designed path rather than a typo.
 */
export const removeFabricResult = { removed: true, remaining: 1 };
export const removeFabricAlreadyGone = { removed: false, remaining: 2 };

/** §3.10 still answers with an empty result; named for what it means. */
export const factoryResetResult = {};

/**
 * §3.11's answer — a `StatusReport` like any other, with two things that only
 * this frame says.
 *
 * The endpoint numbers are 4 and 5 where every other status frame says 2 and 3:
 * a rebuild is the command for the case where the numbers have *already* moved,
 * and a fixture that showed them unmoved would be modelling the one situation in
 * which nobody would run it.
 *
 * `driftChecked` is `true` because §4.3's rule is "false until the node persists
 * the endpoint-number map", and a rebuild persists one. The empty `drift` beside
 * it is trivially true — the map was written *from* the live set one line
 * earlier — and says nothing about the mapping that was discarded.
 */
export const rebuiltStatus = {
    commissioned: true,
    fabrics: [APPLE_HOME],
    endpointCount: 2,
    endpoints: [
        { indigoDeviceId: 123456789, endpointNumber: 4, publishedAs: "indigo-123456789", role: "onOffLight" },
        { indigoDeviceId: 123456790, endpointNumber: 5, publishedAs: "indigo-123456790", role: "dimmableLight" },
    ],
    drift: [],
    driftChecked: true,
    warnings: [],
} satisfies StatusReport;

/**
 * §4.1 issue #220: `batteryLevel` against an endpoint that was not created
 * with a battery. The onOffLight from {@link attachWithEndpoints}
 * (123456789) is the target — it is `attach_with_endpoints`' own device,
 * created without `battery`, chosen because it is the plainest possible
 * "this device is not battery-capable" fixture already in the file.
 */
export const setStateBatteryOnMainsEndpoint = {
    message_id: "m70",
    error_code: "malformed_args",
    details: "endpoint 123456789 was not created with a battery, so batteryLevel cannot be " +
        "published; re-export the device (§4.1)",
} satisfies ErrorFrame;

/**
 * §1.1's refuse-to-start refusal (PRD §7).
 *
 * The request it answers is an ordinary `upsert_endpoint`, chosen because it is
 * the most innocuous thing the plugin does: the point of the frame is that in
 * this state *everything* outside the three recovery commands is refused,
 * including work that has nothing to do with the endpoint map.
 */
export const endpointMapInvalid = {
    message_id: "m24",
    error_code: "endpoint_map_invalid",
    details: "endpoint map is unreadable; only get_status, get_pairing and rebuild_endpoint_map are accepted",
} satisfies ErrorFrame;
