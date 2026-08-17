/**
 * A {@link BridgeFacade} that returns the golden fixture payloads verbatim.
 *
 * The protocol tests exercise the real ws-server against this, so they need no
 * Matter stack, no network and no commissioned state.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
    type BridgeFacade,
    type CommandEventData,
    type CommissioningWindowResult,
    type DriftEntry,
    type EndpointSpec,
    ErrorCode,
    type FabricInfo,
    type PairingReport,
    ProtocolError,
    type RemoveFabricResult,
    type RemoveResult,
    type RoleValue,
    type StatusReport,
    type UpsertResult,
    type WindowClosedReason,
} from "../src/protocol.js";
import { type LiveComposition, planReconcile } from "../src/reconcile.js";

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
    /** §5/§4.3 — the endpoint-number drift report, emitted since E5. */
    drift_detected: GoldenEvent & { data: { drift: unknown[] } };
    command_on_off: GoldenEvent;
    command_set_level: GoldenEvent;
    command_set_color_temp: GoldenEvent;
    command_set_color: GoldenEvent;
    attach_with_endpoints: GoldenExchange;
    attach_replace_all: GoldenExchange;
    attach_mass_removal_refused: GoldenExchange;
    upsert_endpoint: GoldenExchange;
    upsert_endpoint_role_change: GoldenExchange;
    upsert_endpoint_unknown_role: GoldenExchange;
    remove_endpoint: GoldenExchange;
    remove_endpoint_absent: GoldenExchange;
    set_state: GoldenExchange;
    set_state_unknown_device: GoldenExchange;
    set_state_bad_keys: GoldenExchange;
    /** §4.1 issue #220 — `batteryLevel` refused against a non-battery endpoint. */
    set_state_battery_on_a_mains_endpoint: GoldenExchange;
    set_reachable: GoldenExchange;
    /**
     * The whole §4.2 role table in one `attach`, plus the per-role `set_state`
     * family. Graduated out of `pending` at E4, when the node grew the last of
     * the role factories.
     */
    attach_all_roles: GoldenExchange;
    set_state_on_off_plug_in_unit: GoldenExchange;
    set_state_dimmable_light: GoldenExchange;
    set_state_color_temperature_light: GoldenExchange;
    set_state_extended_color_light: GoldenExchange;
    set_state_window_covering: GoldenExchange;
    set_state_door_lock: GoldenExchange;
    set_state_occupancy_sensor: GoldenExchange;
    set_state_contact_sensor: GoldenExchange;
    set_state_temperature_sensor: GoldenExchange;
    set_state_humidity_sensor: GoldenExchange;
    set_state_light_sensor: GoldenExchange;
    set_state_pressure_sensor: GoldenExchange;
    set_state_flow_sensor: GoldenExchange;
    set_state_thermostat: GoldenExchange;
    /**
     * §3.9-§3.11 plus the §1.1 refuse-to-start refusal. Graduated out of
     * `pending` at E5, when the node grew the endpoint-number map that all four
     * of them are about.
     */
    remove_fabric: GoldenExchange;
    /** §3.9 over a stale index — the outcome `{}` used to hide. */
    remove_fabric_already_gone: GoldenExchange;
    factory_reset: GoldenExchange;
    factory_reset_discard_map: GoldenExchange;
    rebuild_endpoint_map: GoldenExchange;
    endpoint_map_invalid: GoldenExchange;
    /**
     * The holding pen for commands the node does not implement yet — EMPTY
     * since E5, and asserted empty. Kept because it is the mechanism the next
     * protocol addition uses, not a backlog.
     */
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

/**
 * The endpoint set the double models, so `attach`/`upsert`/`remove` answer with
 * something a real reconcile could have produced rather than a canned blob.
 *
 * It reuses the production planner (`planReconcile`) on purpose: the §3.1
 * mass-removal guard and the §4.1 role rules are decided once, in `src/`, and
 * the double inherits them. What it fakes is only the Matter part — a *first*
 * endpoint number is handed out in creation order from 2, as matter.js does for
 * a freshly built aggregator, and thereafter the number belongs to the device id
 * for good ({@link #allocated}), as matter.js's persisted `Endpoint.id` map does.
 * Retention is not a detail: it is what makes a role-change recreate keep its
 * accessory identity (`registry.test.ts` asserts exactly that against the real
 * stack), and a double that renumbered would have quietly disagreed with the
 * thing it stands in for.
 */
class EndpointModel {
    readonly #endpoints = new Map<number, { role: RoleValue; endpointNumber: number; battery: boolean }>();
    /** Every number ever handed out, by device id. Never pruned — see above. */
    readonly #allocated = new Map<number, number>();
    #next = 2;

    composition(): Map<number, LiveComposition> {
        return new Map([...this.#endpoints.entries()].map(([id, entry]) =>
            [id, { role: entry.role, battery: entry.battery }]));
    }

    summaries(): StatusReport["endpoints"] {
        // `battery` is deliberately NOT part of the §4.3 `EndpointSummary` wire
        // shape (it is an `endpoint-map.json` concern, not a StatusReport one)
        // — named fields rather than a spread of `entry`, or the model's own
        // bookkeeping leaks onto the wire.
        return [...this.#endpoints.entries()]
            .map(([indigoDeviceId, entry]) => ({ indigoDeviceId, role: entry.role, endpointNumber: entry.endpointNumber }))
            .sort((a, b) => a.indigoDeviceId - b.indigoDeviceId);
    }

    has(indigoDeviceId: number): boolean {
        return this.#endpoints.has(indigoDeviceId);
    }

    reconcile(desired: readonly EndpointSpec[], replaceAll: boolean): void {
        const plan = planReconcile(this.composition(), desired, replaceAll);
        for (const indigoDeviceId of plan.remove) {
            this.#endpoints.delete(indigoDeviceId);
        }
        for (const spec of [...plan.create, ...plan.recreate]) {
            this.#endpoints.delete(spec.indigoDeviceId);
            this.add(spec);
        }
    }

    upsert(spec: EndpointSpec): UpsertResult {
        const existing = this.#endpoints.get(spec.indigoDeviceId);
        if (existing === undefined) {
            return { endpointNumber: this.add(spec) };
        }
        if (existing.role !== spec.role) {
            throw new ProtocolError(
                ErrorCode.roleChange,
                `endpoint ${spec.indigoDeviceId} is ${existing.role}; remove and re-add to change role`,
            );
        }
        return { endpointNumber: existing.endpointNumber };
    }

    remove(indigoDeviceId: number): RemoveResult {
        return { removed: this.#endpoints.delete(indigoDeviceId) };
    }

    require(indigoDeviceId: number): void {
        if (!this.#endpoints.has(indigoDeviceId)) {
            throw new ProtocolError(
                ErrorCode.unknownDevice,
                `no live endpoint for indigoDeviceId ${indigoDeviceId}`,
            );
        }
    }

    private add(spec: EndpointSpec): number {
        const endpointNumber = this.#allocated.get(spec.indigoDeviceId) ?? this.#next++;
        this.#allocated.set(spec.indigoDeviceId, endpointNumber);
        this.#endpoints.set(spec.indigoDeviceId, { role: spec.role, endpointNumber, battery: spec.battery });
        return endpointNumber;
    }
}

export class StubBridge implements BridgeFacade {
    commissioned = false;
    /** Only meaningful while {@link commissioned}: selects the 3rd §3.7 state. */
    windowOpen = false;
    /**
     * The commissioning half of the §4.3 StatusReport. Separate from
     * {@link commissioned}, which the §3.7 pairing tests toggle: a test that is
     * about pairing states must not silently rewrite what `get_status` answers.
     */
    statusCommissioned = false;
    statusFabrics: FabricInfo[] = [];
    readonly model = new EndpointModel();
    /** Every §5 `command` the double was asked to emit — for the event tests. */
    readonly commands: CommandEventData[] = [];
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
    #command?: (data: CommandEventData) => void;

    /**
     * Makes `get_status` answer something `JSON.stringify` cannot encode, so
     * the §1 "exactly one response" guarantee can be tested against a result
     * that fails on the way *out* rather than on the way in.
     */
    poisonStatus = false;

    /** §4.3 — what `get_status` reports; the drift tests set these. */
    drift: DriftEntry[] = [];
    driftChecked = false;
    /** §4.3 — persistence failures the node wants the plugin to surface. */
    warnings: string[] = [];
    /** §1.1 — non-undefined puts the double in the refuse-to-start state. */
    refusal?: string;
    /** Calls recorded by the §3.9/§3.10/§3.11 tests. */
    readonly removedFabrics: number[] = [];
    readonly factoryResets: boolean[] = [];
    rebuilds = 0;

    getStatus(): StatusReport {
        if (this.poisonStatus) {
            const circular: Record<string, unknown> = {};
            circular.self = circular;
            return circular as unknown as StatusReport;
        }
        const endpoints = this.model.summaries();
        return {
            commissioned: this.statusCommissioned,
            fabrics: structuredClone(this.statusFabrics),
            endpointCount: endpoints.length,
            endpoints,
            drift: structuredClone(this.drift),
            driftChecked: this.driftChecked,
            warnings: [...this.warnings],
        };
    }

    endpointMapRefusal(): string | undefined {
        return this.refusal;
    }

    async removeFabric(fabricIndex: number): Promise<RemoveFabricResult> {
        this.removedFabrics.push(fabricIndex);
        return structuredClone(golden.remove_fabric.response.result) as RemoveFabricResult;
    }

    async factoryReset(preserveEndpointNumbers: boolean): Promise<void> {
        this.factoryResets.push(preserveEndpointNumbers);
    }

    async rebuildEndpointMap(): Promise<StatusReport> {
        this.rebuilds += 1;
        this.refusal = undefined;
        return structuredClone(golden.rebuild_endpoint_map.response.result) as StatusReport;
    }

    onDriftDetected(listener: (drift: DriftEntry[]) => void): void {
        this.#drift = listener;
    }

    /** Stand in for the detector finding a moved endpoint number. */
    emitDrift(drift: DriftEntry[]): void {
        this.#drift?.(drift);
    }

    #drift?: (drift: DriftEntry[]) => void;

    onFabricsChanged(listener: (fabrics: FabricInfo[], change: string) => void): void {
        this.#fabrics = listener;
    }

    onCommissioned(listener: () => void): void {
        this.#commissioned = listener;
    }

    onDecommissioned(listener: () => void): void {
        this.#decommissioned = listener;
    }

    /** Stand in for the Matter stack observing a pairing change (§5). */
    emitFabricsChanged(fabrics: FabricInfo[], change: string): void {
        this.#fabrics?.(fabrics, change);
    }

    emitCommissioned(): void {
        this.#commissioned?.();
    }

    emitDecommissioned(): void {
        this.#decommissioned?.();
    }

    #fabrics?: (fabrics: FabricInfo[], change: string) => void;
    #commissioned?: () => void;
    #decommissioned?: () => void;

    async reconcile(endpoints: readonly EndpointSpec[], replaceAll: boolean): Promise<StatusReport> {
        this.model.reconcile(endpoints, replaceAll);
        return this.getStatus();
    }

    async upsertEndpoint(spec: EndpointSpec): Promise<UpsertResult> {
        return this.model.upsert(spec);
    }

    async removeEndpoint(indigoDeviceId: number): Promise<RemoveResult> {
        return this.model.remove(indigoDeviceId);
    }

    async setState(indigoDeviceId: number, states: Record<string, unknown>): Promise<void> {
        this.model.require(indigoDeviceId);
        this.lastStates = states;
    }

    async setReachable(indigoDeviceId: number, reachable: boolean): Promise<void> {
        this.model.require(indigoDeviceId);
        this.lastReachable = reachable;
    }

    lastStates?: Record<string, unknown>;
    lastReachable?: boolean;

    onCommand(listener: (data: CommandEventData) => void): void {
        this.#command = listener;
    }

    /** Stand in for an ecosystem acting on an endpoint. */
    emitCommand(data: CommandEventData): void {
        this.commands.push(data);
        this.#command?.(data);
    }

    getPairing(): PairingReport {
        const source = !this.commissioned
            ? golden.get_pairing_uncommissioned
            : this.windowOpen
              ? golden.get_pairing_commissioned_window_open
              : golden.get_pairing_commissioned;
        return structuredClone(source.response.result) as PairingReport;
    }

    /**
     * §133: the real `getPairingSettled` bounded-retries `getPairing()` on
     * matter.js's transient error. The stub has no Matter stack to race, so
     * it just delegates — the retry/backoff/deadline behaviour itself is
     * unit-tested directly in `test/node.test.ts` against a fake `read`.
     */
    async getPairingSettled(): Promise<PairingReport> {
        return this.getPairing();
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
