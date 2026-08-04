/**
 * The enhanced-commissioning-window state machine (§3.8).
 *
 * Extracted from {@link BridgeNode} so the bookkeeping — double-open refusal,
 * expiry, and which reason a `window_closed` event carries — is unit-testable
 * without a Matter stack. Timer and clock are injected for the same reason.
 */

import { describeError, ErrorCode, ProtocolError, type WindowClosedReason } from "./protocol.js";

/** Cancels a scheduled expiry. */
export type Cancel = () => void;

/** Schedules `callback` `ms` from now and returns its canceller. */
export type Scheduler = (callback: () => void, ms: number) => Cancel;

/** Real timers, unref'd: an open window must never keep the process alive. */
export const defaultScheduler: Scheduler = (callback, ms) => {
    const timer = setTimeout(callback, ms);
    timer.unref?.();
    return () => clearTimeout(timer);
};

export interface OpenWindow {
    expiresAt: Date;
    manualPairingCode: string;
    qrPairingCode: string;
}

export interface CommissioningWindowOptions {
    schedule?: Scheduler;
    now?: () => number;
    log?: (message: string) => void;
    /**
     * Ends the Matter-side window when our expiry timer fires first. Anything
     * it throws is caught and logged — a timer callback must never rethrow.
     */
    onExpire?: () => void;
}

export class CommissioningWindow {
    #open?: OpenWindow;
    #cancel?: Cancel;
    #listener?: (reason: WindowClosedReason) => void;
    readonly #schedule: Scheduler;
    readonly #now: () => number;
    readonly #log: (message: string) => void;
    readonly #onExpire: (() => void) | undefined;

    constructor(options: CommissioningWindowOptions = {}) {
        this.#schedule = options.schedule ?? defaultScheduler;
        this.#now = options.now ?? Date.now;
        this.#log = options.log ?? (() => {});
        this.#onExpire = options.onExpire;
    }

    /** Register the `window_closed` sink (§5). Last registration wins. */
    onClosed(listener: (reason: WindowClosedReason) => void): void {
        this.#listener = listener;
    }

    get isOpen(): boolean {
        return this.#open !== undefined;
    }

    /** The live window, or `undefined` when none is open. */
    get current(): Readonly<OpenWindow> | undefined {
        return this.#open;
    }

    /**
     * Refuse a second window *before* anything Matter-side is touched.
     *
     * matter.js 0.17.8's `DeviceCommissioner.allowEnhancedCommissioning` installs
     * the new PASE commissioner and only then throws on the double-open, so a
     * refusal that reaches the stack has already invalidated the code the user
     * is holding. Guarding here keeps the open window intact.
     */
    assertClosed(): void {
        if (this.#open !== undefined) {
            throw new ProtocolError(ErrorCode.commissioningWindowFailed, "A commissioning window is already open");
        }
    }

    /** Record a freshly opened window and arm its expiry. */
    open(durationSeconds: number, manualPairingCode: string, qrPairingCode: string): Date {
        this.assertClosed();
        const expiresAt = new Date(this.#now() + durationSeconds * 1000);
        this.#open = { expiresAt, manualPairingCode, qrPairingCode };
        this.#cancel = this.#schedule(() => this.#expire(), durationSeconds * 1000);
        return expiresAt;
    }

    /**
     * matter.js reports its window ended. Returns `false` when we had already
     * closed ours — which is what keeps expiry from announcing twice, since the
     * expiry path itself calls `endCommissioning()` and that fires this back.
     */
    noteEnded(): boolean {
        if (this.#open === undefined) {
            return false;
        }
        this.#discard();
        this.#log("Commissioning window closed by commissioner");
        this.#emit("commissioned");
        return true;
    }

    /** Shutdown: drop the window without announcing a close. */
    clear(): void {
        this.#discard();
    }

    #expire(): void {
        if (this.#open === undefined) {
            return;
        }
        this.#discard();
        this.#log("Commissioning window expired");
        this.#emit("expired");
        try {
            this.#onExpire?.();
        } catch (error) {
            this.#log(`Failed to end Matter commissioning after expiry: ${describeError(error)}`);
        }
    }

    #discard(): void {
        this.#cancel?.();
        this.#cancel = undefined;
        this.#open = undefined;
    }

    #emit(reason: WindowClosedReason): void {
        try {
            this.#listener?.(reason);
        } catch (error) {
            this.#log(`window_closed(${reason}) listener failed: ${describeError(error)}`);
        }
    }
}
