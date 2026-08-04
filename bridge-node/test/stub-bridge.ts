/**
 * A {@link BridgeFacade} that returns the golden fixture payloads verbatim.
 *
 * The protocol tests exercise the real ws-server against this, so they need no
 * Matter stack, no network and no commissioned state.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type {
    BridgeFacade,
    CommissioningWindowResult,
    PairingReport,
    StatusReport,
    WindowClosedReason,
} from "../src/protocol.js";

const here = dirname(fileURLToPath(import.meta.url));

export interface GoldenExchange {
    request: Record<string, unknown>;
    response: Record<string, unknown>;
}

/** A §1 event frame as it sits in the golden file. */
export interface GoldenEvent {
    event: string;
    data: Record<string, unknown>;
}

export interface GoldenFrames {
    handshake: Record<string, unknown>;
    attach: GoldenExchange;
    attach_version_mismatch: GoldenExchange;
    not_attached: GoldenExchange;
    unknown_command: GoldenExchange;
    get_status: GoldenExchange;
    get_pairing_uncommissioned: GoldenExchange;
    get_pairing_commissioned: GoldenExchange;
    get_pairing_commissioned_window_open: GoldenExchange;
    open_commissioning_window: GoldenExchange;
    /** §1.1 failures the node really does answer with today. */
    open_window_malformed_args: GoldenExchange;
    open_window_failed: GoldenExchange;
    open_window_internal: GoldenExchange;
    window_closed_expired: GoldenEvent;
    window_closed_commissioned: GoldenEvent;
    /** §3.1-§3.11 exchanges awaiting node-side handlers — skipped by this suite. */
    pending: Record<string, GoldenExchange>;
    /**
     * §5 event frames the node does not emit yet — they exist so the plugin can
     * parse them, and `fixtures.test.ts` sweeps every one of them by name rather
     * than listing them, so a new event frame is covered the moment it is added.
     */
    [key: string]: unknown;
}

/**
 * §7: the golden frames live at the repo root (`tests/fixtures/bridge_protocol/`),
 * shared with the Python suite — a frame change that only updates one side fails
 * that side's tests. `npm test` copies that directory in beside the build, so
 * this path is the same whether the suite runs from source or from `.test-build`.
 */
export const golden: GoldenFrames = JSON.parse(
    readFileSync(join(here, "fixtures", "bridge_protocol", "frames.json"), "utf8"),
) as GoldenFrames;

export class StubBridge implements BridgeFacade {
    commissioned = false;
    /** Only meaningful while {@link commissioned}: selects the 3rd §3.7 state. */
    windowOpen = false;
    openWindowError?: Error;
    /**
     * Makes `openCommissioningWindow` genuinely slow, for the ordering test.
     * A single tick is not enough: `ws` delivers pipelined frames in separate
     * read events, so a one-tick handler finishes before the next frame even
     * arrives and the out-of-order bug never shows.
     */
    delayOpenWindowMs = 0;
    readonly openWindowCalls: number[] = [];
    #windowClosed?: (reason: WindowClosedReason) => void;

    getStatus(): StatusReport {
        return structuredClone(golden.get_status.response.result) as StatusReport;
    }

    getPairing(): PairingReport {
        const source = !this.commissioned
            ? golden.get_pairing_uncommissioned
            : this.windowOpen
              ? golden.get_pairing_commissioned_window_open
              : golden.get_pairing_commissioned;
        return structuredClone(source.response.result) as PairingReport;
    }

    async openCommissioningWindow(durationSeconds: number): Promise<CommissioningWindowResult> {
        this.openWindowCalls.push(durationSeconds);
        if (this.delayOpenWindowMs > 0) {
            await new Promise(resolve => setTimeout(resolve, this.delayOpenWindowMs));
        }
        if (this.openWindowError !== undefined) {
            throw this.openWindowError;
        }
        return structuredClone(golden.open_commissioning_window.response.result) as CommissioningWindowResult;
    }

    onWindowClosed(listener: (reason: WindowClosedReason) => void): void {
        this.#windowClosed = listener;
    }

    /** Stand in for the Matter stack closing the window. */
    emitWindowClosed(reason: WindowClosedReason): void {
        this.#windowClosed?.(reason);
    }
}
