/**
 * Argument parsing and the reconcile planner for the endpoint-CRUD commands
 * (BRIDGE_PROTOCOL §3.1-§3.5, §4.1).
 *
 * Deliberately matter.js-free, like `protocol.ts`: every §1.1 refusal the
 * endpoint commands can produce — `malformed_args`, `unknown_role`,
 * `mass_removal_refused` — is decided here, so the decisions are unit-testable
 * without a Matter stack and the WebSocket server and the test double reach the
 * same verdicts from the same code.
 */

import {
    type EndpointSpec,
    ErrorCode,
    INTENT_REPLACE_ALL,
    isRole,
    ProtocolError,
    type RoleValue,
} from "./protocol.js";

/** True for a plain JSON object — the shape `states`/`options` must have. */
function isStruct(value: unknown): value is Record<string, unknown> {
    return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireStruct(value: unknown, what: string): Record<string, unknown> {
    if (value === undefined) {
        return {};
    }
    if (!isStruct(value)) {
        throw new ProtocolError(ErrorCode.malformedArgs, `${what} must be an object`);
    }
    return value;
}

/**
 * §1.1 gate for the identity key. Every endpoint command carries one, and it is
 * always the same refusal, so it is one function rather than four copies.
 */
export function parseDeviceId(value: unknown): number {
    if (typeof value !== "number" || !Number.isInteger(value)) {
        throw new ProtocolError(ErrorCode.malformedArgs, "indigoDeviceId must be an integer");
    }
    return value;
}

/**
 * Parse one wire `EndpointSpec` (§4.1).
 *
 * A role outside the §4.2 enum is `unknown_role`, not `malformed_args`: §1.1
 * gives it its own code precisely so the plugin can tell "you sent nonsense"
 * from "this node is older than your role vocabulary".
 */
export function parseEndpointSpec(value: unknown): EndpointSpec {
    if (!isStruct(value)) {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoint must be an object");
    }
    const indigoDeviceId = parseDeviceId(value.indigoDeviceId);
    if (typeof value.role !== "string") {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoint.role must be a string");
    }
    if (!isRole(value.role)) {
        throw new ProtocolError(ErrorCode.unknownRole, `role ${value.role} is not in the v1 role enum (§4.2)`);
    }
    if (typeof value.label !== "string") {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoint.label must be a string");
    }
    // `reachable` defaults to true: an omitted flag means "nothing said about
    // availability", and an accessory that greys itself out by default would be
    // the worse reading.
    const reachable = value.reachable === undefined ? true : value.reachable;
    if (typeof reachable !== "boolean") {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoint.reachable must be a boolean");
    }
    return {
        indigoDeviceId,
        role: value.role,
        label: value.label,
        reachable,
        states: requireStruct(value.states, "endpoint.states"),
        options: requireStruct(value.options, "endpoint.options"),
    };
}

/** Parse the `endpoints` array of an `attach` (§3.1). */
export function parseEndpointSpecs(value: unknown): EndpointSpec[] {
    if (value === undefined) {
        return [];
    }
    if (!Array.isArray(value)) {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoints must be an array");
    }
    const specs = value.map(parseEndpointSpec);
    const seen = new Set<number>();
    for (const spec of specs) {
        if (seen.has(spec.indigoDeviceId)) {
            throw new ProtocolError(
                ErrorCode.malformedArgs,
                `endpoints contains indigoDeviceId ${spec.indigoDeviceId} twice`,
            );
        }
        seen.add(spec.indigoDeviceId);
    }
    return specs;
}

/** §3.9: the fabric to drop. `fabricIndex` is a 1-based Matter fabric index. */
export function parseFabricIndex(value: unknown): number {
    if (typeof value !== "number" || !Number.isInteger(value) || value < 1) {
        throw new ProtocolError(
            ErrorCode.malformedArgs,
            `fabricIndex must be a positive integer, got ${JSON.stringify(value)}`,
        );
    }
    return value;
}

/**
 * §3.10: whether a factory reset keeps `endpoint-map.json`.
 *
 * Absent means `true` — the §6.6 rule that destructive operations cannot happen
 * as a default. Discarding endpoint identity is the destructive half of this
 * command, so it takes an explicit `false`; a client that omits the flag gets
 * the reset it asked for and keeps the identities it did not mention.
 */
export function parsePreserveEndpointNumbers(value: unknown): boolean {
    if (value === undefined) {
        return true;
    }
    if (typeof value !== "boolean") {
        throw new ProtocolError(ErrorCode.malformedArgs, "preserveEndpointNumbers must be a boolean");
    }
    return value;
}

/** §3.1: the opt-in that makes emptying the endpoint set deliberate. */
export function parseReplaceAll(intent: unknown): boolean {
    if (intent === undefined) {
        return false;
    }
    if (typeof intent !== "string") {
        throw new ProtocolError(ErrorCode.malformedArgs, "intent must be a string");
    }
    return intent === INTENT_REPLACE_ALL;
}

/**
 * What an `attach` reconcile has to do to the live endpoint set.
 *
 * `recreate` is the role-change case: §4.1 rejects a role change through
 * `upsert_endpoint`, but `attach` is a *full reconcile* from the peer that owns
 * the export set (§6.2), and failing the whole attach would leave the plugin no
 * way to correct a role at all. So attach removes and re-adds instead. It is
 * still an accessory-identity change in every paired ecosystem, hence its own
 * bucket and its own log line rather than hiding inside `update`.
 */
export interface ReconcilePlan {
    create: EndpointSpec[];
    update: EndpointSpec[];
    recreate: EndpointSpec[];
    remove: number[];
}

/**
 * Diff the desired set against the live one, enforcing the §3.1 mass-removal
 * guard.
 *
 * The guard is about the *effect*, not the literal empty array: an `attach`
 * carrying a completely disjoint device set would also remove every live
 * endpoint, and that is just as much "every exported accessory disappears from
 * every paired ecosystem" as `endpoints: []` is.
 */
export function planReconcile(
    live: ReadonlyMap<number, RoleValue>,
    desired: readonly EndpointSpec[],
    replaceAll: boolean,
): ReconcilePlan {
    const plan: ReconcilePlan = { create: [], update: [], recreate: [], remove: [] };
    const desiredIds = new Set<number>();

    for (const spec of desired) {
        desiredIds.add(spec.indigoDeviceId);
        const liveRole = live.get(spec.indigoDeviceId);
        if (liveRole === undefined) {
            plan.create.push(spec);
        } else if (liveRole === spec.role) {
            plan.update.push(spec);
        } else {
            plan.recreate.push(spec);
        }
    }

    for (const indigoDeviceId of live.keys()) {
        if (!desiredIds.has(indigoDeviceId)) {
            plan.remove.push(indigoDeviceId);
        }
    }

    // A `recreate` is not a removal: the device stays exported, it just changes
    // accessory type. Counting it as one would refuse a lawful role correction
    // on a single-export bridge.
    const survivors = live.size - plan.remove.length;
    if (live.size > 0 && survivors === 0 && !replaceAll) {
        throw new ProtocolError(
            ErrorCode.massRemovalRefused,
            `attach would remove all ${live.size} live endpoints without intent: ${INTENT_REPLACE_ALL}`,
        );
    }

    return plan;
}
