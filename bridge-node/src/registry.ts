/**
 * The live bridged-endpoint set: create, update, remove, and the §3.1 reconcile.
 *
 * Split out of `node.ts` so that module stays what it is — the Matter *node*
 * (identity, commissioning, fabrics) — while this one owns the *children*.
 * Both are matter.js-coupled; the protocol layer reaches them only through
 * `BridgeFacade`.
 */

import type { Endpoint } from "@matter/main";

import {
    applyCtBoundsUpdate,
    applyLabel,
    applyReachable,
    applyStates,
    clearLeftoverCountdown,
    type BridgedIdentity,
    createEndpoint,
    isSupportedRole,
    readCountdown,
    UNSUPPORTED_ROLE_DETAILS,
    watchCommands,
    zeroStaleOnTime,
    type CountdownHooks,
} from "./endpoints.js";
import {
    COUNTDOWN_LOST_REASON,
    type CommandEventData,
    STATE_NOT_APPLIED_PHRASE,
    describeError,
    describeErrorWithStack,
    type EndpointSpec,
    type EndpointSummary,
    ErrorCode,
    ProtocolError,
    type RemoveResult,
    type RoleValue,
    supersedes,
    type UpsertResult,
} from "./protocol.js";
import { type LiveComposition, planReconcile } from "./reconcile.js";

/**
 * PRD §5.3: bulk removals are paced so controllers see one subscription update
 * each rather than a burst they may coalesce or drop.
 */
export const REMOVAL_PACING_MS = 100;

/** A countdown cannot exceed 0xFFFE tenths of a second (OnWithTimedOff's `OnTime`). */
const MAX_COUNTDOWN_MS = 0xfffe * 100;
/** How far a tick-by-tick `onTime` may sit from the recorded deadline before it is a real change. */
const TICK_TOLERANCE_MS = 500;

interface LiveEndpoint {
    endpoint: Endpoint;
    role: RoleValue;
    /**
     * The label this endpoint currently publishes. Held here rather than read
     * back off the cluster because it is persisted to `endpoint-map.json` on
     * every drift check (issue #141), and that must not cost a matter.js state
     * read per endpoint per reconcile.
     */
    label: string;
    /**
     * Whether this LIVE endpoint was built with PowerSource (issue #220). Set
     * once at {@link EndpointRegistry.create} from `spec.battery` and never
     * flipped false afterwards — the cluster set is monotonic (§4.1) — so this
     * mirrors what the Matter tree actually holds, not the most recent spec.
     */
    battery: boolean;
    /**
     * Issue #240 — the accessory identity this live endpoint was built with
     * (`Endpoint.id`/`UniqueID`/`SerialNumber`). Held here rather than
     * re-derived because a role-changed or migrated identity is no longer a
     * pure function of `indigoDeviceId` — see {@link EndpointSpec.publishedAs}.
     */
    publishedAs: string;
    /** Reassigned when a failed close forces the listeners to be restored. */
    unwatch: () => void;
}

/** What `endpoint-map.json` needs to know about one live endpoint (issue #141). */
export interface EndpointIdentity {
    indigoDeviceId: number;
    endpointNumber: number;
    role: RoleValue;
    label: string;
    /** Issue #220 — surfaced so `endpoint-map.json` can record it add-only. */
    battery: boolean;
    /** Issues #219/#240 — the accessory identity this endpoint is live under. */
    publishedAs: string;
}

/**
 * #203 — where the registry keeps each endpoint's timed-on countdown marker,
 * keyed on the published identity. {@link EndpointMapStore} is the real one (the
 * marker rides the endpoint's `endpoint-map.json` record, so it is written
 * atomically in the map's own style and dies with the record); the registry
 * falls back to an in-memory one when none is given.
 */
export interface CountdownMarkers {
    countdownDeadline(uniqueId: string): number | undefined;
    /** False when there is nowhere to record it (no map record yet). */
    setCountdown(uniqueId: string, deadlineMs: number): boolean;
    clearCountdown(uniqueId: string): void;
}

class MemoryCountdownMarkers implements CountdownMarkers {
    readonly #deadlines = new Map<string, number>();
    countdownDeadline(uniqueId: string): number | undefined {
        return this.#deadlines.get(uniqueId);
    }
    setCountdown(uniqueId: string, deadlineMs: number): boolean {
        this.#deadlines.set(uniqueId, deadlineMs);
        return true;
    }
    clearCountdown(uniqueId: string): void {
        this.#deadlines.delete(uniqueId);
    }
}

export interface EndpointRegistryOptions {
    /** #203 — durable countdown markers. Defaults to an in-memory set (tests). */
    countdowns?: CountdownMarkers;
    /** #203 — wall clock, injectable for tests. Defaults to `Date.now`. */
    now?: () => number;
    /**
     * #203 — raise (`message`) or withdraw (`undefined`) a §4.3 warning, keyed.
     * The node's own log is stdout under launchd, so a recovery that failed has
     * to reach the plugin through the `warnings` channel to be seen at all.
     */
    warn?: (key: string, message: string | undefined) => void;
    /** The aggregator every bridged child is added to. */
    aggregator: Endpoint;
    /** Sink for §5 `command` events. */
    emit: (data: CommandEventData) => void;
    log?: (message: string) => void;
    /**
     * The identity fields every child's Bridged Device Basic Information carries
     * — vendor, product, hardware and software versions. Bridge-wide, not
     * per-device: Indigo does not know who made a relay.
     */
    bridgeIdentity: BridgedIdentity;
    /** Injectable so the pacing test does not take a real 100ms per removal. */
    removalPacingMs?: number;
    /**
     * Called once per add/remove batch so the caller can bump
     * `ConfigurationVersion`. Optional because the pure-registry tests have no
     * root node to bump.
     */
    onConfigurationChange?: () => Promise<void>;
}

export class EndpointRegistry {
    readonly #live = new Map<number, LiveEndpoint>();
    readonly #log: (message: string) => void;
    readonly #pacingMs: number;
    readonly #markers: CountdownMarkers;
    readonly #now: () => number;
    /** #203 — one plain timer per published identity whose lost countdown ends in the future. */
    readonly #pendingOffs = new Map<string, NodeJS.Timeout>();
    /** Identities whose off was sent this process and is not yet confirmed — a second send is a retry. */
    readonly #sentOffs = new Set<string>();
    /** Tail of the mutation chain — see {@link serialize}. */
    #queue: Promise<void> = Promise.resolve();
    /**
     * Names of the mutations on the chain, head first. Pushed *synchronously*
     * on the way in, because the whole point is to notice a second caller that
     * arrived before the first one's continuation has even been scheduled —
     * a flag set inside the queued callback would still read "idle" then.
     */
    readonly #waiting: string[] = [];

    constructor(private readonly options: EndpointRegistryOptions) {
        this.#log = options.log ?? (() => {});
        this.#pacingMs = options.removalPacingMs ?? REMOVAL_PACING_MS;
        this.#markers = options.countdowns ?? new MemoryCountdownMarkers();
        this.#now = options.now ?? Date.now;
    }

    /**
     * Run one mutation with the registry to itself.
     *
     * Every mutating entry point goes through here, because two of them running
     * at once share both `#live` and the aggregator and neither is transactional.
     * The case that forced it is not exotic: a plugin crash leaves a half-open
     * socket, launchd restarts the plugin, and the new process `attach`es on a
     * *new* socket while the incumbent's reconcile is still mid-flight — and a
     * reconcile that paces bulk removals (PRD §5.3, ~100ms each) holds the
     * registry for seconds. Without this, the second reconcile plans its diff
     * from a snapshot the first is still mutating underneath it, and the two
     * interleave into a live set that matches neither request.
     *
     * A plain promise chain rather than a lock class: there is exactly one
     * critical section (the whole registry), so ordering is the only thing to
     * decide, and FIFO is what §1's in-receipt-order guarantee already promises
     * the plugin.
     */
    private serialize<T>(what: string, run: () => Promise<T>): Promise<T> {
        const ahead = this.#waiting[0];
        if (ahead !== undefined) {
            // Loud on purpose: this is the plugin-crash-restart window, and it
            // is the only explanation for an attach that appears to hang.
            this.#log(`${what} is waiting for an unfinished ${ahead} to release the registry`);
        }
        this.#waiting.push(what);
        const result = this.#queue.then(async () => {
            try {
                return await run();
            } finally {
                this.#waiting.shift();
            }
        });
        // The chain must never be left rejected, or one refusal would poison
        // every command behind it. The caller still gets the rejection.
        this.#queue = result.then(
            () => undefined,
            () => undefined,
        );
        return result;
    }

    /** §4.3 `StatusReport.endpoints`, in device-id order for a stable readout. */
    summaries(): EndpointSummary[] {
        return [...this.#live.entries()]
            .map(([indigoDeviceId, live]) => ({
                indigoDeviceId,
                endpointNumber: Number(live.endpoint.number),
                role: live.role,
                publishedAs: live.publishedAs,
            }))
            .sort((a, b) => a.indigoDeviceId - b.indigoDeviceId);
    }

    get size(): number {
        return this.#live.size;
    }

    /**
     * Everything `endpoint-map.json` records about the live set (issue #141).
     *
     * Separate from {@link summaries} on purpose: that is the §4.3 wire shape
     * and adding `label` to it would change the protocol for the benefit of a
     * local file. Same order, for a stable readout.
     */
    identities(): EndpointIdentity[] {
        return [...this.#live.entries()]
            .map(([indigoDeviceId, live]) => ({
                indigoDeviceId,
                endpointNumber: Number(live.endpoint.number),
                role: live.role,
                label: live.label,
                battery: live.battery,
                publishedAs: live.publishedAs,
            }))
            .sort((a, b) => a.indigoDeviceId - b.indigoDeviceId);
    }

    /**
     * Issue #141 — rebuild the last-known endpoint set before the node goes
     * online, from the persisted map rather than from the plugin.
     *
     * **The node must never be online-and-empty.** A bridge that comes up with
     * a childless aggregator and waits for the plugin tells every paired
     * ecosystem that every accessory has gone; Apple re-adds them as new
     * arrivals seconds later, in the bridge's own room, with metadata the user
     * can no longer edit. Restoring here closes that window: the endpoints are
     * present from the first moment a controller can read `PartsList`.
     *
     * Everything here is the ordinary {@link create} path — same
     * `createEndpoint`, same `Endpoint.id`, so matter.js hands back the same
     * persisted number. What is different is only that the specs come from disk
     * and carry `reachable: false`, because nothing has confirmed any state
     * yet.
     *
     * **A failure restores what it can and says what it could not.** These
     * specs come from a file, and one unusable entry must not cost the user
     * every other accessory: serving four of five and naming the fifth is
     * strictly better than serving none, which is the bug being fixed.
     */
    async restore(specs: readonly EndpointSpec[]): Promise<number> {
        return this.serialize("restore", async () => {
            let restored = 0;
            for (const spec of specs) {
                try {
                    // `restoring`: this is the SAME endpoint coming back, so a
                    // countdown marker it carries must survive (#203).
                    await this.create(spec, true);
                    restored += 1;
                } catch (error) {
                    this.#log(
                        `Could not restore endpoint ${spec.indigoDeviceId} (${spec.role}) from the ` +
                            `endpoint map; it will be created when the plugin attaches: ` +
                            describeErrorWithStack(error),
                    );
                }
            }
            return restored;
        });
    }

    /**
     * The live role+battery of every published identity — what the reconcile
     * planner diffs (issues #219/#240).
     *
     * Keyed on `publishedAs`, not `indigoDeviceId`: `#live` itself stays keyed
     * on device id below (the wire keys every command that way, and one device
     * drives at most one live endpoint), so this is a per-call reprojection
     * rather than a second index to keep in sync.
     */
    liveComposition(): Map<string, LiveComposition> {
        return new Map(
            [...this.#live.entries()].map(([indigoDeviceId, live]) => [
                live.publishedAs,
                { indigoDeviceId, role: live.role, battery: live.battery },
            ]),
        );
    }

    /**
     * Resolve a published identity back to the device id driving it (issues
     * #219/#240) — `plan.remove` entries arrive as `publishedAs`, but
     * `closeOne`/`removeMany` operate on `#live`, which stays keyed on device
     * id. A linear scan: the live set is a handful of exports, not a hot path.
     */
    private deviceIdFor(publishedAs: string): number | undefined {
        for (const [indigoDeviceId, live] of this.#live) {
            if (live.publishedAs === publishedAs) {
                return indigoDeviceId;
            }
        }
        return undefined;
    }

    /**
     * §3.1: full reconcile against the desired set.
     *
     * Refusals that can be decided from the plan alone — an unsupported role,
     * `mass_removal_refused` — are thrown before anything is touched: those are
     * gates, and a gate that half-applies is not a gate.
     *
     * Once mutation starts there is no such promise. matter.js gives us no
     * transaction across several `add`/`close` calls, so a failure part-way
     * through leaves a live set that is neither the old one nor the requested
     * one. What we owe the plugin then is an honest account of it, which is the
     * `reconcile aborted after N/M` line below plus the `internal` refusal —
     * **the plugin should follow a failed `attach` with `get_status`** rather
     * than assume either set.
     */
    async reconcile(desired: readonly EndpointSpec[], replaceAll: boolean): Promise<void> {
        return this.serialize("reconcile", () => this.reconcileNow(desired, replaceAll));
    }

    /**
     * #203 — end every countdown a node restart lost. Called by the attach
     * handler AFTER the attach response is on the wire (never inside
     * {@link reconcile}), for two reasons: a command emitted with nobody
     * attached is dropped, and one emitted during the attach request reaches
     * a plugin that has not finished attaching, so Indigo's confirming state
     * push would be discarded. Still serialised with every other mutation.
     *
     * **Stranded means a countdown was started AND lost — read from a marker,
     * not inferred.** matter.js persists `onTime` on every tick but its timer
     * is in-memory, so after a restart the lamp reads on with a countdown
     * nothing will finish. `onTime > 0` alone is NOT evidence of that: a
     * controller may write the attribute, and a write never starts a timer.
     * So the sweep acts only on an endpoint that has a countdown marker (a
     * wall-clock deadline written when an `OnWithTimedOff` really started the
     * timer), reads on, and has no timer running:
     *
     * * deadline passed: send the off now;
     * * deadline ahead: send it when it comes (a plain timer; matter.js's own
     *   countdown is deliberately not restarted);
     * * nothing is zeroed and no marker is cleared by the sweep itself. They clear when Indigo
     *   CONFIRMS the off (`applyIndigoOnOff(false)` → `super.off()`) — or, as before, when the
     *   countdown ends naturally or an ecosystem turns it off (`off()`), which with the plugin
     *   detached drops that off and leaves the device on (fails toward on) — so
     *   an off the plugin never executed stays stranded and goes out again at
     *   the next attach.
     *
     * A plugin-only reattach while the timer still runs is skipped by the
     * timer check; an attach that says "off" has already ended the countdown.
     *
     * **Known limitation, accepted:** if the user turned the device off and on
     * again in Indigo while the node was down, the attach reads "on" with the
     * marker still set, and the off is sent to a lamp deliberately turned on.
     *
     * Best-effort per endpoint: a failure goes on the §4.3 warnings channel and
     * the node log, the sweep moves on, and the next attach retries.
     */
    async sweepCountdowns(): Promise<void> {
        return this.serialize("countdown sweep", async () => {
            for (const [indigoDeviceId, live] of [...this.#live]) {
                await this.sweepOne(indigoDeviceId, live, false);
            }
        });
    }

    /** One endpoint's share of {@link sweepCountdowns}; `due` is the deadline timer firing (no clock check). */
    private async sweepOne(indigoDeviceId: number, live: LiveEndpoint, due: boolean): Promise<void> {
        const key = `countdown:${live.publishedAs}`;
        try {
            const reading = await readCountdown(live.endpoint);
            if (reading === undefined) {
                return;
            }
            let deadline = this.#markers.countdownDeadline(live.publishedAs);
            if (deadline !== undefined && (!Number.isFinite(deadline) || deadline - this.#now() > MAX_COUNTDOWN_MS)) {
                // No countdown lasts this long, so the file or the clock that wrote it is wrong. Acting on it
                // would overflow setTimeout (~24.8 days) into an immediate off, so it is discarded instead.
                this.#log(
                    `Endpoint ${indigoDeviceId}: a recorded countdown deadline ${deadline} is implausible (more than ` +
                        `${MAX_COUNTDOWN_MS / 1000}s ahead) — discarded, nothing is turned off (#203)`,
                );
                this.clearMarker(live.publishedAs);
                deadline = undefined;
            }
            if (!reading.onOff) {
                if (reading.timerRunning) {
                    return; // an ecosystem countdown Indigo has not confirmed yet: leave it alone
                }
                const cleared = await clearLeftoverCountdown(live.endpoint);
                if (cleared !== undefined) {
                    this.#log(
                        `Endpoint ${indigoDeviceId}: cleared a leftover onTime of ${cleared / 10}s on an endpoint ` +
                            "that reads off — no countdown was running (#203)",
                    );
                }
                if (deadline !== undefined) {
                    this.clearMarker(live.publishedAs);
                }
            } else if (deadline !== undefined && !reading.timerRunning) {
                const remaining = due ? 0 : deadline - this.#now();
                if (remaining > 0) {
                    this.scheduleOff(live.publishedAs, remaining);
                    this.#log(
                        `Endpoint ${indigoDeviceId}: a timed-on countdown was lost across a restart with ` +
                            `${Math.round(remaining / 100) / 10}s still to run — the device will be turned off then (#203)`,
                    );
                } else {
                    this.cancelPendingOff(live.publishedAs);
                    const retry = this.#sentOffs.has(live.publishedAs);
                    this.#sentOffs.add(live.publishedAs);
                    this.#log(
                        `Endpoint ${indigoDeviceId}: a timed-on countdown was lost across a restart — ` +
                            `${retry ? "retrying the off Indigo has not confirmed" : "telling Indigo to turn the device off"} ` +
                            "(#203); it stays stranded, and is re-sent at the next attach, until Indigo confirms",
                    );
                    this.options.emit({
                        indigoDeviceId,
                        command: "onOff",
                        args: { value: false, reason: COUNTDOWN_LOST_REASON },
                    });
                }
            }
            this.options.warn?.(key, undefined);
        } catch (error) {
            const message =
                `device ${indigoDeviceId}: could not end its timed-on countdown lost across a bridge node ` +
                `restart (${describeError(error)}); it will be retried at the next attach`;
            this.#log(`Endpoint ${indigoDeviceId}: ${message} (#203)`);
            this.options.warn?.(key, message);
        }
    }

    private scheduleOff(publishedAs: string, remainingMs: number): void {
        this.cancelPendingOff(publishedAs);
        const timer = setTimeout(() => {
            this.#pendingOffs.delete(publishedAs);
            this.serialize("countdown deadline", async () => {
                const indigoDeviceId = this.deviceIdFor(publishedAs);
                const live = indigoDeviceId === undefined ? undefined : this.#live.get(indigoDeviceId);
                if (indigoDeviceId !== undefined && live !== undefined) {
                    await this.sweepOne(indigoDeviceId, live, true);
                }
            }).catch(error => this.#log(`countdown deadline for ${publishedAs} failed: ${describeError(error)}`));
        }, remainingMs);
        timer.unref?.();
        this.#pendingOffs.set(publishedAs, timer);
    }

    private cancelPendingOff(publishedAs: string): void {
        const timer = this.#pendingOffs.get(publishedAs);
        if (timer !== undefined) {
            clearTimeout(timer);
            this.#pendingOffs.delete(publishedAs);
        }
    }

    private clearMarker(publishedAs: string): void {
        this.#sentOffs.delete(publishedAs);
        this.#markers.clearCountdown(publishedAs);
        this.cancelPendingOff(publishedAs);
    }

    /** What a behaviour calls to record or end its countdown marker (#203). */
    private countdownHooksFor(publishedAs: string, indigoDeviceId: number): CountdownHooks {
        return {
            mark: remainingMs => {
                if (!this.#markers.setCountdown(publishedAs, this.#now() + remainingMs)) {
                    this.#log(
                        `Endpoint ${indigoDeviceId}: a timed-on countdown started but there is no endpoint-map ` +
                            "record to note it in yet, so a node restart could not recover it (#203)",
                    );
                }
            },
            clear: () => this.clearMarker(publishedAs),
            sync: remainingMs => {
                const current = this.#markers.countdownDeadline(publishedAs);
                if (current === undefined) {
                    return; // only follows a marker that exists; starting one is `mark`'s job
                }
                if (remainingMs === undefined) {
                    this.clearMarker(publishedAs);
                    return;
                }
                const target = this.#now() + remainingMs;
                if (Math.abs(target - current) > TICK_TOLERANCE_MS) {
                    this.#markers.setCountdown(publishedAs, target);
                    this.cancelPendingOff(publishedAs);
                }
            },
        };
    }

    private async reconcileNow(desired: readonly EndpointSpec[], replaceAll: boolean): Promise<void> {
        for (const spec of desired) {
            this.assertSupported(spec.role);
        }
        const live = this.liveComposition();
        const plan = planReconcile(live, desired, replaceAll);

        // Issue #240 — a removed identity whose `indigoDeviceId` also
        // appears in `plan.create` is one device changing which identity it
        // publishes, planned as removal+create rather than an in-place recreate
        // (`reconcile.ts`'s `planReconcile`). Logged before anything mutates so
        // the "why" is on record even if the batch later aborts.
        //
        // TWO different things have that shape, and they must not share a
        // sentence. `supersedes()` is the same narrow test `node.ts` uses to
        // decide whether to write `supersededBy`: only a LATER GENERATION of
        // the same identity retires the old one for good. Anything else with
        // this shape leaves an ORDINARY orphan behind — so saying "retired …
        // never reused" about it would be flatly wrong, and its "role X → X"
        // would make the line read as a role change that had not happened.
        const createdByDeviceId = new Map(plan.create.map(spec => [spec.indigoDeviceId, spec]));
        for (const oldPublishedAs of plan.remove) {
            const old = live.get(oldPublishedAs);
            const created = old !== undefined ? createdByDeviceId.get(old.indigoDeviceId) : undefined;
            if (old === undefined || created === undefined) {
                continue;
            }
            const liveEndpoint = this.#live.get(old.indigoDeviceId);
            const number = liveEndpoint !== undefined ? Number(liveEndpoint.endpoint.number) : "?";
            if (supersedes(oldPublishedAs, created.publishedAs)) {
                this.#log(
                    `Superseding endpoint ${old.indigoDeviceId}: role ${old.role} → ${created.role}. Accessory ` +
                        `identity ${oldPublishedAs} (number ${number}) is being retired and ${created.publishedAs} ` +
                        "published in its place, so controllers process a removal and an addition rather than an " +
                        "in-place device-type change (issue #240). The retired number is never reused.",
                );
            } else {
                this.#log(
                    `Endpoint ${old.indigoDeviceId} is moving from accessory identity ${oldPublishedAs} ` +
                        `(number ${number}) to ${created.publishedAs}: controllers process a removal and an ` +
                        "addition. This is NOT a supersession — the identity it is leaving is an ordinary " +
                        "left-behind accessory, and its number is kept for it.",
                );
            }
        }

        for (const spec of plan.recreate) {
            // §4.1 rejects a role change through `upsert_endpoint`; attach is the
            // reconcile path and does it the only way Matter allows for an
            // identity that keeps its `publishedAs`. A supersede (a role change
            // that DID bump the generation) never reaches this loop — it is a
            // remove+create pair, logged above — so everything left here is
            // either a battery gain or the pre-#240 version-skew fallback
            // (PR5 design §1.3: an older plugin that never bumps the generation).
            const existing = live.get(spec.publishedAs);
            if (existing !== undefined && existing.role === spec.role) {
                this.#log(
                    `Recreating endpoint ${spec.indigoDeviceId}: gained a battery (PowerSource can only be ` +
                        "declared at construction; the accessory's name/room MAY not survive in every ecosystem)",
                );
            } else {
                this.#log(
                    `Recreating endpoint ${spec.indigoDeviceId} in place: role changed but the plugin asked ` +
                        `for the same accessory identity ${spec.publishedAs}, so this is the pre-#240 path — ` +
                        "Apple Home may need a home-hub restart (docs/MATTER.md).",
                );
            }
        }

        const removedDeviceIds: number[] = [];
        for (const publishedAs of plan.remove) {
            const indigoDeviceId = this.deviceIdFor(publishedAs);
            if (indigoDeviceId === undefined) {
                // `plan.remove` comes from `liveComposition()`, which is built
                // from `#live` — so a key that resolves to nothing means the
                // two disagree, and the accessory the plugin asked to remove
                // is about to stay in every paired ecosystem. A `.filter()`
                // dropped it without a word; the removal count downstream then
                // looks correct, which is the worst possible shape for this.
                this.#log(
                    `Cannot remove accessory identity ${publishedAs}: no live endpoint is publishing it. ` +
                        "It is NOT being removed from any paired ecosystem — the live set and the endpoint " +
                        "map disagree about who publishes it.",
                );
                continue;
            }
            removedDeviceIds.push(indigoDeviceId);
        }
        // Issue #246 — resolved via `deviceIdFor(spec.publishedAs)`, NOT
        // `spec.indigoDeviceId`. A plain `recreate` (role/battery change,
        // driving device unchanged) has the two agree, but the pre-#240 skew
        // fallback above also covers a device-id change COMBINED with a role
        // or battery change (`planReconcile` refuses the pure rekey shape for
        // those, falling back here instead) — and for that spec, `spec`'s own
        // device id is the NEW driver, not the live one that must be closed.
        // Using it directly would leave the true live endpoint behind,
        // untouched and unremovable, while `create()` below fails trying to
        // `add()` a second endpoint at the same `Endpoint.id`.
        const removals = [
            ...removedDeviceIds,
            // The live snapshot `plan` was built from guarantees a hit here —
            // `spec.publishedAs` was live (that is why it is a `recreate`, not
            // a `create`) and nothing has mutated `#live` yet.
            ...plan.recreate.map(spec => this.deviceIdFor(spec.publishedAs)!),
        ];
        const total =
            removals.length + plan.create.length + plan.recreate.length + plan.update.length + plan.rekey.length;
        let done = 0;
        let mutated = false;

        try {
            await this.removeMany(removals, () => {
                mutated = true;
                done += 1;
            });
            // Issue #246/ADR-0011 — removals run BEFORE rekeys, and the order
            // is LOAD-BEARING, not cosmetic. When a migrate's target device
            // was previously exported, its OLD identity is in `plan.remove`,
            // and that removal must free the target's `#live` key before any
            // rekey can claim it. Reversed, a rekey's `#live.set()` would
            // overwrite the target's still-live entry — and `removeMany`
            // (which resolves via `#live.get` at execution time) would then
            // close the freshly-migrated endpoint while leaving the target's
            // OLD endpoint an unwatched zombie: still in the Matter tree,
            // no longer in `#live`, unreachable by any command.
            //
            // A rekey touches neither the aggregator's children nor
            // `ConfigurationVersion`, so it does not set `mutated`. A
            // part-applied reconcile that got this far and then failed later
            // still owes no bump for the rekeys alone.
            //
            // Applied in two phases so a SWAP CYCLE (desired [spec(1,
            // publishedAs indigo-106), spec(106, publishedAs indigo-1)]
            // against live {1: indigo-1, 106: indigo-106} — a shape
            // `parseEndpointSpecs` accepts and a hand-edited `.indiPref` can
            // reach) does not corrupt `#live`. Processing rekeys one at a
            // time would have the first one's `#live.set()` overwrite the
            // entry the second one still needs to read. Phase 1 frees every
            // rekey's `fromDeviceId` key before phase 2 claims any target
            // key, so by the time phase 2 runs every target key is provably
            // free — the same device id twice in `desired` is refused by
            // `parseEndpointSpecs`, and an identity that should have vacated
            // a target key was removed above.
            const rekeys: { spec: EndpointSpec; fromDeviceId: number; live: LiveEndpoint }[] = [];
            for (const { spec, fromDeviceId } of plan.rekey) {
                const fromLive = this.#live.get(fromDeviceId);
                if (fromLive === undefined) {
                    // `planReconcile` saw this identity live under
                    // `fromDeviceId` in the SAME snapshot this reconcile was
                    // planned from; if it is gone by the time this runs, the
                    // planner and `#live` disagree. Logging and skipping used
                    // to let the plugin's `attach` report `told=True` — and
                    // the Indigo Event Log a "MIGRATED … no ecosystem
                    // processed any change" WARNING — over a rekey that never
                    // landed, and the node's own log never reaches Indigo.
                    // Throwing propagates the abort instead, so the plugin
                    // reports `told=False` honestly and the part-applied
                    // `done`/`mutated` accounting above still holds.
                    throw new ProtocolError(
                        ErrorCode.internal,
                        `cannot rekey accessory identity ${spec.publishedAs} to device ${spec.indigoDeviceId}: ` +
                            `no live endpoint is publishing it under device ${fromDeviceId} — the live set and ` +
                            "the plan disagree about who currently drives it",
                    );
                }
                this.#live.delete(fromDeviceId);
                rekeys.push({ spec, fromDeviceId, live: fromLive });
            }
            for (const { spec, fromDeviceId, live } of rekeys) {
                await this.rekeyOne(fromDeviceId, spec, live);
                done += 1;
            }
            for (const spec of [...plan.create, ...plan.recreate]) {
                await this.create(spec);
                mutated = true;
                done += 1;
            }
            for (const spec of plan.update) {
                await this.update(spec);
                done += 1;
            }
        } catch (error) {
            // A part-applied reconcile still changed the bridged-node set, so
            // controllers still need the nudge — skipping the bump because the
            // batch failed is how a half-applied set becomes an invisible one.
            if (mutated) {
                await this.noteConfigurationChange();
            }
            this.#log(
                `reconcile aborted after ${done}/${total} operations; ` +
                    `live set now [${[...this.#live.keys()].sort((a, b) => a - b).join(", ")}] ` +
                    `(requested [${desired.map(spec => spec.indigoDeviceId).join(", ")}])`,
            );
            throw error;
        }

        if (plan.create.length > 0 || plan.recreate.length > 0 || plan.remove.length > 0) {
            await this.noteConfigurationChange();
        }
        this.#log(
            `Reconciled endpoints: ${plan.create.length} created, ${plan.update.length} updated, ` +
                `${plan.recreate.length} recreated, ${plan.rekey.length} rekeyed, ${plan.remove.length} removed ` +
                `(${this.#live.size} live)`,
        );
    }

    /** §3.2 — create-or-update, idempotent, `role_change` on a role mismatch. */
    async upsert(spec: EndpointSpec): Promise<UpsertResult> {
        return this.serialize("upsert_endpoint", async () => {
            this.assertSupported(spec.role);
            const existing = this.#live.get(spec.indigoDeviceId);
            if (existing === undefined) {
                const claimedBy = this.deviceIdFor(spec.publishedAs);
                if (claimedBy !== undefined) {
                    // Issue #246/ADR-0011 — the transpose of the
                    // `existing.publishedAs !== spec.publishedAs` refusal
                    // below. THIS device is not live, but the identity it
                    // wants IS, under a DIFFERENT device — falling into
                    // `create()` here would `add()` a second endpoint at the
                    // same `Endpoint.id` the aggregator already holds, which
                    // matter.js fails on internally rather than refusing
                    // cleanly. A driving-device change under a stable
                    // identity is a reconcile REKEY (`reconcile.ts`), and only
                    // `attach` — the plugin's migrate flow — is allowed to
                    // make it; `upsert_endpoint` names the door instead of
                    // falling through it.
                    throw new ProtocolError(
                        ErrorCode.roleChange,
                        `accessory identity ${spec.publishedAs} is live on device ${claimedBy}; ` +
                            `upsert_endpoint cannot move it to device ${spec.indigoDeviceId} — a driving-device ` +
                            "change must go through a full attach reconcile (the plugin's migrate flow), not " +
                            "upsert_endpoint",
                    );
                }
                const created = await this.create(spec);
                await this.noteConfigurationChange();
                return { endpointNumber: Number(created.number) };
            }
            if (existing.publishedAs !== spec.publishedAs) {
                // Issues #219/#240 — `upsert_endpoint` must never silently
                // retire an accessory identity: that is a supersede, and the
                // only path allowed to do it is the plugin's remove-then-add
                // `replace()` (`export_bridge.py`), which sends the two mutations as two
                // separate commands rather than asking this one to move a live
                // endpoint out from under itself.
                throw new ProtocolError(
                    ErrorCode.roleChange,
                    `endpoint ${spec.indigoDeviceId} is published as ${existing.publishedAs}; upsert_endpoint ` +
                        `cannot move it to ${spec.publishedAs} — that is a supersession (issue #240); remove ` +
                        "the old identity and create the new one instead",
                );
            }
            if (existing.role !== spec.role) {
                throw new ProtocolError(
                    ErrorCode.roleChange,
                    `endpoint ${spec.indigoDeviceId} is ${existing.role}; remove and re-add to change role`,
                );
            }
            if (spec.battery && !existing.battery) {
                // Issue #220: PowerSource can only be declared at construction
                // (measured, §0), so a battery GAIN has to go through the same
                // remove-then-add `reconcileNow` uses for a role change — an
                // ordinary `update()` (a plain `endpoint.set()`) cannot add a
                // cluster to a live endpoint. A LOSS is deliberately not
                // symmetric: see `update`'s call into `applyStates`. Loud for
                // the same reason `reconcileNow` is loud about its own
                // recreate: the accessory's name/room MAY not survive in
                // every paired ecosystem.
                this.#log(
                    `Recreating endpoint ${spec.indigoDeviceId}: gained a battery (PowerSource can only be ` +
                        "declared at construction; the accessory's name/room MAY not survive in every ecosystem)",
                );
                await this.closeOne(spec.indigoDeviceId, existing);
                try {
                    const created = await this.create(spec);
                    await this.noteConfigurationChange();
                    return { endpointNumber: Number(created.number) };
                } catch (error) {
                    // `closeOne` already succeeded, so the bridged-node set
                    // changed even though `create` never finished — the exact
                    // case `reconcileNow`'s own catch handles above ("a
                    // half-applied set becomes an invisible one"). Skipping
                    // the bump here would leave every paired controller
                    // believing the old accessory still exists.
                    await this.noteConfigurationChange();
                    throw error;
                }
            }
            await this.update(spec);
            return { endpointNumber: Number(existing.endpoint.number) };
        });
    }

    /** §3.3 — idempotent removal. The endpoint-number allocation is retained. */
    async remove(indigoDeviceId: number): Promise<RemoveResult> {
        return this.serialize("remove_endpoint", async () => {
            const live = this.#live.get(indigoDeviceId);
            if (live === undefined) {
                return { removed: false };
            }
            await this.closeOne(indigoDeviceId, live);
            await this.noteConfigurationChange();
            return { removed: true };
        });
    }

    /** §3.4 — role-specific state keys, applied as local writes. */
    async setState(indigoDeviceId: number, states: Record<string, unknown>): Promise<void> {
        return this.serialize("set_state", async () => {
            const live = this.require(indigoDeviceId);
            await applyStates(live.endpoint, live.role, states, live.battery, indigoDeviceId);
        });
    }

    /** §3.5 */
    async setReachable(indigoDeviceId: number, reachable: boolean): Promise<void> {
        return this.serialize("set_reachable", async () => {
            const live = this.require(indigoDeviceId);
            await applyReachable(live.endpoint, reachable);
        });
    }

    /** Drop every listener. The endpoints themselves die with the ServerNode. */
    close(): void {
        for (const timer of this.#pendingOffs.values()) {
            clearTimeout(timer);
        }
        this.#pendingOffs.clear();
        for (const live of this.#live.values()) {
            live.unwatch();
        }
        this.#live.clear();
    }

    private require(indigoDeviceId: number): LiveEndpoint {
        const live = this.#live.get(indigoDeviceId);
        if (live === undefined) {
            throw new ProtocolError(
                ErrorCode.unknownDevice,
                `no live endpoint for indigoDeviceId ${indigoDeviceId}`,
            );
        }
        return live;
    }

    private assertSupported(role: RoleValue): void {
        if (!isSupportedRole(role)) {
            throw new ProtocolError(ErrorCode.internal, UNSUPPORTED_ROLE_DETAILS(role));
        }
    }

    private async create(spec: EndpointSpec, restoring = false): Promise<Endpoint> {
        const endpoint = createEndpoint(spec, this.options.bridgeIdentity);
        await this.options.aggregator.add(endpoint);
        if (!restoring) {
            // #203 — a fresh export has no countdown. matter.js keeps `onTime`
            // under the endpoint id after an un-export, and the map keeps a
            // marker until the record goes; neither belongs to this endpoint.
            this.clearMarker(spec.publishedAs);
            try {
                await zeroStaleOnTime(endpoint);
            } catch (error) {
                await endpoint.close().catch(() => undefined);
                throw error;
            }
        }
        let unwatch: () => void;
        try {
            unwatch = watchCommands(
                endpoint,
                spec,
                this.options.emit,
                this.#log,
                this.countdownHooksFor(spec.publishedAs, spec.indigoDeviceId),
            );
        } catch (error) {
            // The endpoint is already in the Matter tree but will never be in
            // `#live`: take it back out rather than leave an accessory the
            // registry does not know about (the same zombie {@link closeOne}
            // exists to prevent, from the other end).
            await endpoint.close().catch(() => undefined);
            throw error;
        }
        this.#live.set(spec.indigoDeviceId, {
            endpoint, role: spec.role, label: spec.label, battery: spec.battery, publishedAs: spec.publishedAs, unwatch,
        });
        this.#log(`Endpoint ${spec.indigoDeviceId} (${spec.role}) added as number ${Number(endpoint.number)}`);
        // #355: the states sent with the export must land. matter.js keeps the
        // persisted attributes of an endpoint id that was merely closed
        // (ADR-0016 deletes our map record, not matter.js's storage), so after
        // un-export -> node restart -> re-export the construction values lose
        // to stale persisted ones, and the plugin has already recorded these
        // states as pushed — nothing would ever correct the accessory.
        // `restore()` passes `{}`, so a restored endpoint is untouched. Goes
        // through `applyStates` like `update()` does: the same locked onOff
        // write (#338), ADR-0017's level retention, and no command events
        // (the push is the plugin telling the node, not an ecosystem write).
        if (Object.keys(spec.states).length > 0) {
            try {
                await applyStates(endpoint, spec.role, spec.states, spec.battery, spec.indigoDeviceId);
            } catch (error) {
                // The endpoint is in the tree and stays registered (the next
                // `update()` retries these states), so the bridged-node set
                // HAS changed even though this call throws — and callers'
                // `mutated`/post-create bump only run on success. Announce it
                // here, then let the failure propagate so the reconcile
                // reports it instead of leaving stale state unmentioned.
                await this.noteConfigurationChange();
                // The endpoint IS live (added, watched, counted) — only the
                // state it was exported with did not land, so a controller may
                // be reading a stale value from it. Say exactly that: a bare
                // matter.js error names neither the device nor that the
                // accessory exists (#355).
                throw new ProtocolError(
                    ErrorCode.internal,
                    `device ${spec.indigoDeviceId} (${spec.role}): ${STATE_NOT_APPLIED_PHRASE} ` +
                        `and it may show stale state until the plugin pushes it again: ${describeError(error)}`,
                );
            }
        }
        return endpoint;
    }

    /** Label, reachability, CT physical bounds (#293) and state — everything an existing endpoint can change. */
    private async update(spec: EndpointSpec): Promise<void> {
        const live = this.require(spec.indigoDeviceId);
        // Both labels, because {@link applyLabel} writes both: comparing only
        // `nodeLabel` would call an endpoint up to date while its `productLabel`
        // still showed the name from two renames ago.
        const info = live.endpoint.stateOf("bridgedDeviceBasicInformation") as {
            nodeLabel?: string;
            productLabel?: string;
        };
        if (info.nodeLabel !== spec.label || info.productLabel !== spec.label) {
            await applyLabel(live.endpoint, spec.label);
        }
        // Recorded whether or not the write was needed: the cached copy is what
        // `endpoint-map.json` persists (issue #141), and letting it lag behind
        // the cluster would restore yesterday's name after a restart.
        live.label = spec.label;
        await applyReachable(live.endpoint, spec.reachable);
        // #293 — before `applyStates`: a `set_state` in the SAME push that
        // changes bounds must clamp against the NEW bounds, not the ones this
        // endpoint had a moment ago.
        //
        // Caught and logged rather than left to propagate: bounds are
        // advisory (ADR-0014 — "a refused attach takes every export offline
        // over one bad option" applies just as much to a live update as to
        // an attach), and `applyStates` below already clamps CT to the
        // endpoint's LIVE bounds, so continuing with the OLD bounds still
        // in place is safe. Left uncaught, one device's bad bounds would
        // both lose that device's own state push AND abort the whole
        // `reconcileNow` batch (its `plan.update` loop has no per-spec
        // catch of its own), taking every other device in the batch down
        // with it over an advisory write.
        try {
            await applyCtBoundsUpdate(live.endpoint, live.role, spec.options, spec.indigoDeviceId);
        } catch (error) {
            this.#log(
                `endpoint ${spec.indigoDeviceId} CT bounds update failed, keeping its previous ` +
                    `bounds: ${describeError(error)}`,
            );
        }
        // `live.battery`, not `spec.battery`: a battery LOSS on `spec` is left
        // alone here (§4.1, monotonic) — the gain case never reaches `update`
        // at all, `upsert`/`reconcileNow` route it to a recreate instead.
        await applyStates(live.endpoint, live.role, spec.states, live.battery, spec.indigoDeviceId);
    }

    /**
     * Issue #246/ADR-0011 — retarget a live identity's driving device without
     * adding, removing, or reallocating its `Endpoint`.
     *
     * Called only from `reconcileNow`'s phase 2, on a `live` entry phase 1
     * has ALREADY removed from `#live` under `fromDeviceId` — this method's
     * job is putting it back, under `spec.indigoDeviceId`, re-wired. The
     * command sink is re-watched under the SAME `Endpoint.id`
     * (`spec.publishedAs` is unchanged — that is what makes this a rekey and
     * not a `recreate`) so a `command` event raised a moment later is
     * attributed to the device that will actually receive it, not the one
     * that used to. Role and `publishedAs` are carried over from the live
     * entry untouched (the planner only reaches this bucket when both
     * already match `spec`); battery likewise, for the same monotonic reason
     * {@link update} leaves it alone. Label/reachable/state ARE applied
     * fresh, via `update` — a genuine, subscribable attribute write (a value
     * mismatch between the old and new device is visible at migrate time,
     * the same as any other `set_state`) — chosen over duplicating its three
     * lines because after the re-key `require(spec.indigoDeviceId)` resolves
     * to exactly this entry.
     *
     * **The occupied-key check is defensive, not a real branch.** By the time
     * this runs, `spec.indigoDeviceId` is provably free: removals ran before
     * any rekey (`reconcileNow`), phase 1 already vacated every rekey's
     * `fromDeviceId`, and `parseEndpointSpecs` refuses the same device id
     * twice in one `desired` set. If it is occupied anyway, something this
     * method cannot safely guess at has gone wrong — throw rather than let
     * `#live.set()` silently overwrite the occupant, which is the exact
     * swap-cycle corruption issue #246 exists to fix.
     *
     * **Restore-on-failure, matching {@link closeOne}'s order.** If
     * re-watching throws, the OLD watcher (the one `fromDeviceId` used) is
     * restored and `live` is put back in `#live` under `fromDeviceId` BEFORE
     * this throws — the same order `closeOne` uses, and for the same reason:
     * a caller that gets `internal` back must find the registry exactly as
     * it was, not half-migrated with no watcher live under either device id.
     *
     * Nothing here creates, closes, or reallocates an `Endpoint` — the whole
     * point. `reconcileNow` therefore never sets `mutated` for a rekey and
     * never bumps `ConfigurationVersion` on its account: no paired ecosystem
     * has anything to re-read.
     */
    private async rekeyOne(fromDeviceId: number, spec: EndpointSpec, live: LiveEndpoint): Promise<void> {
        if (this.#live.has(spec.indigoDeviceId)) {
            throw new ProtocolError(
                ErrorCode.internal,
                `cannot rekey accessory identity ${spec.publishedAs} from device ${fromDeviceId} to device ` +
                    `${spec.indigoDeviceId}: device ${spec.indigoDeviceId} already has a live endpoint — the ` +
                    "plan and the live set disagree about who is free to take this identity",
            );
        }
        live.unwatch();
        try {
            live.unwatch = watchCommands(
                live.endpoint,
                spec,
                this.options.emit,
                this.#log,
                this.countdownHooksFor(live.publishedAs, spec.indigoDeviceId),
            );
        } catch (error) {
            // Restore first, log, THEN throw — closeOne's order, so a caller
            // that sees `internal` can retry against a registry that still
            // knows this endpoint, under the device id it drove before this
            // rekey was ever attempted.
            live.unwatch = watchCommands(
                live.endpoint,
                { indigoDeviceId: fromDeviceId, role: live.role, publishedAs: live.publishedAs },
                this.options.emit,
                this.#log,
                this.countdownHooksFor(live.publishedAs, fromDeviceId),
            );
            this.#live.set(fromDeviceId, live);
            this.#log(
                `Endpoint ${fromDeviceId} failed to re-key to device ${spec.indigoDeviceId} for accessory ` +
                    `identity ${spec.publishedAs}; watcher restored under device ${fromDeviceId}: ` +
                    describeErrorWithStack(error),
            );
            throw new ProtocolError(
                ErrorCode.internal,
                `endpoint ${fromDeviceId} failed to re-key to device ${spec.indigoDeviceId}: ${describeError(error)}`,
            );
        }
        this.#live.set(spec.indigoDeviceId, live);
        // Resolves `spec.indigoDeviceId` — the entry moved above.
        await this.update(spec);
        this.#log(
            `Endpoint ${spec.indigoDeviceId} now drives accessory identity ${spec.publishedAs} (number ` +
                `${Number(live.endpoint.number)}), replacing device ${fromDeviceId}. Nothing changed on the ` +
                "wire — no paired ecosystem sees any change (issue #246 migrate).",
        );
    }

    /**
     * Close one endpoint and, only if that worked, forget it.
     *
     * The order is the whole point. Evicting first and closing after means a
     * throwing `close()` leaves an accessory that is still in the Matter tree
     * and still visible in every paired ecosystem, but that the registry denies
     * exists: taps on it produce no `command` events, and the plugin's retry
     * gets `{removed: false}` — a §3.3-idempotent *success* — so nothing ever
     * reports the zombie. Closing first makes the failure a failure: the entry
     * stays, its listeners are put back, and the plugin sees `internal` on a
     * call it can honestly retry.
     */
    private async closeOne(indigoDeviceId: number, live: LiveEndpoint): Promise<void> {
        // Unwatch first so a close-time attribute change cannot emit a command
        // for an endpoint on its way out; restored below if the close fails.
        live.unwatch();
        try {
            await live.endpoint.close();
        } catch (error) {
            live.unwatch = watchCommands(
                live.endpoint,
                { indigoDeviceId, role: live.role, publishedAs: live.publishedAs },
                this.options.emit,
                this.#log,
                this.countdownHooksFor(live.publishedAs, indigoDeviceId),
            );
            this.#log(
                `Endpoint ${indigoDeviceId} failed to close; registry and Matter tree may disagree: ` +
                    describeErrorWithStack(error),
            );
            throw new ProtocolError(
                ErrorCode.internal,
                `endpoint ${indigoDeviceId} failed to close: ${describeError(error)}`,
            );
        }
        this.#live.delete(indigoDeviceId);
        // The marker dies with the endpoint (#203); a re-export starts clean.
        this.clearMarker(live.publishedAs);
        this.options.warn?.(`countdown:${live.publishedAs}`, undefined);
        this.#log(`Endpoint ${indigoDeviceId} removed`);
    }

    /**
     * PRD §5.3: ~100ms apart, so each removal is its own subscription update.
     *
     * `onRemoved` rather than a return count because the count matters most
     * when this *throws*: a mid-batch failure aborts the remaining removals,
     * and a returned tally would be thrown away exactly when §3.1's partial-
     * failure account needs it. The state {@link closeOne} restored is intact
     * either way; `reconcile` reports how far it got.
     */
    private async removeMany(indigoDeviceIds: readonly number[], onRemoved: () => void): Promise<void> {
        for (const [index, indigoDeviceId] of indigoDeviceIds.entries()) {
            const live = this.#live.get(indigoDeviceId);
            if (live === undefined) {
                continue;
            }
            await this.closeOne(indigoDeviceId, live);
            onRemoved();
            if (index < indigoDeviceIds.length - 1 && this.#pacingMs > 0) {
                await new Promise(resolve => setTimeout(resolve, this.#pacingMs));
            }
        }
    }

    private async noteConfigurationChange(): Promise<void> {
        try {
            await this.options.onConfigurationChange?.();
        } catch (error) {
            // A ConfigurationVersion bump is a hint to controllers, not a
            // correctness requirement: failing the whole command over it would
            // turn a cosmetic problem into a broken export.
            this.#log(`Could not bump ConfigurationVersion: ${String(error)}`);
        }
    }
}
