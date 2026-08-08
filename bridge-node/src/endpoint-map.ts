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
 * **One narrow, deliberate exception (issue #140).** An entry the node itself
 * marked {@link EndpointRecord.numberVoid} is not a drifted entry at all —
 * {@link EndpointMapStore.voidNumbers} is only ever called at the two points
 * where matter.js has just erased its OWN number allocation along with every
 * fabric (§3.10's `factory_reset preserveEndpointNumbers: true`, and the
 * last-fabric self-reset). matter.js is always the allocator; this map is only
 * ever its witness (see below), and with no fabric left there is no paired
 * ecosystem that could still be holding the old numbers to disagree with —
 * adoption there is unobservable outside this node. {@link
 * EndpointMapStore.check} adopts a void entry's live number silently instead
 * of reporting it. An entry that was never voided still drifts exactly as
 * documented above; only the reset that erased matter.js's own allocation
 * earns the adoption.
 *
 * The marker's lifetime is bounded by when its `UniqueID` next comes back
 * LIVE, not by the moment of the reset: only entries `check()` actually sees
 * get to adopt, so a device that stays offline through the first post-reset
 * reconcile simply keeps its VOID marker — still unadopted — until it next
 * exports, however much later that is. That is still safe even if fabrics
 * have been re-paired by then: the number it eventually adopts is the one
 * matter.js handed out fresh after THIS reset, not a stale one, and no
 * ecosystem ever saw the old number in between (it was never live to be
 * observed). The safety argument above just has to be read as "no fabric
 * survived the reset that erased the allocation", not "no fabric exists at
 * the moment of adoption".
 *
 * **It also carries what it takes to REBUILD an endpoint** (issue #141). Numbers
 * alone were enough while the file was only a witness, but a node that comes
 * online with an empty aggregator and waits for the plugin tells every paired
 * ecosystem that every accessory has gone — Apple re-adds them as new arrivals
 * and the user's room assignments are destroyed on every restart. So each entry
 * also records the `role` and `label` the endpoint was last created with, which
 * is exactly (and only) what `createEndpoint` needs; `node.ts` restores the set
 * from here before the stack goes online. `options` is deliberately NOT stored:
 * nothing in this node reads it (window-covering polarity is applied
 * plugin-side), so persisting it would be storing a field to satisfy a sentence
 * rather than a caller.
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

/**
 * Schema version of the persisted file.
 *
 * **2 since issue #141**, which added `role`/`label` to every entry so the
 * endpoint set can be rebuilt without the plugin. Version 1 files are still
 * read — see {@link ENDPOINT_MAP_VERSION_LEGACY} — because a v1 file holds the
 * one thing that can never be re-derived: the numbers paired ecosystems know.
 *
 * **Still 2 as of issue #140's `numberVoid` marker — not bumped to 3.** A
 * schema bump exists to protect an OLD build from a NEW file it cannot parse;
 * `numberVoid` needs no such protection, because every reader here (old and
 * new) reaches a field by name (`candidate.number`/`.role`/`.label`) rather
 * than by validating the object shape as a whole, so an old build reading a
 * file with an unknown extra key simply never looks at it — no throw, no
 * `readRecord` rejection, the entry loads exactly as it would without the
 * marker. There is nothing here for a version bump to guard.
 */
export const ENDPOINT_MAP_VERSION = 2;

/** The numbers-only shape shipped by E5. Still read; never written again. */
export const ENDPOINT_MAP_VERSION_LEGACY = 1;

/** Stable keys so a repeated failure replaces its warning rather than stacking. */
const WARN_PERSIST = "endpoint-map-persist";
const WARN_DELETE = "endpoint-map-delete";

/**
 * One persisted endpoint.
 *
 * `number` is the identity half and is always there. `role`/`label` are the
 * *restoration* half and are optional, because a v1 file has neither and
 * discarding a v1 file to get a tidier type would throw away the numbers every
 * paired ecosystem is keyed on. An entry without them is still a perfectly good
 * witness; it simply cannot be restored until the plugin next attaches and
 * fills them in.
 */
export interface EndpointRecord {
    number: number;
    role?: string;
    label?: string;
    /**
     * Set by {@link EndpointMapStore.voidNumbers} (issue #140): `number` is a
     * pre-reset value that {@link EndpointMapStore.check} must silently ADOPT
     * from the next live number it sees for this `UniqueID`, rather than
     * report as drift. Absent (not `false`) on every ordinary entry, so a plain
     * `JSON.stringify` of a non-void record is unchanged from before #140 —
     * `readRecord` only ever sets this to `true` and never anything else. An
     * old build reading a file that has it simply never looks at the key: it
     * destructures `number`/`role`/`label` off the parsed object by name, so
     * an unrecognised extra property is inert, not a crash — see the schema
     * version note below, which is why this stays version 2.
     */
    numberVoid?: true;
}

/** The on-disk shape (v2). */
export interface EndpointMapFile {
    version: number;
    /** `UniqueID → {number, role?, label?}`. */
    endpoints: Record<string, EndpointRecord>;
}

/** The v1 on-disk shape — `UniqueID → endpoint number`. Read-only, for tests and migration. */
export interface EndpointMapFileV1 {
    version: 1;
    endpoints: Record<string, number>;
}

/** One live endpoint, as the detector needs to see it. */
export interface LiveEndpointNumber {
    uniqueId: string;
    endpointNumber: number;
    /**
     * What it would take to rebuild this endpoint at the next start. Optional
     * so a caller that genuinely does not know (the E5 migration seed, which
     * has no live set at all) cannot be forced to invent them — an absent
     * value never erases a recorded one.
     */
    role?: string;
    label?: string;
}

/** An entry complete enough for `node.ts` to reconstruct before going online. */
export interface RestorableEndpoint {
    uniqueId: string;
    endpointNumber: number;
    role: string;
    label: string;
}

/** The outcome of reading the file: absent, usable, or present-but-broken. */
export interface EndpointMapLoad {
    endpoints: Map<string, EndpointRecord>;
    /** True when a file was there at all — absent is a first run, not a fault. */
    present: boolean;
    /** Set only when a file was present and could not be used. */
    problem?: string;
}

function isNonEmptyString(value: unknown): value is string {
    return typeof value === "string" && value.length > 0;
}

function isEndpointNumber(value: unknown): value is number {
    return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

/**
 * Read one entry in either schema, or `undefined` if it is not usable.
 *
 * A v2 entry with a bad `role`/`label` keeps its number and loses only the
 * fields that were wrong, for the same reason a v1 file is not discarded: the
 * number is the irreplaceable part, and refusing the whole file over a label
 * would turn a cosmetic corruption into a refuse-to-start.
 */
function readRecord(value: unknown): EndpointRecord | undefined {
    if (isEndpointNumber(value)) {
        return { number: value };
    }
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        return undefined;
    }
    const candidate = value as Partial<EndpointRecord>;
    if (!isEndpointNumber(candidate.number)) {
        return undefined;
    }
    const record: EndpointRecord = { number: candidate.number };
    if (isNonEmptyString(candidate.role)) {
        record.role = candidate.role;
    }
    if (isNonEmptyString(candidate.label)) {
        record.label = candidate.label;
    }
    // Tolerant, like role/label: anything other than a literal `true` is
    // simply not a void marker, rather than a reason to reject the entry.
    if (candidate.numberVoid === true) {
        record.numberVoid = true;
    }
    return record;
}

/**
 * Parse the `endpoints` object of either schema version, or `undefined` if the
 * file is not an endpoint map at all.
 */
function readEndpoints(value: unknown): Map<string, EndpointRecord> | undefined {
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
        return undefined;
    }
    const endpoints = new Map<string, EndpointRecord>();
    for (const [uniqueId, entry] of Object.entries(value)) {
        const record = readRecord(entry);
        if (record === undefined) {
            return undefined;
        }
        endpoints.set(uniqueId, record);
    }
    return endpoints;
}

function isKnownVersion(version: unknown): boolean {
    return version === ENDPOINT_MAP_VERSION || version === ENDPOINT_MAP_VERSION_LEGACY;
}

function isNotFound(error: unknown): boolean {
    return (error as NodeJS.ErrnoException | null)?.code === "ENOENT";
}

/**
 * Read `endpoint-map.json`. Never throws — the caller decides what a fault means.
 *
 * **A v1 file is migrated, never rejected.** Its entries are numbers-only, so
 * they load as records with no `role`/`label` and are simply not restorable
 * until the next attach records them; the numbers themselves are the identity
 * every paired ecosystem is keyed on and must survive the upgrade untouched.
 */
export function loadEndpointMap(storagePath: string): EndpointMapLoad {
    const file = join(storagePath, ENDPOINT_MAP_FILE);
    try {
        const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
        const version = (parsed as { version?: unknown } | null)?.version;
        const endpoints = isKnownVersion(version)
            ? readEndpoints((parsed as EndpointMapFile).endpoints)
            : undefined;
        if (endpoints === undefined) {
            return {
                endpoints: new Map(),
                present: true,
                problem:
                    `${file} is not a version ${ENDPOINT_MAP_VERSION} ` +
                    `(or legacy version ${ENDPOINT_MAP_VERSION_LEGACY}) endpoint map`,
            };
        }
        return { endpoints, present: true };
    } catch (error) {
        if (isNotFound(error)) {
            return { endpoints: new Map(), present: false };
        }
        return {
            endpoints: new Map(),
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

/** A fresh record from a live endpoint, carrying whatever it could tell us. */
function recordFor(entry: LiveEndpointNumber): EndpointRecord {
    const record: EndpointRecord = { number: entry.endpointNumber };
    if (entry.role !== undefined) {
        record.role = entry.role;
    }
    if (entry.label !== undefined) {
        record.label = entry.label;
    }
    return record;
}

/**
 * Refresh an existing record's restoration half from a live endpoint.
 *
 * An **absent** value never erases a recorded one: the seed path has no live
 * set at all, and reading its silence as "this endpoint has no role any more"
 * would strip a perfectly good entry of the very fields that make it
 * restorable. Returns whether anything changed, so a steady state still costs
 * no disk write.
 */
function noteRestorable(record: EndpointRecord, entry: LiveEndpointNumber): boolean {
    let changed = false;
    if (entry.role !== undefined && record.role !== entry.role) {
        record.role = entry.role;
        changed = true;
    }
    if (entry.label !== undefined && record.label !== entry.label) {
        record.label = entry.label;
        changed = true;
    }
    return changed;
}

/** Why {@link EndpointMapStore.check} is writing, for the log line. */
function persistReason(added: number, refreshed: number, adopted: number): string {
    const parts: string[] = [];
    if (added > 0) {
        parts.push(`recorded ${added} new endpoint number(s)`);
    }
    if (refreshed > 0) {
        parts.push(`refreshed ${refreshed} endpoint role/label(s)`);
    }
    if (adopted > 0) {
        parts.push(`adopted ${adopted} renumbered endpoint number(s)`);
    }
    return parts.length === 0 ? "retried after a failed write" : parts.join(" and ");
}

/**
 * The map plus the operations §3.10/§3.11 and the drift detector need.
 *
 * One instance per node, constructed at startup and {@link load}ed before the
 * Matter stack comes up, because whether the file is readable is what decides
 * if the node serves endpoints at all.
 */
export class EndpointMapStore {
    #endpoints = new Map<string, EndpointRecord>();
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
    /**
     * True once the file {@link load} found unusable has been copied aside.
     *
     * Both {@link rebuild} and {@link persist} quarantine before overwriting,
     * and without this the two would each take a copy of the same bytes — which
     * is worse than it sounds now that the copies are timestamped, because
     * every one of them is a file a human has to look at and decide about.
     */
    #quarantined = false;

    constructor(
        private readonly storagePath: string,
        private readonly log: (message: string) => void = () => {},
        /** Injectable so the timestamped quarantine name is testable. */
        private readonly now: () => Date = () => new Date(),
    ) {}

    /** Read the file into memory. Idempotent; the last read wins. */
    load(): EndpointMapLoad {
        const loaded = loadEndpointMap(this.storagePath);
        this.#endpoints = loaded.endpoints;
        this.#present = loaded.present;
        this.#problem = loaded.problem;
        this.#checked = false;
        this.#dirty = false;
        this.#quarantined = false;
        this.#warnings.clear();
        if (loaded.problem !== undefined) {
            this.log(`Endpoint map unusable: ${loaded.problem}`);
        } else if (!loaded.present) {
            this.log("No endpoint map yet; the first reconcile will record one");
        } else {
            const restorable = this.restorable().length;
            this.log(
                `Loaded endpoint map: ${loaded.endpoints.size} endpoint number(s), ` +
                    `${restorable} restorable at startup`,
            );
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
        return this.#endpoints.get(uniqueId)?.number;
    }

    get size(): number {
        return this.#endpoints.size;
    }

    /**
     * The entries complete enough to rebuild before the stack goes online
     * (issue #141).
     *
     * Entries missing `role` or `label` are silently absent rather than
     * reported as a fault: that is precisely a v1 file's normal state, and one
     * attach fills it in. Order is the map's own insertion order, which is the
     * order the numbers were first recorded — irrelevant to matter.js, which
     * keys each endpoint's number on its `Endpoint.id`, but stable, which makes
     * the log read the same way twice.
     *
     * **`numberVoid` is deliberately not part of this filter (issue #140).** A
     * voided number is still the best guess for `createEndpoint`'s
     * `Endpoint.id`-keyed restore — matter.js hands back whatever it currently
     * has for that id regardless of what this map believes, and the {@link
     * check} that follows `server.start()` is what reconciles the map to
     * whatever number came back. Excluding void entries here would only widen
     * the online-and-empty window issue #141 already closed, for no benefit.
     */
    restorable(): RestorableEndpoint[] {
        const restorable: RestorableEndpoint[] = [];
        for (const [uniqueId, record] of this.#endpoints) {
            if (record.role === undefined || record.label === undefined) {
                continue;
            }
            restorable.push({
                uniqueId,
                endpointNumber: record.number,
                role: record.role,
                label: record.label,
            });
        }
        return restorable;
    }

    /**
     * A device was **un-exported**: keep its number, drop what would rebuild it.
     *
     * The other half of {@link restorable}, and without it the restore is a bug
     * rather than a fix. `check` only ever *adds* and *refreshes*, so once an
     * entry has carried a `role`/`label` it carries them for ever — and a device
     * the user deliberately un-exported therefore stayed restorable, was
     * re-created as a child endpoint on every single boot, and was removed again
     * by the plugin's next attach seconds later. That is precisely the
     * appear-then-vanish churn issue #141 exists to eliminate, aimed at the
     * devices the user had already told us to stop exporting, and it regresses
     * XAC7's "un-exported accessories are gone".
     *
     * **The number stays.** §3.3 retains the allocation on purpose, so re-adding
     * the same device gets the same endpoint number back and paired ecosystems
     * see the accessory they already know rather than a new one. Deleting the
     * entry outright would throw that away to save three fields. Clearing only
     * the restoration half makes the entry non-restorable — {@link restorable}
     * filters on `role` **and** `label` — while leaving the identity intact, and
     * one re-export refills it through the ordinary {@link check}.
     *
     * **Only a caller that watched a specific endpoint go may call this.** An
     * empty live set is not evidence of anything: a node that has never attached
     * has none, and `seed([])` deliberately knows nothing. So this takes the
     * uniqueIds that were actually removed rather than inferring them from
     * whatever happens to be live, and `node.ts` derives them by diffing the
     * live set across one mutation. A factory reset is deliberately NOT such a
     * caller: `erase()` wipes matter.js's endpoints, but the user un-exported
     * nothing and the plugin's re-attach re-creates the same set.
     *
     * Returns how many entries actually lost something, so a no-op costs no
     * disk write.
     */
    forget(uniqueIds: readonly string[]): number {
        let forgotten = 0;
        for (const uniqueId of uniqueIds) {
            const record = this.#endpoints.get(uniqueId);
            if (record === undefined || (record.role === undefined && record.label === undefined)) {
                continue;
            }
            delete record.role;
            delete record.label;
            forgotten += 1;
        }
        if (forgotten > 0) {
            this.persist(
                `cleared the role/label of ${forgotten} un-exported endpoint(s), keeping their ` +
                    "numbers so a re-export returns the same accessory",
            );
        }
        return forgotten;
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
     *
     * **A `numberVoid` entry is adopted, not drift-checked (issue #140).** See
     * the class comment for why that is safe. The live number is taken as the
     * new baseline and the marker is cleared — even when the live number
     * happens to already equal the stale one, because the marker itself is
     * what has to stop being true, not just the mismatch it was guarding
     * against.
     */
    check(live: readonly LiveEndpointNumber[]): DriftEntry[] {
        const drift: DriftEntry[] = [];
        let added = 0;
        let refreshed = 0;
        let adopted = 0;
        for (const entry of live) {
            const record = this.#endpoints.get(entry.uniqueId);
            if (record === undefined) {
                this.#endpoints.set(entry.uniqueId, recordFor(entry));
                added += 1;
                continue;
            }
            // The restoration half is refreshed even when the number drifted:
            // a renamed accessory whose number also moved must still come back
            // under its current name, and role/label are independent facts from
            // the number. The NUMBER is never touched — see the class comment.
            if (noteRestorable(record, entry)) {
                refreshed += 1;
            }
            if (record.numberVoid) {
                record.number = entry.endpointNumber;
                delete record.numberVoid;
                adopted += 1;
                continue;
            }
            if (record.number !== entry.endpointNumber) {
                drift.push({ uniqueId: entry.uniqueId, expected: record.number, actual: entry.endpointNumber });
            }
        }
        if (adopted > 0) {
            this.log(
                `Adopted ${adopted} endpoint number(s) renumbered by the factory reset; matter.js's ` +
                    "allocation was erased with the fabrics, so no paired ecosystem holds the old " +
                    "numbers — this is the reset's own renumbering, not drift.",
            );
        }
        // `#dirty` is the retry: a write that failed last time leaves the map
        // owing the disk something even when this pass added nothing, and the
        // steady state (`added === 0`, clean) still costs no I/O. Without it a
        // full disk at the moment of the first check would never be revisited,
        // and `checked` — see the `#dirty` field — would never come true again.
        if (added > 0 || refreshed > 0 || adopted > 0 || this.#dirty) {
            this.persist(persistReason(added, refreshed, adopted));
        }
        // An empty comparison compared nothing, so it cannot make `driftChecked`
        // true — the same rule {@link seed} already applies. §4.3 says
        // `driftChecked: true` means "checked, nothing moved", and a `check([])`
        // (a reconcile that removed the last endpoint, a remove on a node that
        // never attached) would otherwise turn "there was nothing to look at"
        // into an all-clear. It only ever raises the flag: a real comparison
        // that has already happened is not un-done by a later empty one.
        if (live.length > 0) {
            this.#checked = true;
        }
        return drift;
    }

    /**
     * §3.10's preserving reset and the last-fabric self-reset (issue #140) —
     * mark every entry's number as no longer trustworthy, WITHOUT discarding
     * it, so the next {@link check} adopts whatever number each `UniqueID`
     * comes back with instead of reporting it as drift forever.
     *
     * **Why this is safe — see the class comment for the full argument.** In
     * one line: matter.js is always the allocator and this map only ever
     * witnesses it, and both call sites reach this with an empty fabric set,
     * so nothing paired can still be holding the numbers this call is about
     * to let move.
     *
     * Unlike {@link rebuild}, this keeps every entry (role/label survive
     * untouched — a void number does not make an endpoint any less
     * restorable, see {@link restorable}) and unlike {@link discard}, it
     * throws nothing away: a caller who wants "adopt whatever exists right
     * now" already has {@link rebuild}, and this is deliberately weaker than
     * that — it only stops treating the CURRENT numbers as ground truth.
     *
     * A no-op on an empty map: nothing to mark is not worth a write, and a
     * bridge that has never exported anything must not have this manufacture
     * a baseline out of nothing.
     *
     * Returns whether the voided baseline reached disk — `false` on the
     * empty-map no-op as well as on a failed write, since neither actually
     * persisted anything new. A caller that has to tell those two apart
     * (a no-op is not a failure worth warning about) should check {@link size}
     * before calling, the way both `node.ts` call sites now do.
     *
     * Also drops {@link checked}: a baseline the node itself just declared
     * VOID has not been verified against anything, and leaving the flag set
     * would have §4.3's `driftChecked: true` claim "checked, nothing moved"
     * over numbers that are, by construction, not yet trusted. The first
     * post-reset {@link check} against live entries earns it back.
     */
    voidNumbers(why: string): boolean {
        if (this.#endpoints.size === 0) {
            return false;
        }
        for (const record of this.#endpoints.values()) {
            record.numberVoid = true;
        }
        this.#checked = false;
        return this.persist(`voided every endpoint number — ${why}`);
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
     * **This is a full REPLACE, not a merge**, and the next caller has to mean
     * it: whatever is in memory is dropped, so seeding a partial list silently
     * discards the numbers of everything absent from it — the one thing in this
     * file that cannot be re-derived. Today's single caller
     * (`node.bootstrapEndpointMap`) passes `[]` against a map that is by
     * definition absent, so it drops nothing; the assertion below is what keeps
     * that true if a later caller has a map in hand. A caller that wants to add
     * to a baseline wants {@link check}, and one that wants to replace a
     * *disagreeing* baseline wants {@link rebuild}, which quarantines first.
     *
     * Returns whether the baseline reached disk.
     */
    seed(numbers: readonly LiveEndpointNumber[], why: string): boolean {
        if (this.#endpoints.size > 0 && numbers.length < this.#endpoints.size) {
            throw new Error(
                `refusing to seed ${numbers.length} endpoint number(s) over a baseline of ` +
                    `${this.#endpoints.size}: seed() replaces the whole map, so this would discard ` +
                    "endpoint numbers paired ecosystems are keyed on — use check() to add, or " +
                    "rebuild() to deliberately replace",
            );
        }
        this.#endpoints = new Map(numbers.map(entry => [entry.uniqueId, recordFor(entry)]));
        this.#problem = undefined;
        this.#checked = numbers.length > 0;
        return this.persist(`seeded with ${this.#endpoints.size} endpoint number(s) — ${why}`);
    }

    /**
     * §3.11 — throw the baseline away and adopt the live numbers as the truth.
     *
     * The recovery path out of `endpoint_map_invalid`, and the only operation
     * here that discards a mapping. It does not renumber anything in matter.js:
     * whatever duplication lost Matter storage implies has already happened by
     * the time a user is asked to confirm this, and what the rebuild does is
     * stop refusing and start telling the truth about the numbers that now
     * exist.
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
        this.#endpoints = new Map(live.map(entry => [entry.uniqueId, recordFor(entry)]));
        this.#problem = undefined;
        const persisted = this.persist(`rebuilt from ${this.#endpoints.size} live endpoint(s)`);
        // Same rule as `check`: a rebuild from zero live endpoints — which is
        // the *normal* rebuild, since §1.1 keeps `attach` out of the recovery
        // commands — has compared nothing, and the baseline it wrote is empty.
        // Claiming `driftChecked` there says an all-clear about a comparison
        // that never happened; the reconcile that follows is what records the
        // real numbers and earns the flag.
        this.#checked = persisted && live.length > 0;
        return persisted;
    }

    /**
     * Copy a present-but-unusable map to `<file>.corrupt-<stamp>` before it is
     * lost.
     *
     * Timestamped like `identity.json.unreadable-<stamp>`, and for the same
     * reason: a fixed name means the *second* thing to go wrong overwrites the
     * evidence of the first, and these bytes are the only surviving record of
     * what the endpoint numbers used to be.
     */
    private quarantineUnusable(): void {
        if (this.#problem === undefined || this.#quarantined) {
            return;
        }
        this.#quarantined = true;
        const file = join(this.storagePath, ENDPOINT_MAP_FILE);
        const target = `${file}.corrupt-${this.now().toISOString().replace(/[:.]/g, "-")}`;
        try {
            copyFileSync(file, target);
            this.log(`Unusable endpoint map copied to ${target} before it is overwritten`);
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
        this.#endpoints = new Map();
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
        // Not only `rebuild`'s job. `refuseReasonFor` deliberately does NOT
        // refuse a corrupt map on a bridge that has never been commissioned —
        // there is no accessory identity to protect — so the first `check` on
        // such a bridge walks straight in here and writes over the unusable
        // file. That file may be a truncation a human could have repaired, and
        // once it is gone the numbers it held cannot be recovered; the same
        // copy-first care `rebuild` takes costs nothing here.
        this.quarantineUnusable();
        const file: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: Object.fromEntries(this.#endpoints),
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
