/**
 * The commissioning-window state machine (§3.8). Timer and clock are injected,
 * so every branch — including expiry — is exercised without waiting.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ProtocolError, type WindowClosedReason } from "../src/protocol.js";
import { CommissioningWindow } from "../src/window.js";

const MANUAL = "34970112332";
const QR = "MT:-24J0AFN00KA0648G00";

interface Harness {
    window: CommissioningWindow;
    /** Fire the scheduled expiry, if one is armed. */
    fire(): void;
    scheduled(): number | undefined;
    cancels: number;
    logs: string[];
    closed: WindowClosedReason[];
    expiries: number;
}

function harness(options: { onExpire?: () => void } = {}): Harness {
    let pending: { callback: () => void; ms: number } | undefined;
    const state = {
        cancels: 0,
        logs: [] as string[],
        closed: [] as WindowClosedReason[],
        expiries: 0,
    };
    const window = new CommissioningWindow({
        now: () => 1_000_000,
        log: message => state.logs.push(message),
        schedule: (callback, ms) => {
            pending = { callback, ms };
            return () => {
                pending = undefined;
                state.cancels++;
            };
        },
        onExpire: () => {
            state.expiries++;
            options.onExpire?.();
        },
    });
    window.onClosed(reason => state.closed.push(reason));
    return {
        window,
        fire: () => pending?.callback(),
        scheduled: () => pending?.ms,
        get cancels() {
            return state.cancels;
        },
        logs: state.logs,
        closed: state.closed,
        get expiries() {
            return state.expiries;
        },
    };
}

describe("CommissioningWindow", () => {
    it("starts closed and reports no window", () => {
        const h = harness();
        assert.equal(h.window.isOpen, false);
        assert.equal(h.window.current, undefined);
        h.window.assertClosed();
    });

    it("records the codes and an ISO-representable expiry", () => {
        const h = harness();
        const expiresAt = h.window.open(900, MANUAL, QR);
        assert.equal(h.window.isOpen, true);
        assert.equal(expiresAt.getTime(), 1_000_000 + 900_000);
        assert.equal(expiresAt.toISOString(), new Date(1_900_000).toISOString());
        assert.deepEqual(h.window.current, { expiresAt, manualPairingCode: MANUAL, qrPairingCode: QR });
        assert.equal(h.scheduled(), 900_000);
    });

    it("refuses a second window without disturbing the first", () => {
        const h = harness();
        const first = h.window.open(900, MANUAL, QR);
        assert.throws(
            () => h.window.open(900, "other", "other"),
            (error: unknown) => {
                assert.ok(error instanceof ProtocolError);
                assert.equal(error.code, "commissioning_window_failed");
                assert.equal(error.message, "A commissioning window is already open");
                return true;
            },
        );
        assert.throws(() => h.window.assertClosed(), ProtocolError);
        // The live window is untouched — the whole point of guarding first.
        assert.equal(h.window.current?.manualPairingCode, MANUAL);
        assert.equal(h.window.current?.expiresAt, first);
        assert.equal(h.closed.length, 0);
    });

    it("closes with reason \"expired\" when the timer fires, and ends Matter commissioning once", () => {
        const h = harness();
        h.window.open(900, MANUAL, QR);
        h.fire();
        assert.deepEqual(h.closed, ["expired"]);
        assert.equal(h.expiries, 1);
        assert.equal(h.window.isOpen, false);
        assert.equal(h.logs.filter(line => line.includes("expired")).length, 1);
    });

    it("does not announce a second close when Matter reports the end we caused", () => {
        // Our expiry calls endCommissioning(), which fires matter.js's own end
        // callback straight back at us — synchronously, in this stand-in.
        // Exactly one `window_closed`, exactly one log line.
        let window!: CommissioningWindow;
        const h = harness({ onExpire: () => window.noteEnded() });
        window = h.window;

        window.open(900, MANUAL, QR);
        h.fire();

        assert.deepEqual(h.closed, ["expired"]);
        assert.equal(h.logs.filter(line => line.startsWith("Commissioning window")).length, 1);
        // A later, unrelated end callback is still a no-op.
        assert.equal(window.noteEnded(), false);
        assert.deepEqual(h.closed, ["expired"]);
    });

    it("closes with reason \"commissioned\" and cancels the timer when a commissioner completes", () => {
        const h = harness();
        h.window.open(900, MANUAL, QR);
        assert.equal(h.window.noteEnded(), true);
        assert.deepEqual(h.closed, ["commissioned"]);
        assert.equal(h.cancels, 1);
        assert.equal(h.window.isOpen, false);
        // The cancelled timer cannot fire a late "expired" afterwards.
        h.fire();
        assert.deepEqual(h.closed, ["commissioned"]);
        assert.equal(h.expiries, 0);
    });

    it("noteEnded on a closed window is a no-op", () => {
        const h = harness();
        assert.equal(h.window.noteEnded(), false);
        assert.deepEqual(h.closed, []);
    });

    it("clear() drops the window silently, so shutdown emits nothing", () => {
        const h = harness();
        h.window.open(900, MANUAL, QR);
        h.window.clear();
        assert.equal(h.window.isOpen, false);
        assert.equal(h.cancels, 1);
        assert.deepEqual(h.closed, []);
        h.window.clear(); // idempotent
        assert.equal(h.cancels, 1);
        h.window.assertClosed();
    });

    it("reopens after a close", () => {
        const h = harness();
        h.window.open(900, MANUAL, QR);
        h.window.noteEnded();
        const second = h.window.open(180, "b", "c");
        assert.equal(h.scheduled(), 180_000);
        assert.equal(second.getTime(), 1_000_000 + 180_000);
    });

    it("catches a throwing window_closed listener and stays consistent", () => {
        const h = harness();
        h.window.onClosed(() => {
            throw new Error("listener exploded");
        });
        h.window.open(900, MANUAL, QR);
        assert.doesNotThrow(() => h.fire());
        assert.equal(h.window.isOpen, false);
        assert.ok(h.logs.some(line => line.includes("listener exploded")));
        // The Matter-side teardown still ran despite the listener failure.
        assert.equal(h.expiries, 1);
    });

    it("catches a throwing onExpire so the timer never rethrows", () => {
        const h = harness({
            onExpire: () => {
                throw new Error("env.get raced close()");
            },
        });
        h.window.open(900, MANUAL, QR);
        assert.doesNotThrow(() => h.fire());
        assert.deepEqual(h.closed, ["expired"]);
        assert.ok(h.logs.some(line => line.includes("env.get raced close()")));
    });
});
