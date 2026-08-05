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
    type UpsertResult,
} from "./protocol.js";
import { planReconcile } from "./reconcile.js";

/**
 * PRD §5.3: bulk removals are paced so controllers see one subscription update
 * each rather than a burst they may coalesce or drop.
 */
export const REMOVAL_PACING_MS = 100;

interface LiveEndpoint {
    endpoint: Endpoint;
    role: RoleValue;
    /** Reassigned when a failed close forces the listeners to be restored. */
    unwatch: () => void;
}

export interface EndpointRegistryOptions {
    /** The aggregator every bridged child is added to. */
    aggregator: Endpoint;
    /** Sink for §5 `command` events. */
    emit: (data: CommandEventData) => void;
    log?: (message: string) => void;
    /** `ProductName` on every child's Bridged Device Basic Information. */
    productName: string;
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
            }))
            .sort((a, b) => a.indigoDeviceId - b.indigoDeviceId);
    }

    get size(): number {
        return this.#live.size;
    }

    /** The live role of a device, or undefined — what the reconcile planner diffs. */
    liveRoles(): Map<number, RoleValue> {
        return new Map([...this.#live.entries()].map(([id, live]) => [id, live.role]));
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
        const plan = planReconcile(this.liveRoles(), desired, replaceAll);

        for (const indigoDeviceId of plan.recreate.map(spec => spec.indigoDeviceId)) {
            // §4.1 rejects a role change through `upsert_endpoint`; attach is the
            // reconcile path and does it the only way Matter allows, which is a
            // new accessory. Loud, because ecosystems lose the old one's name.
            this.#log(`Recreating endpoint ${indigoDeviceId}: role changed`);
        }

        const removals = [...plan.remove, ...plan.recreate.map(spec => spec.indigoDeviceId)];
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
            if (existing.role !== spec.role) {
                throw new ProtocolError(
                    ErrorCode.roleChange,
                    `endpoint ${spec.indigoDeviceId} is ${existing.role}; remove and re-add to change role`,
                );
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
            await applyStates(live.endpoint, live.role, states);
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
        const endpoint = createEndpoint(spec, this.options.productName);
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
        this.#live.set(spec.indigoDeviceId, { endpoint, role: spec.role, unwatch });
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
        await applyReachable(live.endpoint, spec.reachable);
        await applyStates(live.endpoint, live.role, spec.states);
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
                { indigoDeviceId, role: live.role },
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
