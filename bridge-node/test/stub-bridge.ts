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
} from "../src/protocol.js";

const here = dirname(fileURLToPath(import.meta.url));

export interface GoldenExchange {
    request: Record<string, unknown>;
    response: Record<string, unknown>;
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
    open_commissioning_window: GoldenExchange;
}

export const golden: GoldenFrames = JSON.parse(
    readFileSync(join(here, "fixtures", "e0-frames.json"), "utf8"),
) as GoldenFrames;

export class StubBridge implements BridgeFacade {
    commissioned = false;
    openWindowError?: Error;
    readonly openWindowCalls: number[] = [];

    getStatus(): StatusReport {
        return structuredClone(golden.get_status.response.result) as StatusReport;
    }

    getPairing(): PairingReport {
        const source = this.commissioned ? golden.get_pairing_commissioned : golden.get_pairing_uncommissioned;
        return structuredClone(source.response.result) as PairingReport;
    }

    async openCommissioningWindow(durationSeconds: number): Promise<CommissioningWindowResult> {
        this.openWindowCalls.push(durationSeconds);
        if (this.openWindowError !== undefined) {
            throw this.openWindowError;
        }
        return structuredClone(golden.open_commissioning_window.response.result) as CommissioningWindowResult;
    }
}
