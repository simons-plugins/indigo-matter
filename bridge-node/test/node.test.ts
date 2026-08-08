/**
 * §133 — `settlePairingRead`'s retry/backoff/deadline behaviour, exercised
 * against a fake `read` with an injected fake clock. No live Matter stack
 * is started here (importing `node.js` pulls in `@matter/main`'s classes,
 * but nothing is constructed) — `UninitializedDependencyError` is real
 * matter.js so the transient-detection path under test is the real one.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { UninitializedDependencyError } from "@matter/main";

import { settlePairingRead } from "../src/node.js";
import type { PairingReport } from "../src/protocol.js";

const REPORT: PairingReport = {
    commissioned: true,
    windowOpen: false,
    windowExpiresAt: null,
    manualPairingCode: null,
    qrPairingCode: null,
    fabrics: [],
};

/** The exact shape `Construction#assert` throws while a subject is Inactive. */
function transientError(): UninitializedDependencyError {
    return new UninitializedDependencyError("indigo-matter-bridge", "is not initialized");
}

/**
 * A fake clock where `sleep` advances `now` by exactly the requested amount
 * instead of actually waiting — deterministic, and instant regardless of
 * `deadlineMs`/`intervalMs`, the same trick `window.test.ts` uses for its
 * injected scheduler.
 */
function fakeClock(): { now: () => number; sleep: (ms: number) => Promise<void>; sleeps: number[] } {
    let time = 0;
    const sleeps: number[] = [];
    return {
        now: () => time,
        sleep: async ms => {
            sleeps.push(ms);
            time += ms;
        },
        sleeps,
    };
}

describe("settlePairingRead (§133)", () => {
    it("retries the transient not-initialized error until the read succeeds", async () => {
        const clock = fakeClock();
        let calls = 0;
        const result = await settlePairingRead(
            () => {
                calls++;
                if (calls <= 2) {
                    throw transientError();
                }
                return REPORT;
            },
            { now: clock.now, sleep: clock.sleep },
        );
        assert.deepEqual(result, REPORT);
        assert.equal(calls, 3, "two failures then a success: three reads");
        assert.equal(clock.sleeps.length, 2, "one sleep per retry, none after the eventual success");
    });

    it("rethrows the SAME error, unwrapped, once the deadline elapses", async () => {
        const clock = fakeClock();
        const err = transientError();
        let calls = 0;
        await assert.rejects(
            () =>
                settlePairingRead(
                    () => {
                        calls++;
                        throw err;
                    },
                    { now: clock.now, sleep: clock.sleep, deadlineMs: 200, intervalMs: 50 },
                ),
            (thrown: unknown) => thrown === err,
        );
        assert.ok(calls > 1, "must have retried at least once before giving up");
    });

    it("does not retry a non-transient error: called once, no delay", async () => {
        const err = new Error("boom — some other failure entirely");
        let calls = 0;
        let sleeps = 0;
        await assert.rejects(
            () =>
                settlePairingRead(
                    () => {
                        calls++;
                        throw err;
                    },
                    { sleep: async () => void sleeps++ },
                ),
            (thrown: unknown) => thrown === err,
        );
        assert.equal(calls, 1, "a non-transient error must not be retried");
        assert.equal(sleeps, 0, "a non-transient error must not sleep");
    });

    it("returns the first successful read without ever sleeping", async () => {
        let calls = 0;
        let sleeps = 0;
        const result = await settlePairingRead(
            () => {
                calls++;
                return REPORT;
            },
            { sleep: async () => void sleeps++ },
        );
        assert.deepEqual(result, REPORT);
        assert.equal(calls, 1);
        assert.equal(sleeps, 0);
    });
});
