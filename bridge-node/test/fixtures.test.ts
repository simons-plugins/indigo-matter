/**
 * §7 testing contract: the golden JSON (`../tests/fixtures/bridge_protocol/frames.json`,
 * at the repo root) is shared with the Python suite, so it cannot itself be
 * typed. `fixture-shapes.ts` is its compile-time mirror; this file is the
 * runtime half that keeps the two honest.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import * as shapes from "./fixture-shapes.js";
import { golden } from "./stub-bridge.js";

describe("golden fixtures match their typed mirror", () => {
    const cases: [string, unknown, unknown][] = [
        ["handshake", golden.handshake, shapes.handshake],
        ["attach result", golden.attach.response.result, shapes.status],
        ["get_status result", golden.get_status.response.result, shapes.status],
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
    ];

    for (const [name, actual, expected] of cases) {
        it(`${name} is shape-identical`, () => {
            assert.deepEqual(actual, expected);
        });
    }

    describe("pending E1 exchanges", () => {
        const names = Object.keys(golden.pending).filter(key => !key.startsWith("_"));

        it("every pending entry is a well-formed exchange", () => {
            assert.ok(names.length > 0, "pending section should not be empty while E1 is in flight");
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

        // Shape assertions wait for the node-side handlers: `protocol.ts` has no
        // types for these results yet, so there is nothing to mirror. The Python
        // client suite asserts them today — that half of E1 ships now.
        for (const name of names) {
            it.skip(`${name} — node handler not implemented yet`, () => {});
        }
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
