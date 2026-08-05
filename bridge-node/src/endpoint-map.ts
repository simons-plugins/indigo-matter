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

import { copyFileSync, readFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";

import { describeError, type DriftEntry, RefuseReason } from "./protocol.js";
import { writeJsonAtomic } from "./storage.js";

/** What the PRD §7 refuse-to-start decision is taken from. */
export interface EndpointIdentityState {
    /** Does the Matter stack hold at least one fabric right now? */
    commissioned: boolean;
    /**
     * Why a *present* map is unusable, if it is.
     *
     * Deliberately the only map input. An **absent** map is not a fault — see
     * {@link refuseReasonFor} — so `present` has no say in the decision and is
     * not passed here; the caller uses it to choose between bootstrapping a
     * baseline and simply loading one.
     */
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
 * blocked by one. Two conditions remain fatal once a fabric has existed:
 *
 * * **a map that is there and cannot be read** — the numbers being served
 *   cannot be checked against a baseline we know exists, and silently
 *   re-recording them would bless whatever matter.js happens to hand out this
 *   boot as the truth over a file that says something else;
 * * **the witness but no fabrics** — PRD §7's "storage missing but previously
 *   commissioned". matter.js's storage has gone, and its own `Endpoint.id →
 *   number` allocation went with it, so every accessory is about to be
 *   re-created in every ecosystem the user paired.
 *
 * **A commissioned bridge with no map at all is NOT one of them, and the first
 * cut of E5 had that wrong.** Every bridge commissioned before E5 shipped is in
 * exactly that state — fabrics, endpoints, no `endpoint-map.json`, because the
 * file did not exist yet — so refusing would have taken every already-working
 * export offline on upgrade. And the refusal buys nothing even in principle:
 * **matter.js owns the numbers**, keyed on `Endpoint.id` in its own store, and
 * this map is only the independent witness of what they were. A missing witness
 * renumbers precisely nothing; it means we cannot yet *check*, which is what
 * bootstrapping a baseline fixes and what refusing does not. The one thing a
 * missing map must never do is silently paper over a *present* one, which is
 * why `mapProblem` still refuses.
 *
 * Returns the reason to refuse, or `undefined` to serve normally.
 */
export function refuseReasonFor(state: EndpointIdentityState): string | undefined {
    if (state.commissioned) {
        return state.mapProblem === undefined ? undefined : RefuseReason.mapUnreadable;
    }
    return state.commissionedAt === undefined ? undefined : RefuseReason.fabricStorageLost;
}

/** Sibling of `identity.json` in the bridge storage dir. */
export const ENDPOINT_MAP_FILE = "endpoint-map.json";

/** Schema version of the persisted file. Bump only on an incompatible change. */
export const ENDPOINT_MAP_VERSION = 1;

/** Stable keys so a repeated failure replaces its warning rather than stacking. */
const WARN_PERSIST = "endpoint-map-persist";
const WARN_DELETE = "endpoint-map-delete";

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
    /**
     * True when the in-memory map holds something the disk does not.
     *
     * Set by a failed {@link persist}, cleared by a successful one. It is what
     * stops {@link checked} claiming an all-clear over a baseline that exists
     * only in RAM: a `driftChecked: true` there asserts that the numbers were
     * verified against something durable, and the next restart would find no
     * such thing, adopt whatever matter.js reallocates as the new truth, and
     * report a permanently clean bridge across a real renumbering.
     */
    #dirty = false;
    /** Persistence failures worth putting in `StatusReport.warnings` (§4.3). */
    #warnings = new Map<string, string>();

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
        this.#dirty = false;
        this.#warnings.clear();
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
        return this.#checked && !this.#dirty;
    }

    /**
     * §4.3 `warnings` — the persistence failures a client has to be told about.
     *
     * The node's log is stdout, and in this milestone the node is started **by
     * hand**, so stdout is a terminal somebody closed. A map that cannot be
     * written is exactly the fault this file exists to make visible, and it
     * would otherwise be visible only there. `get_status` is polled, so this is
     * the channel that actually reaches a user.
     */
    get warnings(): string[] {
        return [...this.#warnings.values()];
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
        // `#dirty` is the retry: a write that failed last time leaves the map
        // owing the disk something even when this pass added nothing, and the
        // steady state (`added === 0`, clean) still costs no I/O. Without it a
        // full disk at the moment of the first check would never be revisited,
        // and `checked` — see the `#dirty` field — would never come true again.
        if (added > 0 || this.#dirty) {
            this.persist(added > 0 ? `recorded ${added} new endpoint number(s)` : "retried after a failed write");
        }
        this.#checked = true;
        return drift;
    }

    /**
     * Adopt a baseline for a bridge that is already commissioned but has no map
     * — the E5 upgrade path (PRD §7).
     *
     * Not a rebuild: there is nothing to discard. A pre-E5 install has fabrics,
     * endpoints and numbers that matter.js has been persisting all along, and
     * the only thing missing is our witness of them. Recording what is already
     * true is the migration; refusing to serve until a human confirms a
     * *rebuild* would take a working bridge offline to fix a file it never had.
     *
     * Returns whether the baseline reached disk.
     */
    seed(numbers: readonly LiveEndpointNumber[], why: string): boolean {
        this.#numbers = new Map(numbers.map(({ uniqueId, endpointNumber }) => [uniqueId, endpointNumber]));
        this.#problem = undefined;
        this.#checked = numbers.length > 0;
        return this.persist(`seeded with ${this.#numbers.size} endpoint number(s) — ${why}`);
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
    rebuild(live: readonly LiveEndpointNumber[]): boolean {
        // The unusable file is COPIED aside before it is overwritten. It is the
        // only surviving record of what the numbers used to be, and the reason
        // it is unusable may be a truncation or a bad byte that a human can
        // repair — whereas the rebuild is about to replace it with the numbers
        // that exist *now*, which is a different and irreversible answer.
        this.quarantineUnusable();
        if (live.length === 0) {
            this.log(
                "Rebuilding the endpoint map from ZERO live endpoints — the node refuses to " +
                    "create any while it is refusing to serve (§1.1 excludes attach), so this " +
                    "records an empty baseline and stops refusing; the numbers themselves are " +
                    "recorded by the reconcile that follows.",
            );
        }
        this.#numbers = new Map(live.map(({ uniqueId, endpointNumber }) => [uniqueId, endpointNumber]));
        this.#problem = undefined;
        const persisted = this.persist(`rebuilt from ${this.#numbers.size} live endpoint(s)`);
        this.#checked = persisted;
        return persisted;
    }

    /** Copy a present-but-unusable map to `<file>.corrupt` before it is lost. */
    private quarantineUnusable(): void {
        if (this.#problem === undefined) {
            return;
        }
        const file = join(this.storagePath, ENDPOINT_MAP_FILE);
        try {
            copyFileSync(file, `${file}.corrupt`);
            this.log(`Unusable endpoint map copied to ${file}.corrupt before rebuilding`);
        } catch (error) {
            this.log(`Could not copy the unusable endpoint map aside: ${describeError(error)}`);
        }
    }

    /**
     * §3.10 with `preserveEndpointNumbers: false` — delete the file and forget
     * everything. The "explicit rebuild" of PRD §7, for when the map itself is
     * what is corrupt.
     */
    discard(): boolean {
        this.#numbers = new Map();
        this.#problem = undefined;
        this.#checked = false;
        this.#dirty = false;
        try {
            const removed = deleteEndpointMap(this.storagePath);
            this.#present = false;
            this.#warnings.delete(WARN_DELETE);
            this.#warnings.delete(WARN_PERSIST);
            this.log(removed ? "Endpoint map discarded" : "No endpoint map to discard");
            return true;
        } catch (error) {
            // The in-memory map is already empty, so the node behaves as asked;
            // what survives is a stale file that the next start would load as a
            // baseline for numbers that no longer mean anything. Say so.
            const message = `Could not delete the endpoint map: ${describeError(error)}`;
            this.log(message);
            this.#warnings.set(WARN_DELETE, message);
            return false;
        }
    }

    /**
     * Write the current map, reporting whether it landed.
     *
     * Never throws — refusing to serve endpoints because a *witness* could not
     * be written would take down a working bridge over a disk problem. But it
     * is no longer silent to its callers either: an unwritten map means the
     * next start has no baseline, and the two callers that must not report
     * success over that (§3.11's rebuild and §4.3's `driftChecked`) both read
     * the answer.
     */
    private persist(why: string): boolean {
        const file: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: Object.fromEntries(this.#numbers),
        };
        try {
            writeJsonAtomic(join(this.storagePath, ENDPOINT_MAP_FILE), file);
            this.#present = true;
            this.#dirty = false;
            // A write that succeeded is the definitive answer about the file's
            // usability: whatever was wrong with the old one is gone with it.
            this.#problem = undefined;
            this.#warnings.delete(WARN_PERSIST);
            this.log(`Endpoint map ${why}`);
            return true;
        } catch (error) {
            this.#dirty = true;
            const message =
                `Could not write the endpoint map (${why}): ${describeError(error)}. ` +
                "Endpoint-number drift will not be detectable after a restart.";
            this.log(message);
            this.#warnings.set(WARN_PERSIST, message);
            return false;
        }
    }
}
