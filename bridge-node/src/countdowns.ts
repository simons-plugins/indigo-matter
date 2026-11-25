/**
 * The timed-on countdown markers (issue #203), in their own small file beside
 * `endpoint-map.json`.
 *
 * **Not in the endpoint map, on purpose.** That file is the identity witness for
 * every pairing (ADR-0016): an unreadable one on a commissioned bridge means the
 * node refuses to serve, it is written with temp+rename and no fsync, and until
 * the markers existed it was rewritten only when an export changed. A marker is
 * written on every countdown start and every end, so keeping it there would put
 * the file everyone's accessory numbers depend on in the path of routine traffic.
 * Here, losing the file only loses recovery of a lost countdown — which fails
 * toward the light staying on — so a missing, unreadable or malformed file simply
 * means "no markers", with one warning.
 *
 * Keyed by the published identity (`indigo-<id>`), the same key the endpoint map
 * uses; the value is the countdown's wall-clock end in epoch milliseconds.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describeError } from "./protocol.js";
import { writeJsonAtomic } from "./storage.js";

export const COUNTDOWNS_FILE = "countdowns.json";
export const COUNTDOWNS_VERSION = 1;

interface CountdownsFile {
    version: number;
    countdowns: Record<string, number>;
}

export class CountdownStore {
    #deadlines = new Map<string, number>();
    #warned = false;

    constructor(
        private readonly storagePath: string,
        private readonly log: (message: string) => void = () => {},
    ) {}

    /** Read the file. Never throws: anything but a usable file is "no markers". */
    load(): void {
        this.#deadlines = new Map();
        const file = join(this.storagePath, COUNTDOWNS_FILE);
        let raw: string;
        try {
            raw = readFileSync(file, "utf8");
        } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== "ENOENT") {
                this.warn(`${file} could not be read (${describeError(error)})`);
            }
            return;
        }
        try {
            const parsed = JSON.parse(raw) as Partial<CountdownsFile> | null;
            const entries = parsed?.countdowns;
            if (typeof entries !== "object" || entries === null || Array.isArray(entries)) {
                throw new Error("no countdowns object");
            }
            for (const [uniqueId, deadline] of Object.entries(entries)) {
                if (typeof deadline === "number" && Number.isFinite(deadline) && deadline > 0) {
                    this.#deadlines.set(uniqueId, deadline);
                } else {
                    this.warn(`${file} has an unusable deadline for ${uniqueId}; ignored`);
                }
            }
        } catch (error) {
            this.#deadlines = new Map();
            this.warn(`${file} is not usable (${describeError(error)})`);
        }
    }

    /** Drop markers for identities the endpoint map does not know — their endpoint is gone. */
    prune(known: ReadonlySet<string>): void {
        let removed = 0;
        for (const uniqueId of [...this.#deadlines.keys()]) {
            if (!known.has(uniqueId)) {
                this.#deadlines.delete(uniqueId);
                removed += 1;
            }
        }
        if (removed > 0) {
            this.log(`Dropped ${removed} countdown marker(s) for endpoints no longer in the endpoint map`);
            this.persist();
        }
    }

    /** A factory reset wipes the endpoints' state; no countdown survives it. */
    clearAll(): void {
        if (this.#deadlines.size > 0) {
            this.#deadlines.clear();
            this.persist();
        }
    }

    countdownDeadline(uniqueId: string): number | undefined {
        return this.#deadlines.get(uniqueId);
    }

    setCountdown(uniqueId: string, deadlineMs: number): boolean {
        if (!Number.isFinite(deadlineMs) || deadlineMs <= 0) {
            return false;
        }
        if (this.#deadlines.get(uniqueId) !== deadlineMs) {
            this.#deadlines.set(uniqueId, deadlineMs);
            this.persist();
        }
        return true;
    }

    clearCountdown(uniqueId: string): void {
        if (this.#deadlines.delete(uniqueId)) {
            this.persist();
        }
    }

    private warn(message: string): void {
        // Once: a file that stays broken is rewritten (and so repaired) by the next marker.
        if (!this.#warned) {
            this.#warned = true;
            this.log(`Countdown markers: ${message}. Recovery of a countdown lost across a restart is off until it is rewritten.`);
        }
    }

    /** Never throws: a marker that cannot be written costs only recovery of that countdown. */
    private persist(): void {
        const file: CountdownsFile = {
            version: COUNTDOWNS_VERSION,
            countdowns: Object.fromEntries(this.#deadlines),
        };
        try {
            writeJsonAtomic(join(this.storagePath, COUNTDOWNS_FILE), file);
        } catch (error) {
            this.log(`Could not write the countdown markers: ${describeError(error)}`);
        }
    }
}
