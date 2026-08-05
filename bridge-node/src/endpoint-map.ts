/**
 * The persisted `UniqueID → endpoint number` map, and the drift detector built
 * on it (PRD §4.3, BRIDGE_PROTOCOL §4.3/§3.10/§3.11).
 *
 * **Why this file exists at all.** matter.js persists endpoint numbers itself,
 * keyed on the string `Endpoint.id`, and that is what actually holds identity
 * together across a restart. This map does not allocate anything: it is the
 * independent *witness* of what those numbers were last time, so that the one
 * failure that silently duplicates every accessory in every paired ecosystem —
 * matter.js's storage being lost, moved or reset underneath us — becomes a
 * statement in the log instead of a mystery in someone's Home app. That is
 * matterbridge's `checkEndpointNumbers()` pattern, and PRD §4.3 adopts it for
 * the same reason: it is the only way to make "is it us or the controller?"
 * falsifiable in the field.
 *
 * **It lives outside matter.js's storage context on purpose.** §3.10's factory
 * reset wipes that context; a witness stored inside it would be wiped by the
 * very event it exists to survive, so this file sits beside `identity.json` in
 * the bridge storage dir root and is written through the same atomic write.
 *
 * **Drift is reported, never repaired.** §4.3 is explicit, and the reasoning is
 * asymmetric: a wrong report costs a log line, while an auto-repair that pinned
 * the "right" number would hide the storage loss that caused it and make the
 * next occurrence invisible too. Nothing here mutates a drifted entry — the
 * baseline stays, so the drift keeps being reported until a human resolves it
 * with §3.11.
 *
 * Stdlib only — no matter.js import — which is what lets the whole detector be
 * tested without a Matter stack. Callers hand in `{uniqueId, endpointNumber}`
 * pairs; deriving `uniqueId` from an Indigo device id is `endpoints.ts`'s job.
 */

import { readFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";

import { describeError, type DriftEntry, RefuseReason } from "./protocol.js";
import { writeJsonAtomic } from "./storage.js";

/** What the PRD §7 refuse-to-start decision is taken from. */
export interface EndpointIdentityState {
    /** Does the Matter stack hold at least one fabric right now? */
    commissioned: boolean;
    /** Is there a usable persisted endpoint map? */
    mapPresent: boolean;
    /** Why a *present* map is unusable, if it is. */
    mapProblem?: string;
    /** `identity.commissionedAt` — the witness that we have ever been paired. */
    commissionedAt?: string;
}

/**
 * The PRD §7 refuse-to-start decision, as one pure function.
 *
 * Endpoint numbers only matter to somebody who is *paired*, and that asymmetry
 * is the whole rule: on a bridge nothing has ever commissioned, a missing or
 * unreadable map costs precisely nothing, so a fresh install must never be
 * blocked by one. Once a fabric has existed, both directions of the loss are
 * fatal to accessory identity and neither may be papered over:
 *
 * * **fabrics but no usable map** — the numbers being served cannot be checked
 *   against anything, and silently re-recording them would bless whatever
 *   matter.js happens to hand out this boot as the truth;
 * * **the witness but no fabrics** — PRD §7's "storage missing but previously
 *   commissioned". matter.js's storage has gone, and its own `Endpoint.id →
 *   number` allocation went with it, so every accessory is about to be
 *   re-created in every ecosystem the user paired.
 *
 * Returns the reason to refuse, or `undefined` to serve normally.
 */
export function refuseReasonFor(state: EndpointIdentityState): string | undefined {
    if (state.commissioned) {
        if (state.mapProblem !== undefined) {
            return RefuseReason.mapUnreadable;
        }
        return state.mapPresent ? undefined : RefuseReason.mapMissingWhileCommissioned;
    }
    return state.commissionedAt === undefined ? undefined : RefuseReason.fabricStorageLost;
}

/** Sibling of `identity.json` in the bridge storage dir. */
export const ENDPOINT_MAP_FILE = "endpoint-map.json";

/** Schema version of the persisted file. Bump only on an incompatible change. */
export const ENDPOINT_MAP_VERSION = 1;

/** The on-disk shape. */
export interface EndpointMapFile {
    version: number;
    /** `UniqueID → endpoint number`. */
    endpoints: Record<string, number>;
}

/** One live endpoint, as the detector needs to see it. */
export interface LiveEndpointNumber {
    uniqueId: string;
    endpointNumber: number;
}

/** The outcome of reading the file: absent, usable, or present-but-broken. */
export interface EndpointMapLoad {
    numbers: Map<string, number>;
    /** True when a file was there at all — absent is a first run, not a fault. */
    present: boolean;
    /** Set only when a file was present and could not be used. */
    problem?: string;
}

function isEndpointMapFile(value: unknown): value is EndpointMapFile {
    if (typeof value !== "object" || value === null) {
        return false;
    }
    const candidate = value as Partial<EndpointMapFile>;
    if (candidate.version !== ENDPOINT_MAP_VERSION) {
        return false;
    }
    const { endpoints } = candidate;
    if (typeof endpoints !== "object" || endpoints === null || Array.isArray(endpoints)) {
        return false;
    }
    return Object.values(endpoints).every(
        number => typeof number === "number" && Number.isInteger(number) && number >= 0,
    );
}

function isNotFound(error: unknown): boolean {
    return (error as NodeJS.ErrnoException | null)?.code === "ENOENT";
}

/** Read `endpoint-map.json`. Never throws — the caller decides what a fault means. */
export function loadEndpointMap(storagePath: string): EndpointMapLoad {
    const file = join(storagePath, ENDPOINT_MAP_FILE);
    try {
        const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
        if (!isEndpointMapFile(parsed)) {
            return {
                numbers: new Map(),
                present: true,
                problem: `${file} is not a version ${ENDPOINT_MAP_VERSION} endpoint map`,
            };
        }
        return { numbers: new Map(Object.entries(parsed.endpoints)), present: true };
    } catch (error) {
        if (isNotFound(error)) {
            return { numbers: new Map(), present: false };
        }
        return {
            numbers: new Map(),
            present: true,
            problem: `${file} is unreadable (${describeError(error)})`,
        };
    }
}

/** Delete `endpoint-map.json`. Returns whether a file was actually removed. */
export function deleteEndpointMap(storagePath: string): boolean {
    try {
        unlinkSync(join(storagePath, ENDPOINT_MAP_FILE));
        return true;
    } catch (error) {
        if (isNotFound(error)) {
            return false;
        }
        throw error;
    }
}

/**
 * The map plus the operations §3.10/§3.11 and the drift detector need.
 *
 * One instance per node, constructed at startup and {@link load}ed before the
 * Matter stack comes up, because whether the file is readable is what decides
 * if the node serves endpoints at all.
 */
export class EndpointMapStore {
    #numbers = new Map<string, number>();
    #present = false;
    #problem: string | undefined;
    #checked = false;

    constructor(
        private readonly storagePath: string,
        private readonly log: (message: string) => void = () => {},
    ) {}

    /** Read the file into memory. Idempotent; the last read wins. */
    load(): EndpointMapLoad {
        const loaded = loadEndpointMap(this.storagePath);
        this.#numbers = loaded.numbers;
        this.#present = loaded.present;
        this.#problem = loaded.problem;
        this.#checked = false;
        if (loaded.problem !== undefined) {
            this.log(`Endpoint map unusable: ${loaded.problem}`);
        } else if (!loaded.present) {
            this.log("No endpoint map yet; the first reconcile will record one");
        } else {
            this.log(`Loaded endpoint map: ${loaded.numbers.size} endpoint number(s)`);
        }
        return loaded;
    }

    /** True once a file has been read or written — i.e. there is a baseline. */
    get present(): boolean {
        return this.#present && this.#problem === undefined;
    }

    /** Why the present file cannot be used, if it cannot. */
    get problem(): string | undefined {
        return this.#problem;
    }

    /**
     * §4.3 `driftChecked` — whether an empty `drift` is an answer or an absence.
     *
     * False until {@link check} has actually run against a baseline. The two
     * readings of `drift: []` are opposites ("checked, nothing moved" versus
     * "there is nothing to check against yet"), and a client that cannot tell
     * them apart reads a fresh install's silence as an all-clear.
     */
    get checked(): boolean {
        return this.#checked;
    }

    /** The recorded number for a `UniqueID`, if there is one. */
    numberFor(uniqueId: string): number | undefined {
        return this.#numbers.get(uniqueId);
    }

    get size(): number {
        return this.#numbers.size;
    }

    /**
     * Compare the live endpoint numbers against the map (PRD §4.3).
     *
     * Endpoints the map has never seen are *recorded*; endpoints whose number
     * has moved are *reported* and left alone. Persisting only happens when
     * something was actually added, so a steady state costs no disk write per
     * reconcile.
     *
     * A device that is not currently live is not touched either way: §3.3
     * retains the allocation so re-adding the same device restores the same
     * number, and forgetting it here would throw away the very baseline that
     * makes a re-add verifiable.
     */
    check(live: readonly LiveEndpointNumber[]): DriftEntry[] {
        const drift: DriftEntry[] = [];
        let added = 0;
        for (const { uniqueId, endpointNumber } of live) {
            const expected = this.#numbers.get(uniqueId);
            if (expected === undefined) {
                this.#numbers.set(uniqueId, endpointNumber);
                added += 1;
                continue;
            }
            if (expected !== endpointNumber) {
                drift.push({ uniqueId, expected, actual: endpointNumber });
            }
        }
        if (added > 0) {
            this.persist(`recorded ${added} new endpoint number(s)`);
        }
        this.#checked = true;
        return drift;
    }

    /**
     * §3.11 — throw the baseline away and adopt the live numbers as the truth.
     *
     * The recovery path out of `endpoint_map_invalid`, and the only operation
     * here that discards a mapping. It does not renumber anything in matter.js:
     * whatever duplication a lost map implies has already happened by the time a
     * user is asked to confirm this, and what the rebuild does is stop refusing
     * and start telling the truth about the numbers that now exist.
     */
    rebuild(live: readonly LiveEndpointNumber[]): void {
        this.#numbers = new Map(live.map(({ uniqueId, endpointNumber }) => [uniqueId, endpointNumber]));
        this.#problem = undefined;
        this.persist(`rebuilt from ${this.#numbers.size} live endpoint(s)`);
        this.#checked = true;
    }

    /**
     * §3.10 with `preserveEndpointNumbers: false` — delete the file and forget
     * everything. The "explicit rebuild" of PRD §7, for when the map itself is
     * what is corrupt.
     */
    discard(): void {
        this.#numbers = new Map();
        this.#problem = undefined;
        this.#checked = false;
        try {
            const removed = deleteEndpointMap(this.storagePath);
            this.#present = false;
            this.log(removed ? "Endpoint map discarded" : "No endpoint map to discard");
        } catch (error) {
            // The in-memory map is already empty, so the node behaves as asked;
            // what survives is a stale file that the next start would load as a
            // baseline for numbers that no longer mean anything. Say so.
            this.log(`Could not delete the endpoint map: ${describeError(error)}`);
        }
    }

    /** Write the current map. Never throws — a failed write is loud, not fatal. */
    private persist(why: string): void {
        const file: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: Object.fromEntries(this.#numbers),
        };
        try {
            writeJsonAtomic(join(this.storagePath, ENDPOINT_MAP_FILE), file);
            this.#present = true;
            this.log(`Endpoint map ${why}`);
        } catch (error) {
            // Deliberately not fatal: refusing to serve endpoints because a
            // *witness* could not be written would take down a working bridge
            // over a disk problem. But an unwritten map means the next start
            // has no baseline, so this cannot be quiet either.
            this.log(
                `Could not write the endpoint map (${why}): ${describeError(error)}. ` +
                    "Endpoint-number drift will not be detectable after a restart.",
            );
        }
    }
}
