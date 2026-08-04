/**
 * §7 testing contract: the golden JSON is shared with the Python suite, so it
 * cannot itself be typed. `fixture-shapes.ts` is its compile-time mirror; this
 * file is the runtime half that keeps the two honest.
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
