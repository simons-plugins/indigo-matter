/**
 * §7 testing contract: the golden JSON (`../tests/fixtures/bridge_protocol/frames.json`,
 * at the repo root) is shared with the Python suite, so it cannot itself be
 * typed. `fixture-shapes.ts` is its compile-time mirror; this file is the
 * runtime half that keeps the two honest.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { EventName } from "../src/protocol.js";
import * as shapes from "./fixture-shapes.js";
import type { GoldenEvent } from "./stub-bridge.js";
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
        ["remove_endpoint result", golden.remove_endpoint.response.result, shapes.removeResult],
        ["remove_endpoint (absent) result", golden.remove_endpoint_absent.response.result, shapes.removeAbsentResult],
        ["set_state result", golden.set_state.response.result, shapes.emptyResult],
        ["set_state unknown_device", golden.set_state_unknown_device.response, shapes.setStateUnknownDevice],
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

    describe("pending exchanges (§3.9-§3.11 and the E4 roles)", () => {
        const names = Object.keys(golden.pending).filter(key => !key.startsWith("_"));

        it("every pending entry is a well-formed exchange", () => {
            assert.ok(names.length > 0, "pending section should not be empty while E4 is outstanding");
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
