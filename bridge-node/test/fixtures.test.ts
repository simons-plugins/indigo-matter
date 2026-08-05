/**
 * §7 testing contract: the golden JSON (`../tests/fixtures/bridge_protocol/frames.json`,
 * at the repo root) is shared with the Python suite, so it cannot itself be
 * typed. `fixture-shapes.ts` is its compile-time mirror; this file is the
 * runtime half that keeps the two honest.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { rejectedStateKeys } from "../src/endpoints.js";
import { type EndpointSpec, EventName, Role, type RoleValue } from "../src/protocol.js";
import * as shapes from "./fixture-shapes.js";
import type { GoldenEvent, GoldenExchange } from "./stub-bridge.js";
import { golden } from "./stub-bridge.js";

/** The §5 domain, as runtime values, for the event sweep below. */
const EVENT_NAMES: readonly string[] = Object.values(EventName);

/**
 * Every top-level event frame in the golden file, found by shape rather than by
 * name — a new `command`/`fabrics_changed`/… fixture is swept the moment it is
 * added, instead of sitting in the file referenced by nothing.
 */
function goldenEvents(): [string, GoldenEvent][] {
    return Object.entries(golden as Record<string, unknown>).filter(
        (entry): entry is [string, GoldenEvent] => {
            const value = entry[1];
            return typeof value === "object" && value !== null && "event" in value;
        },
    );
}

describe("golden fixtures match their typed mirror", () => {
    const cases: [string, unknown, unknown][] = [
        ["handshake", golden.handshake, shapes.handshake],
        // §3.1: the golden attach REQUEST carries `endpoints: []`, so the lawful
        // answer against a node with nothing live is an empty live set.
        ["attach result", golden.attach.response.result, shapes.statusEmpty],
        ["get_status result", golden.get_status.response.result, shapes.status],
        ["attach_with_endpoints result", golden.attach_with_endpoints.response.result, shapes.status],
        ["attach_replace_all result", golden.attach_replace_all.response.result, shapes.statusReplaceAll],
        ["attach mass_removal_refused", golden.attach_mass_removal_refused.response, shapes.massRemovalRefused],
        ["upsert_endpoint result", golden.upsert_endpoint.response.result, shapes.upsertResult],
        ["upsert_endpoint role_change", golden.upsert_endpoint_role_change.response, shapes.roleChange],
        ["upsert_endpoint unknown_role", golden.upsert_endpoint_unknown_role.response, shapes.upsertUnknownRole],
        ["remove_endpoint result", golden.remove_endpoint.response.result, shapes.removeResult],
        ["remove_endpoint (absent) result", golden.remove_endpoint_absent.response.result, shapes.removeAbsentResult],
        ["set_state result", golden.set_state.response.result, shapes.emptyResult],
        ["set_state unknown_device", golden.set_state_unknown_device.response, shapes.setStateUnknownDevice],
        ["set_state malformed_args (bad keys)", golden.set_state_bad_keys.response, shapes.setStateBadKeys],
        ["set_reachable result", golden.set_reachable.response.result, shapes.emptyResult],
        ["get_pairing (uncommissioned)", golden.get_pairing_uncommissioned.response.result, shapes.pairingUncommissioned],
        ["get_pairing (commissioned)", golden.get_pairing_commissioned.response.result, shapes.pairingCommissioned],
        [
            "get_pairing (commissioned, window open)",
            golden.get_pairing_commissioned_window_open.response.result,
            shapes.pairingCommissionedWindowOpen,
        ],
        ["open_commissioning_window result", golden.open_commissioning_window.response.result, shapes.commissioningWindow],
        ["window_closed (expired)", golden.window_closed_expired, shapes.windowClosedExpired],
        ["window_closed (commissioned)", golden.window_closed_commissioned, shapes.windowClosedCommissioned],
        ["attach version_mismatch", golden.attach_version_mismatch.response, shapes.versionMismatch],
        ["not_attached", golden.not_attached.response, shapes.notAttached],
        ["unknown_command", golden.unknown_command.response, shapes.unknownCommand],
        ["open_commissioning_window malformed_args", golden.open_window_malformed_args.response,
            shapes.openWindowMalformedArgs],
        ["open_commissioning_window commissioning_window_failed", golden.open_window_failed.response,
            shapes.openWindowFailed],
        ["open_commissioning_window internal", golden.open_window_internal.response,
            shapes.openWindowInternal],
    ];

    for (const [name, actual, expected] of cases) {
        it(`${name} is shape-identical`, () => {
            assert.deepEqual(actual, expected);
        });
    }

    describe("the §4.2 role table, from the golden frames", () => {
        /** `attach_all_roles`' endpoint specs — one per v1 role, by contract. */
        const specs = (golden.attach_all_roles.request.args as { endpoints: EndpointSpec[] }).endpoints;
        const byDeviceId = new Map(specs.map(spec => [spec.indigoDeviceId, spec]));

        /**
         * Every `set_state_*` frame at the top level, paired with the role its
         * device was attached as. Found by device id rather than by decoding
         * the frame's own name, so a fixture renamed from
         * `set_state_light_sensor` to something else is still swept.
         */
        function setStateFrames(): [string, EndpointSpec, Record<string, unknown>][] {
            return Object.entries(golden as Record<string, unknown>)
                .filter((entry): entry is [string, GoldenExchange] => entry[0].startsWith("set_state_"))
                .flatMap(([name, exchange]) => {
                    const args = exchange.request.args as { indigoDeviceId?: number; states?: unknown };
                    const spec = byDeviceId.get(args.indigoDeviceId ?? -1);
                    // set_state_unknown_device / _bad_keys are deliberately about
                    // devices that were never attached; they have their own
                    // shape assertions above.
                    return spec === undefined ? [] : [[name, spec, args.states as Record<string, unknown>]];
                });
        }

        it("attaches every v1 role exactly once", () => {
            assert.deepEqual(
                specs.map(spec => spec.role).sort(),
                [...(Object.values(Role) as RoleValue[])].sort(),
                "attach_all_roles is the file's only statement of the whole §4.2 enum",
            );
        });

        it("builds every attached spec's initial state without refusing a key", () => {
            // `createEndpoint` runs the same statePatchOrRefuse as `set_state`,
            // so an initial `states` the role does not speak is a malformed_args
            // at attach — which would take the whole reconcile down.
            for (const spec of specs) {
                assert.deepEqual(rejectedStateKeys(spec.role, spec.states), [], `${spec.role} initial states`);
            }
        });

        it("consumes every key of every role's set_state frame", () => {
            // The cross-check that matters most: the golden file is the shared
            // §4.2 vocabulary, and this asserts the node's converters answer to
            // all of it. A mistyped key in `endpoints.ts` (`humidityPercent`
            // for `humidityPct`) would otherwise be a silent no-op that only
            // shows as a sensor stuck at "unknown" in someone's Home app.
            const frames = setStateFrames();
            assert.ok(frames.length > 0, "no set_state frames matched an attached device");
            for (const [name, spec, states] of frames) {
                assert.ok(Object.keys(states).length > 0, `${name} sends no states`);
                assert.deepEqual(rejectedStateKeys(spec.role, states), [], `${name} (${spec.role})`);
            }
        });

        it("gives every role its own set_state frame", () => {
            const covered = new Set(setStateFrames().map(([, spec]) => spec.role));
            // `onOffLight`'s frame is the plain top-level `set_state`, which
            // targets the same device id the other attach fixtures use rather
            // than one of attach_all_roles' — hence the explicit add.
            covered.add(Role.onOffLight);
            for (const role of Object.values(Role) as RoleValue[]) {
                assert.ok(covered.has(role), `no set_state golden frame for ${role}`);
            }
        });
    });

    describe("pending exchanges (§3.9-§3.11)", () => {
        const names = Object.keys(golden.pending).filter(key => !key.startsWith("_"));

        it("every pending entry is a well-formed exchange", () => {
            assert.ok(names.length > 0, "the fabric/reset/rebuild family is still outstanding");
            for (const name of names) {
                const exchange = golden.pending[name];
                assert.ok(exchange?.request !== undefined, `${name} has no request`);
                assert.ok(exchange?.response !== undefined, `${name} has no response`);
                assert.equal(
                    exchange.request.message_id,
                    exchange.response.message_id,
                    `${name} request/response message_id must match`,
                );
            }
        });

        // Shape assertions wait for the node-side handlers. The Python client
        // suite asserts them today — the plugin half shipped at E1 — and a
        // frame graduates to a real deepEqual above when the node grows its
        // handler, which is what E3 did to the endpoint-CRUD family.
        for (const name of names) {
            it.skip(`${name} — node handler not implemented yet`, () => {});
        }
    });

    describe("§5 event frames", () => {
        // These used to be declared on GoldenFrames and referenced by nothing:
        // seven frames the TS side carried but never checked, so a malformed one
        // would only ever have been caught by the Python suite.
        const events = goldenEvents();

        it("every event frame is a well-formed envelope with a §5 name", () => {
            assert.ok(events.length > 0, "the golden file must carry §5 event frames");
            for (const [name, frame] of events) {
                assert.ok(EVENT_NAMES.includes(frame.event), `${name}: ${frame.event} is not a §5 event`);
                assert.equal(typeof frame.data, "object", `${name} has no data object`);
                assert.notEqual(frame.data, null, `${name} has a null data`);
                // §1: events carry no message_id — that is what distinguishes them.
                assert.ok(!("message_id" in frame), `${name} must not carry a message_id`);
                assert.deepEqual(Object.keys(frame).sort(), ["data", "event"], `${name} envelope`);
            }
        });

        it("covers every §5 event name", () => {
            const covered = new Set(events.map(([, frame]) => frame.event));
            for (const name of EVENT_NAMES) {
                assert.ok(covered.has(name), `no golden frame for the ${name} event`);
            }
        });
    });

    it("covers all four §3.7 pairing states", () => {
        // uncommissioned+window, commissioned+no window, commissioned+window;
        // the fourth (uncommissioned, no window) cannot exist — an
        // uncommissioned node always advertises.
        const states = [
            shapes.pairingUncommissioned,
            shapes.pairingCommissioned,
            shapes.pairingCommissionedWindowOpen,
        ].map(state => `${state.commissioned}/${state.windowOpen}`);
        assert.deepEqual(states.sort(), ["false/true", "true/false", "true/true"]);
        assert.notEqual(shapes.pairingCommissionedWindowOpen.windowExpiresAt, null);
    });
});
