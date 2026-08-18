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
    parsePublishedId,
    ProtocolError,
    PUBLISHED_ID_MAX,
    publishedIdFor,
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
    // `battery` defaults to FALSE, the opposite of `reachable` — absent means
    // "no evidence this device has a battery", not "nothing said". Defaulting
    // it true would publish an unevidenced PowerSource cluster on every
    // accessory a client forgot to declare, which no paired ecosystem could
    // ever un-see (§4.1: the cluster set is monotonic).
    const battery = value.battery === undefined ? false : value.battery;
    if (typeof battery !== "boolean") {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoint.battery must be a boolean");
    }
    // Issues #219/#240 — `publishedAs` defaults to today's derivation, never to
    // a bare absence: every `EndpointSpec` in memory has a real published
    // identity, so nothing downstream has to ask "or is it the default?"
    // Validated with the same function that builds it, so a client cannot ask
    // for an identity this bridge would refuse to construct later.
    const publishedAs = value.publishedAs === undefined ? publishedIdFor(indigoDeviceId) : value.publishedAs;
    if (typeof publishedAs !== "string") {
        throw new ProtocolError(ErrorCode.malformedArgs, "endpoint.publishedAs must be a string");
    }
    if (parsePublishedId(publishedAs) === undefined) {
        throw new ProtocolError(
            ErrorCode.malformedArgs,
            `endpoint.publishedAs "${publishedAs}" must be indigo-<deviceId> or indigo-<deviceId>~<generation ` +
                `≥ 2>, at most ${PUBLISHED_ID_MAX} characters (§1.1)`,
        );
    }
    return {
        indigoDeviceId,
        role: value.role,
        label: value.label,
        reachable,
        states: requireStruct(value.states, "endpoint.states"),
        options: requireStruct(value.options, "endpoint.options"),
        battery,
        publishedAs,
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
    // Issue #219/#240's E12 — two exports must never claim the same published
    // identity, the backstop for a hand-edited `.indiPref` pointing two
    // devices at one accessory. A separate set from `seen` (indigoDeviceId):
    // the two collisions are different mistakes and deserve their own message.
    const seenPublishedAs = new Set<string>();
    for (const spec of specs) {
        if (seen.has(spec.indigoDeviceId)) {
            throw new ProtocolError(
                ErrorCode.malformedArgs,
                `endpoints contains indigoDeviceId ${spec.indigoDeviceId} twice`,
            );
        }
        seen.add(spec.indigoDeviceId);
        if (seenPublishedAs.has(spec.publishedAs)) {
            throw new ProtocolError(
                ErrorCode.malformedArgs,
                `endpoints contains publishedAs ${spec.publishedAs} twice`,
            );
        }
        seenPublishedAs.add(spec.publishedAs);
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
 * `recreate` is the in-place case: an identity present in both sets whose
 * shape changed — a battery gain, or (the pre-#240 version-skew fallback, §1.3)
 * a role change from a plugin that never bumped `publishedAs`. §4.1 rejects
 * both through `upsert_endpoint`, but `attach` is a *full reconcile* from the
 * peer that owns the export set (§6.2), and failing the whole attach would
 * leave the plugin no way to correct either at all. So attach removes and
 * re-adds instead, at the SAME `Endpoint.id` — still an accessory-identity
 * change in every paired ecosystem, hence its own bucket and its own log line
 * rather than hiding inside `update`.
 *
 * A role change that DOES bump `publishedAs` (issue #240's normal path, an
 * up-to-date plugin) never reaches `recreate` at all: the old identity simply
 * is not in `desired` (it fails `remove`) and the new one is not in `live`
 * (it falls to `create`) — a supersede, planned as removal-plus-create because
 * matter.js hands the new `Endpoint.id` a fresh number rather than reusing the
 * retired one (F1).
 */
export interface ReconcilePlan {
    create: EndpointSpec[];
    update: EndpointSpec[];
    recreate: EndpointSpec[];
    /** Published identities (`publishedAs`), not device ids — issues #219/#240. */
    remove: string[];
}

/** What the planner needs to know about one live endpoint (§4.1/§4.2). */
export interface LiveComposition {
    /**
     * The device driving this published identity — issues #219/#240. `live`
     * is now keyed on `publishedAs`, so this is how the planner (and the
     * mass-removal guard) still knows which removal and which create belong
     * to the same Indigo device.
     */
    indigoDeviceId: number;
    role: RoleValue;
    /**
     * Whether the LIVE endpoint currently carries PowerSource. Compared against
     * the desired spec's own {@link EndpointSpec.battery} below — a GAIN
     * recreates (the cluster set can only be added to at construction, never
     * patched on), a LOSS is left as an ordinary update (§4.1: monotonic).
     */
    battery: boolean;
}

/**
 * Diff the desired set against the live one, enforcing the §3.1 mass-removal
 * guard.
 *
 * Keyed on `publishedAs` (issues #219/#240), not `indigoDeviceId`: the same
 * device can drive different published identities over time (a role-change
 * supersede bumps the generation), and the reconcile has to tell "this
 * identity's shape changed" (`recreate`, same key) apart from "this identity
 * was retired and a new one for the same device took its place" (`remove` +
 * `create`, two different keys).
 *
 * The guard is about the *effect*, not the literal empty array: an `attach`
 * carrying a completely disjoint device set would also remove every live
 * endpoint, and that is just as much "every exported accessory disappears from
 * every paired ecosystem" as `endpoints: []` is.
 */
export function planReconcile(
    live: ReadonlyMap<string, LiveComposition>,
    desired: readonly EndpointSpec[],
    replaceAll: boolean,
): ReconcilePlan {
    const plan: ReconcilePlan = { create: [], update: [], recreate: [], remove: [] };
    const desiredPublishedAs = new Set<string>();

    for (const spec of desired) {
        desiredPublishedAs.add(spec.publishedAs);
        const liveEndpoint = live.get(spec.publishedAs);
        if (liveEndpoint === undefined) {
            plan.create.push(spec);
        } else if (liveEndpoint.role === spec.role && !(spec.battery && !liveEndpoint.battery)) {
            plan.update.push(spec);
        } else {
            plan.recreate.push(spec);
        }
    }

    for (const publishedAs of live.keys()) {
        if (!desiredPublishedAs.has(publishedAs)) {
            plan.remove.push(publishedAs);
        }
    }

    // A `recreate` is not a removal: the identity stays live, it just changes
    // shape in place. Counting it as one would refuse a lawful role correction
    // on a single-export bridge.
    //
    // Mass-removal guard fix (#240): a supersede is one `remove` plus one
    // `create` for the SAME `indigoDeviceId` under two DIFFERENT `publishedAs`
    // keys — the device stays exported, so it must not count as a departure
    // either. `supersededCount` is how many removed identities have a sibling
    // create for the same device; without it, changing the role of the only
    // exported device on a bridge would be refused as emptying the live set.
    const createdDeviceIds = new Set(plan.create.map(spec => spec.indigoDeviceId));
    const supersededCount = plan.remove.filter(publishedAs => {
        const removedDeviceId = live.get(publishedAs)?.indigoDeviceId;
        return removedDeviceId !== undefined && createdDeviceIds.has(removedDeviceId);
    }).length;
    const survivors = live.size - plan.remove.length + supersededCount;
    if (live.size > 0 && survivors === 0 && !replaceAll) {
        throw new ProtocolError(
            ErrorCode.massRemovalRefused,
            `attach would remove all ${live.size} live endpoints without intent: ${INTENT_REPLACE_ALL}`,
        );
    }

    return plan;
}
