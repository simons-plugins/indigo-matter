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

    constructor(private readonly options: EndpointRegistryOptions) {
        this.#log = options.log ?? (() => {});
        this.#pacingMs = options.removalPacingMs ?? REMOVAL_PACING_MS;
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
     * §3.1: full reconcile against the desired set. Throws
     * `mass_removal_refused` before touching anything — the guard has to be a
     * gate, not a rollback.
     */
    async reconcile(desired: readonly EndpointSpec[], replaceAll: boolean): Promise<void> {
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
        await this.removeMany([...plan.remove, ...plan.recreate.map(spec => spec.indigoDeviceId)]);

        for (const spec of [...plan.create, ...plan.recreate]) {
            await this.create(spec);
        }
        for (const spec of plan.update) {
            await this.update(spec);
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
    }

    /** §3.3 — idempotent removal. The endpoint-number allocation is retained. */
    async remove(indigoDeviceId: number): Promise<RemoveResult> {
        const live = this.#live.get(indigoDeviceId);
        if (live === undefined) {
            return { removed: false };
        }
        await this.closeOne(indigoDeviceId, live);
        await this.noteConfigurationChange();
        return { removed: true };
    }

    /** §3.4 — role-specific state keys, applied as local writes. */
    async setState(indigoDeviceId: number, states: Record<string, unknown>): Promise<void> {
        const live = this.require(indigoDeviceId);
        await applyStates(live.endpoint, live.role, states);
    }

    /** §3.5 */
    async setReachable(indigoDeviceId: number, reachable: boolean): Promise<void> {
        const live = this.require(indigoDeviceId);
        await applyReachable(live.endpoint, reachable);
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
        const unwatch = watchCommands(endpoint, spec, this.options.emit);
        this.#live.set(spec.indigoDeviceId, { endpoint, role: spec.role, unwatch });
        this.#log(`Endpoint ${spec.indigoDeviceId} (${spec.role}) added as number ${Number(endpoint.number)}`);
        return endpoint;
    }

    /** Label, reachability and state — everything an existing endpoint can change. */
    private async update(spec: EndpointSpec): Promise<void> {
        const live = this.require(spec.indigoDeviceId);
        const info = live.endpoint.stateOf("bridgedDeviceBasicInformation") as { nodeLabel?: string };
        if (info.nodeLabel !== spec.label) {
            await applyLabel(live.endpoint, spec.label);
        }
        await applyReachable(live.endpoint, spec.reachable);
        await applyStates(live.endpoint, live.role, spec.states);
    }

    private async closeOne(indigoDeviceId: number, live: LiveEndpoint): Promise<void> {
        live.unwatch();
        this.#live.delete(indigoDeviceId);
        await live.endpoint.close();
        this.#log(`Endpoint ${indigoDeviceId} removed`);
    }

    /** PRD §5.3: ~100ms apart, so each removal is its own subscription update. */
    private async removeMany(indigoDeviceIds: readonly number[]): Promise<void> {
        for (const [index, indigoDeviceId] of indigoDeviceIds.entries()) {
            const live = this.#live.get(indigoDeviceId);
            if (live === undefined) {
                continue;
            }
            await this.closeOne(indigoDeviceId, live);
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
