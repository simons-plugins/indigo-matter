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
    applyLabel,
    applyReachable,
    applyStates,
    type BridgedIdentity,
    createEndpoint,
    isSupportedRole,
    UNSUPPORTED_ROLE_DETAILS,
    watchCommands,
} from "./endpoints.js";
import {
    type CommandEventData,
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
     * Issues #219/#240 — the accessory identity this live endpoint was built
     * with (`Endpoint.id`/`UniqueID`/`SerialNumber`). Held here rather than
     * re-derived because a re-adopted identity is no longer a pure function of
     * `indigoDeviceId` — see {@link EndpointSpec.publishedAs}.
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

export interface EndpointRegistryOptions {
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
                    await this.create(spec);
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

    private async reconcileNow(desired: readonly EndpointSpec[], replaceAll: boolean): Promise<void> {
        for (const spec of desired) {
            this.assertSupported(spec.role);
        }
        const live = this.liveComposition();
        const plan = planReconcile(live, desired, replaceAll);

        // Issues #219/#240 — a removed identity whose `indigoDeviceId` also
        // appears in `plan.create` is one device changing which identity it
        // publishes, planned as removal+create rather than an in-place recreate
        // (`reconcile.ts`'s `planReconcile`). Logged before anything mutates so
        // the "why" is on record even if the batch later aborts.
        //
        // TWO different things have that shape, and they must not share a
        // sentence. `supersedes()` is the same narrow test `node.ts` uses to
        // decide whether to write `supersededBy`: only a LATER GENERATION of
        // the same identity retires the old one for good. A re-adopt (PR5
        // design E2/E5) is also one removal plus one create for one device, and
        // the identity it leaves behind is an ORDINARY orphan the map goes on
        // offering to the re-adopt picker — so saying "retired … never reused"
        // about it was flatly wrong, and its "role X → X" made the line read
        // as a role change that had not happened.
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
                        "left-behind accessory that stays re-adoptable, and its number is kept for it " +
                        "(issue #219 re-adopt).",
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
            // (§1.3: an older plugin that never bumps the generation).
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

        const removals = [
            ...plan.remove
                .map(publishedAs => this.deviceIdFor(publishedAs))
                .filter((indigoDeviceId): indigoDeviceId is number => indigoDeviceId !== undefined),
            ...plan.recreate.map(spec => spec.indigoDeviceId),
        ];
        const total = removals.length + plan.create.length + plan.recreate.length + plan.update.length;
        let done = 0;
        let mutated = false;

        try {
            await this.removeMany(removals, () => {
                mutated = true;
                done += 1;
            });
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
                `${plan.recreate.length} recreated, ${plan.remove.length} removed (${this.#live.size} live)`,
        );
    }

    /** §3.2 — create-or-update, idempotent, `role_change` on a role mismatch. */
    async upsert(spec: EndpointSpec): Promise<UpsertResult> {
        return this.serialize("upsert_endpoint", async () => {
            this.assertSupported(spec.role);
            const existing = this.#live.get(spec.indigoDeviceId);
            if (existing === undefined) {
                const created = await this.create(spec);
                await this.noteConfigurationChange();
                return { endpointNumber: Number(created.number) };
            }
            if (existing.publishedAs !== spec.publishedAs) {
                // Issues #219/#240 — `upsert_endpoint` must never silently
                // retire an accessory identity: that is a supersede, and the
                // only path allowed to do it is the plugin's remove-then-add
                // `replace()` (commit 5), which sends the two mutations as two
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

    private async create(spec: EndpointSpec): Promise<Endpoint> {
        const endpoint = createEndpoint(spec, this.options.bridgeIdentity);
        await this.options.aggregator.add(endpoint);
        let unwatch: () => void;
        try {
            unwatch = watchCommands(endpoint, spec, this.options.emit, this.#log);
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
        return endpoint;
    }

    /** Label, reachability and state — everything an existing endpoint can change. */
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
        // `live.battery`, not `spec.battery`: a battery LOSS on `spec` is left
        // alone here (§4.1, monotonic) — the gain case never reaches `update`
        // at all, `upsert`/`reconcileNow` route it to a recreate instead.
        await applyStates(live.endpoint, live.role, spec.states, live.battery, spec.indigoDeviceId);
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
