/**
 * Role → matter.js endpoint construction, the §4.2 unit conversions, and the
 * cluster-change listeners that turn ecosystem writes into `command` events.
 *
 * Everything Matter-shaped about a *bridged child* lives here: one table entry
 * per role, carrying its device type, its `set_state` writer and its command
 * listeners, so the converter for a role sits next to the cluster it feeds
 * (§4.2's "exactly one converter per role, in the node").
 *
 * E3 implements the relay/dimmer/colour roles. The remaining §4.2 roles
 * (sensors, thermostat, doorLock, windowCovering) are E4 and are refused here
 * rather than silently dropped — see {@link UNSUPPORTED_ROLE_DETAILS}.
 */

import { Endpoint, type EndpointType, Logger } from "@matter/main";
import { BridgedDeviceBasicInformationServer } from "@matter/main/behaviors/bridged-device-basic-information";
import { ColorControlServer } from "@matter/main/behaviors/color-control";
import { ColorControl } from "@matter/main/clusters/color-control";
import { ColorTemperatureLightDevice } from "@matter/main/devices/color-temperature-light";
import { DimmableLightDevice } from "@matter/main/devices/dimmable-light";
import { ExtendedColorLightDevice } from "@matter/main/devices/extended-color-light";
import { OnOffLightDevice } from "@matter/main/devices/on-off-light";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";

import {
    type CommandEventData,
    describeErrorWithStack,
    type EndpointSpec,
    ErrorCode,
    ProtocolError,
    Role,
    type RoleValue,
} from "./protocol.js";

/**
 * Clamping is silent by design at the protocol boundary (§4.2 says the node
 * owns bounds), but a value that had to be clamped is the fingerprint of a
 * plugin-side unit bug, so it is worth a debug line naming what moved.
 */
const logger = Logger.get("indigo-matter-endpoints");

/** Bridged Device Basic Information `UniqueID`, stable across restarts. */
export function uniqueIdFor(indigoDeviceId: number): string {
    return `indigo-${indigoDeviceId}`;
}

/**
 * `Endpoint.id` derivation — the identity key of BRIDGE_PROTOCOL §4.1/§6.3.
 * Deliberately the *same* value as {@link uniqueIdFor}: one derivation means the
 * two can never drift apart, and §6.3's one-way identity flow reads directly.
 * matter.js keys persisted endpoint numbers on this string alone (PRD §4.3), so
 * it must never be reused or mutated.
 */
export const endpointIdFor = uniqueIdFor;

/**
 * `SerialNumber` must differ from `UniqueID` (Matter rejects equal values), so
 * the device id goes in bare here and prefixed there.
 */
export function serialNumberFor(indigoDeviceId: number): string {
    return String(indigoDeviceId);
}

// ---------------------------------------------------------------------------
// §4.2 unit conversions
// ---------------------------------------------------------------------------

/** Matter LevelControl and ColorControl 8-bit ranges. */
export const MATTER_LEVEL_MAX = 254;
/** §4.2: mireds are clamped to the range we advertise as physically supported. */
export const MIREDS_MIN = 153;
export const MIREDS_MAX = 500;
/** Matter `CurrentHue` spans 0-254 for a full 0-360° turn. */
export const HUE_DEGREES_MAX = 360;

function clamp(value: number, min: number, max: number): number {
    return Math.min(max, Math.max(min, value));
}

/**
 * {@link clamp}, but says so. Only the *inbound* clamp of each converter uses
 * this: the outbound one exists to satisfy the type range and can never fire
 * once the input is already in range, so logging it would only ever be noise.
 */
function clampLogged(value: number, min: number, max: number, what: string): number {
    const clamped = clamp(value, min, max);
    if (clamped !== value) {
        logger.debug(`Clamped ${what} ${value} to ${clamped} (supported range ${min}-${max})`);
    }
    return clamped;
}

/**
 * Round half away from zero. `Math.round` is half-*up* (−0.5 → −0), which
 * differs for negatives; every quantity here is non-negative, but naming the
 * rule keeps §4.2's "round-half-up" honest if a signed unit ever arrives.
 */
function roundHalfUp(value: number): number {
    return Math.floor(value + 0.5);
}

/**
 * Indigo 0-100 → Matter 0-254 (§4.2). 0 maps to 0 exactly, so "off" survives
 * the conversion rather than becoming the dimmest on-level.
 */
export function percentToMatter(percent: number): number {
    return clamp(roundHalfUp((clampLogged(percent, 0, 100, "percentage") * MATTER_LEVEL_MAX) / 100), 0, MATTER_LEVEL_MAX);
}

/** Matter 0-254 → Indigo 0-100. Inverse of {@link percentToMatter} for all 101 inputs. */
export function matterToPercent(level: number): number {
    return clamp(roundHalfUp((clamp(level, 0, MATTER_LEVEL_MAX) * 100) / MATTER_LEVEL_MAX), 0, 100);
}

/**
 * The value actually written to `currentLevel`.
 *
 * The Lighting feature constrains `currentLevel` to 1-254 on all four lighting
 * device types (verified against the 0.17.8 device definitions, which `alter`
 * the attribute to `min: 1`), so a literal 0 fails validation. Writing 1
 * instead is lossless at the protocol boundary: {@link matterToPercent}(1) is 0.
 *
 * Note that this does **not** also write OnOff false: {@link levelPatch} never
 * touches the OnOff cluster. Turning a light off at 0% is the plugin's call —
 * it owns the Indigo device's on/off state and sends `onOff` in the same
 * `set_state` when it means it (§4.2 lists them as independent state keys).
 */
export function percentToCurrentLevel(percent: number): number {
    return Math.max(1, percentToMatter(percent));
}

/** §4.2: colour temperature is clamped to the advertised physical bounds. */
export function clampMireds(mireds: number): number {
    return clampLogged(roundHalfUp(mireds), MIREDS_MIN, MIREDS_MAX, "colour temperature (mireds)");
}

/** Indigo hue 0-360° → Matter `CurrentHue` 0-254. */
export function degreesToMatterHue(degrees: number): number {
    return clamp(
        roundHalfUp((clampLogged(degrees, 0, HUE_DEGREES_MAX, "hue (degrees)") * MATTER_LEVEL_MAX) / HUE_DEGREES_MAX),
        0,
        MATTER_LEVEL_MAX,
    );
}

/**
 * Matter `CurrentHue` 0-254 → Indigo hue 0-360°.
 *
 * Not an exact inverse, and cannot be: 361 degrees do not fit in 255 steps, so
 * a round trip is accurate to within one Matter step (~1.42°). Level and
 * saturation *do* round-trip exactly because 254 steps span only 101 values.
 */
export function matterHueToDegrees(hue: number): number {
    return clamp(
        roundHalfUp((clamp(hue, 0, MATTER_LEVEL_MAX) * HUE_DEGREES_MAX) / MATTER_LEVEL_MAX),
        0,
        HUE_DEGREES_MAX,
    );
}

// ---------------------------------------------------------------------------
// The role table
// ---------------------------------------------------------------------------

/** Matter behaviour ids, as they key `Endpoint.state` / `Endpoint.eventsOf`. */
const ON_OFF = "onOff";
const LEVEL_CONTROL = "levelControl";
const COLOR_CONTROL = "colorControl";
const BRIDGED_INFO = "bridgedDeviceBasicInformation";

/**
 * Colour attributes matter.js requires up front.
 *
 * `colorMode`, `enhancedColorMode`, `colorCapabilities`,
 * `coupleColorTempToLevelMinMireds` and `startUpColorTemperatureMireds` are all
 * mandatory-on-conformance for a ColorControl server with the CT feature, and
 * matter.js 0.17.8 refuses to initialise the behaviour without them (verified:
 * the endpoint fails construction with a `conformance` error naming each in
 * turn). Nothing in the matter.js docs says so — the typings mark them
 * optional, which is why they are pinned here with a reason rather than
 * discovered again.
 */
function colorControlDefaults(hueSaturation: boolean): Record<string, unknown> {
    return {
        colorMode: ColorControl.ColorMode.ColorTemperatureMireds,
        enhancedColorMode: ColorControl.EnhancedColorMode.ColorTemperatureMireds,
        colorCapabilities: { colorTemperature: true, xy: true, hueSaturation },
        colorTempPhysicalMinMireds: MIREDS_MIN,
        colorTempPhysicalMaxMireds: MIREDS_MAX,
        coupleColorTempToLevelMinMireds: MIREDS_MIN,
        startUpColorTemperatureMireds: null,
        ...(hueSaturation ? { currentHue: 0, currentSaturation: 0 } : {}),
    };
}

/**
 * The LevelControl state every level-bearing role starts from.
 *
 * `managedTransitionTimeHandling` is pinned `false` rather than left to
 * matter.js's default because the echo guard depends on it. With managed
 * transitions ON, matter.js drives a `moveToLevel` to its target in steps of
 * its own making, and those steps are written by matter.js itself — i.e. in an
 * *offline* context. {@link isEcosystemChange} would read every one of them as
 * our own write and drop it, so an ecosystem dimming a lamp would move the
 * Matter attribute and the plugin would never learn the level changed at all.
 * Unmanaged, the remote `moveToLevel` lands as one attribute change in a remote
 * context, which is exactly what §4.2's `setLevel` is supposed to report.
 *
 * 0.17.8 already defaults it to `false` (`LevelControlServer.State`), so this
 * pin changes no behaviour today — it is here so that a future default flip
 * cannot silently sever the whole `setLevel` command family.
 */
const LEVEL_CONTROL_INITIAL = { currentLevel: 1, managedTransitionTimeHandling: false };

/** The subset of §4.2 roles this bridge version can construct. */
export const SUPPORTED_ROLES: readonly RoleValue[] = [
    Role.onOffPlugInUnit,
    Role.onOffLight,
    Role.dimmableLight,
    Role.colorTemperatureLight,
    Role.extendedColorLight,
];

export function isSupportedRole(role: RoleValue): boolean {
    return SUPPORTED_ROLES.includes(role);
}

/**
 * Why a lawful §4.2 role can still be refused.
 *
 * `unknown_role` would be a lie (the role *is* in the v1 enum) and silently
 * skipping the export would be worse — the user selected the device and would
 * see nothing appear. So it is an `internal` refusal that names the milestone.
 */
export const UNSUPPORTED_ROLE_DETAILS = (role: RoleValue): string =>
    `role ${role} is in the v1 enum but not implemented by this bridge version`;

interface RoleDefinition {
    /** Extra behaviour state supplied at construction, beyond bridged info. */
    initialState: () => Record<string, unknown>;
    /** Build the state patch for a `set_state` (§3.4) in this role's vocabulary. */
    statePatch: (states: Record<string, unknown>) => Record<string, unknown>;
    /** Attribute → `command` event, for changes an ecosystem made (§4.2). */
    watch: readonly WatchSpec[];
    /**
     * The matter.js device type, already `.with()`-ed for a bridged child.
     *
     * Typed as the base {@link EndpointType} on purpose: each role's `.with()`
     * produces a *different* concrete type, and the registry holds them all as
     * plain `Endpoint`s. Keeping the per-role types would buy nothing — the
     * state patches are keyed by behaviour id, which is untyped either way.
     */
    deviceType: () => EndpointType;
}

/** One observable to subscribe: a behaviour id, an attribute, and its command. */
interface WatchSpec {
    behavior: string;
    attribute: string;
    /** Build the §5 `command` payload; `undefined` means "nothing to report". */
    command: (
        value: unknown,
        state: Readonly<Record<string, unknown>>,
    ) => { command: string; args: Record<string, unknown> } | undefined;
}

function numberOr(value: unknown, fallback: number): number {
    return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** `onOff` → §4.2 `onOff {"value": bool}`. Shared by every role that has it. */
const WATCH_ON_OFF: WatchSpec = {
    behavior: ON_OFF,
    attribute: "onOff",
    command: value => ({ command: "onOff", args: { value: value === true } }),
};

/**
 * `currentLevel` → §4.2 `setLevel {"level": 0-100}`. Matter's `moveToLevel`,
 * `step`, `move` and the Lighting on/off transitions all land here as an
 * attribute change, which is why one listener covers the whole command family.
 */
const WATCH_LEVEL: WatchSpec = {
    behavior: LEVEL_CONTROL,
    attribute: "currentLevel",
    command: value =>
        typeof value === "number" ? { command: "setLevel", args: { level: matterToPercent(value) } } : undefined,
};

const WATCH_COLOR_TEMP: WatchSpec = {
    behavior: COLOR_CONTROL,
    attribute: "colorTemperatureMireds",
    command: value =>
        typeof value === "number"
            ? { command: "setColorTemp", args: { colorTempMireds: clampMireds(value) } }
            : undefined,
};

/**
 * Hue and saturation are one §4.2 command (`setColor` carries both), so each
 * observable reads its partner out of the endpoint's current state. Matter
 * changes them in one transaction for `moveToHueAndSaturation`, which yields two
 * events and therefore two `setColor` emissions with the same final values —
 * harmless (the plugin applies the same colour twice) and much simpler than
 * coalescing.
 */
const WATCH_HUE: WatchSpec = {
    behavior: COLOR_CONTROL,
    attribute: "currentHue",
    command: (value, state) => ({
        command: "setColor",
        args: {
            hue: matterHueToDegrees(numberOr(value, 0)),
            saturation: matterToPercent(numberOr(state.currentSaturation, 0)),
        },
    }),
};

const WATCH_SATURATION: WatchSpec = {
    behavior: COLOR_CONTROL,
    attribute: "currentSaturation",
    command: (value, state) => ({
        command: "setColor",
        args: {
            hue: matterHueToDegrees(numberOr(state.currentHue, 0)),
            saturation: matterToPercent(numberOr(value, 0)),
        },
    }),
};

function onOffPatch(states: Record<string, unknown>): Record<string, unknown> {
    return typeof states.onOff === "boolean" ? { [ON_OFF]: { onOff: states.onOff } } : {};
}

function levelPatch(states: Record<string, unknown>): Record<string, unknown> {
    if (typeof states.level !== "number") {
        return {};
    }
    return { [LEVEL_CONTROL]: { currentLevel: percentToCurrentLevel(states.level) } };
}

/**
 * Colour patch, including the `colorMode` bookkeeping Matter requires.
 *
 * An ecosystem reads `colorMode` to decide which attributes to believe. Indigo
 * devices happily report hue/saturation *and* a colour temperature at the same
 * time (the fixture `set_state_extended_color_light` does exactly that), so a
 * rule is needed: hue/saturation win when present, because a user who set a
 * colour expects to see that colour, not the white point left over from before.
 */
function colorPatch(states: Record<string, unknown>, hueSaturation: boolean): Record<string, unknown> {
    const color: Record<string, unknown> = {};
    if (typeof states.colorTempMireds === "number") {
        color.colorTemperatureMireds = clampMireds(states.colorTempMireds);
        color.colorMode = ColorControl.ColorMode.ColorTemperatureMireds;
        color.enhancedColorMode = ColorControl.EnhancedColorMode.ColorTemperatureMireds;
    }
    if (hueSaturation) {
        let touched = false;
        if (typeof states.hue === "number") {
            color.currentHue = degreesToMatterHue(states.hue);
            touched = true;
        }
        if (typeof states.saturation === "number") {
            color.currentSaturation = percentToMatter(states.saturation);
            touched = true;
        }
        if (touched) {
            color.colorMode = ColorControl.ColorMode.CurrentHueAndCurrentSaturation;
            color.enhancedColorMode = ColorControl.EnhancedColorMode.CurrentHueAndCurrentSaturation;
        }
    }
    return Object.keys(color).length > 0 ? { [COLOR_CONTROL]: color } : {};
}

const ROLE_DEFINITIONS: Partial<Record<RoleValue, RoleDefinition>> = {
    [Role.onOffPlugInUnit]: {
        deviceType: () => OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: onOffPatch,
        watch: [WATCH_ON_OFF],
    },
    [Role.onOffLight]: {
        deviceType: () => OnOffLightDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: onOffPatch,
        watch: [WATCH_ON_OFF],
    },
    [Role.dimmableLight]: {
        deviceType: () => DimmableLightDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({ [LEVEL_CONTROL]: { ...LEVEL_CONTROL_INITIAL } }),
        statePatch: states => ({ ...onOffPatch(states), ...levelPatch(states) }),
        watch: [WATCH_ON_OFF, WATCH_LEVEL],
    },
    [Role.colorTemperatureLight]: {
        deviceType: () => ColorTemperatureLightDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({
            [LEVEL_CONTROL]: { ...LEVEL_CONTROL_INITIAL },
            [COLOR_CONTROL]: { ...colorControlDefaults(false), colorTemperatureMireds: MIREDS_MIN },
        }),
        statePatch: states => ({ ...onOffPatch(states), ...levelPatch(states), ...colorPatch(states, false) }),
        watch: [WATCH_ON_OFF, WATCH_LEVEL, WATCH_COLOR_TEMP],
    },
    [Role.extendedColorLight]: {
        // matter.js 0.17.8 builds ExtendedColorLightDevice with
        // `ColorControlServer.with("Xy", "ColorTemperature")` — no HueSaturation
        // — so `currentHue`/`currentSaturation` would not exist and §4.2's
        // hue/saturation vocabulary would be unimplementable. The Matter spec
        // makes HS mandatory for device type 0x010D, so this override restores
        // conformance rather than extending it.
        deviceType: () =>
            ExtendedColorLightDevice.with(
                BridgedDeviceBasicInformationServer,
                ColorControlServer.with("HueSaturation", "Xy", "ColorTemperature"),
            ),
        initialState: () => ({
            [LEVEL_CONTROL]: { ...LEVEL_CONTROL_INITIAL },
            [COLOR_CONTROL]: { ...colorControlDefaults(true), colorTemperatureMireds: MIREDS_MIN },
        }),
        statePatch: states => ({ ...onOffPatch(states), ...levelPatch(states), ...colorPatch(states, true) }),
        watch: [WATCH_ON_OFF, WATCH_LEVEL, WATCH_COLOR_TEMP, WATCH_HUE, WATCH_SATURATION],
    },
};

function definitionFor(role: RoleValue): RoleDefinition {
    const definition = ROLE_DEFINITIONS[role];
    if (definition === undefined) {
        throw new ProtocolError(ErrorCode.internal, UNSUPPORTED_ROLE_DETAILS(role));
    }
    return definition;
}

/**
 * Merge two behaviour-keyed patches one level deep.
 *
 * A plain spread is wrong here and fails loudly: `{...defaults, ...patch}`
 * *replaces* the whole `colorControl` object, dropping the mandatory
 * `colorCapabilities`/`colorTempPhysicalMinMireds`/… that
 * {@link colorControlDefaults} supplies, and matter.js then refuses to
 * construct the endpoint with a bare "Behaviors have errors". Merging per
 * behaviour is what lets a spec's initial `states` coexist with them.
 */
function mergeBehaviors(...patches: Record<string, unknown>[]): Record<string, unknown> {
    const merged: Record<string, Record<string, unknown>> = {};
    for (const patch of patches) {
        for (const [behavior, values] of Object.entries(patch)) {
            merged[behavior] = { ...merged[behavior], ...(values as Record<string, unknown>) };
        }
    }
    return merged;
}

/** The Bridged Device Basic Information every child carries (PRD §5.3). */
function bridgedInfoFor(spec: EndpointSpec, productName: string): Record<string, unknown> {
    return {
        nodeLabel: spec.label,
        productName,
        productLabel: spec.label,
        serialNumber: serialNumberFor(spec.indigoDeviceId),
        uniqueId: uniqueIdFor(spec.indigoDeviceId),
        reachable: spec.reachable,
        // Optional on bridged devices, and matter.js only lets
        // `increaseConfigurationVersion` run when an initial value exists — so
        // it is seeded here rather than discovered as a throw on the first bump.
        configurationVersion: 1,
    };
}

/**
 * Build the bridged child endpoint for one spec. Not yet added to a parent;
 * `Endpoint.id` is fixed at construction and is the identity matter.js keys its
 * persisted endpoint number on.
 */
export function createEndpoint(spec: EndpointSpec, productName: string): Endpoint {
    const definition = definitionFor(spec.role);
    return new Endpoint(definition.deviceType() as never, {
        id: endpointIdFor(spec.indigoDeviceId),
        ...mergeBehaviors(
            { [BRIDGED_INFO]: bridgedInfoFor(spec, productName) },
            definition.initialState(),
            statePatchOrRefuse(spec.role, spec.states),
        ),
    } as never);
}

/**
 * Which of the given keys this role's converter would not consume.
 *
 * Derived by running the converter on one key at a time rather than from a
 * second table of key names and types, because a second table is a thing that
 * can disagree with the first: every §4.2 state key contributes to the patch
 * independently (there is no key that is only meaningful alongside another), so
 * "did the converter produce anything from this key alone" *is* the definition
 * of "consumed", and it cannot drift from {@link RoleDefinition.statePatch}.
 *
 * This catches a misspelt key, a key belonging to another role, and a key of
 * the wrong type (`{"onOff": "true"}`) with the same test — all three are
 * cases the patch builders skip.
 */
export function rejectedStateKeys(role: RoleValue, states: Record<string, unknown>): string[] {
    const { statePatch } = definitionFor(role);
    return Object.keys(states).filter(key => Object.keys(statePatch({ [key]: states[key] })).length === 0);
}

/**
 * Apply a §3.4 `set_state` as a local (offline-context) write.
 *
 * An empty `states` is a lawful no-op and succeeds. Keys the role does not
 * speak are a different thing entirely: answering `{}` to
 * `{"level": 55}` on an `onOffLight` reports success for a write that never
 * happened, and the symptom the user sees — the ecosystem showing stale state —
 * is indistinguishable from a bridge that is simply down. So a `states` that
 * yields *nothing at all* is `malformed_args` naming the keys that were
 * dropped.
 *
 * A *partly* consumed `states` still succeeds, and that asymmetry is deliberate:
 * the plugin and the node version-skew across a release (PM-B keeps an old node
 * running), so a newer plugin sending a key this bridge does not know yet
 * alongside keys it does must still apply the ones it understands.
 */
export async function applyStates(
    endpoint: Endpoint,
    role: RoleValue,
    states: Record<string, unknown>,
): Promise<void> {
    const patch = statePatchOrRefuse(role, states);
    if (Object.keys(patch).length === 0) {
        return;
    }
    await endpoint.set(patch as never);
}

/**
 * The role's state patch, refusing a `states` that produced nothing from
 * something. Shared by {@link applyStates} and {@link createEndpoint} so a set
 * of keys refused on update is not quietly accepted on create — an `attach`
 * reconcile runs both paths and they must agree on what is lawful.
 */
function statePatchOrRefuse(role: RoleValue, states: Record<string, unknown>): Record<string, unknown> {
    const patch = definitionFor(role).statePatch(states);
    if (Object.keys(states).length > 0 && Object.keys(patch).length === 0) {
        throw new ProtocolError(
            ErrorCode.malformedArgs,
            `role ${role} consumed none of the states given; ` +
                `rejected key(s): ${rejectedStateKeys(role, states).join(", ")} (§4.2)`,
        );
    }
    return patch;
}

/** §3.5 / PRD §5.3 — `Reachable` tracks the Indigo device's enabled state. */
export async function applyReachable(endpoint: Endpoint, reachable: boolean): Promise<void> {
    await endpoint.set({ [BRIDGED_INFO]: { reachable } } as never);
}

/** §4.1 — `NodeLabel` follows the export's display name. */
export async function applyLabel(endpoint: Endpoint, label: string): Promise<void> {
    await endpoint.set({ [BRIDGED_INFO]: { nodeLabel: label, productLabel: label } } as never);
}

/**
 * True when a cluster change came from our own `set_state` rather than from an
 * ecosystem (§6.4). matter.js runs local agent writes in a `LocalActorContext`,
 * whose `offline` is the literal `true`; a remote (network) action carries a
 * `RemoteActorContext` with `offline` absent or `false`.
 *
 * **Assumes matter.js is not managing transitions.** Anything matter.js writes
 * on its own behalf — notably the intermediate steps of a managed LevelControl
 * transition — is offline too, and would be swallowed here as if we had written
 * it. That is why {@link LEVEL_CONTROL_INITIAL} pins
 * `managedTransitionTimeHandling: false`: the guard can only tell "us" from
 * "them" while matter.js itself is not a third writer.
 *
 * Exported because it is the whole echo guard, and a pure predicate is testable
 * without persuading a real Matter stack to originate a remote write.
 */
export function isEcosystemChange(context: unknown): boolean {
    return (context as { offline?: boolean } | undefined)?.offline !== true;
}

/**
 * Subscribe the role's cluster-change listeners and return their teardown.
 *
 * The teardown matters: a removed endpoint whose observables still hold our
 * handler would keep emitting `command` events for a device the plugin no
 * longer exports, and would keep the closure (and the endpoint) alive.
 *
 * Throws if a watched observable is missing. That is a role-definition contract
 * violation — this table and the device type it names have to agree — and the
 * alternative is worse than a crash: skipping the subscription leaves the
 * accessory fully controllable in the ecosystem while every command it produces
 * goes nowhere, with nothing in any log to say so. Failing at construction
 * means any endpoint-building test catches a matter.js rename or a feature
 * regression the first time it runs.
 */
export function watchCommands(
    endpoint: Endpoint,
    spec: { indigoDeviceId: number; role: RoleValue },
    emit: (data: CommandEventData) => void,
    log: (message: string) => void = () => {},
): () => void {
    const teardown: (() => void)[] = [];

    for (const watch of definitionFor(spec.role).watch) {
        const events = endpoint.eventsOf(watch.behavior) as Record<
            string,
            { on(handler: (...args: unknown[]) => void): void; off(handler: (...args: unknown[]) => void): void }
        >;
        const observable = events[`${watch.attribute}$Changed`];
        if (observable === undefined) {
            throw new ProtocolError(
                ErrorCode.internal,
                `endpoint ${spec.indigoDeviceId} (${spec.role}): matter.js exposes no ` +
                    `${watch.behavior}.${watch.attribute}$Changed observable, so this role's command family ` +
                    "could never be reported",
            );
        }
        const handler = (...args: unknown[]): void => {
            // Everything below runs *inside* matter.js's observable, which is
            // itself inside the commit of a remote write. A throw here does not
            // fail the write — it escapes as an unhandled rejection, and this
            // process exits on those, so one bad payload would take the whole
            // bridge down and every exported accessory with it. Mirrors the
            // fabricsChanged hardening in node.ts: log, never rethrow.
            try {
                const [value, , context] = args;
                if (!isEcosystemChange(context)) {
                    return;
                }
                const state = endpoint.stateOf(watch.behavior) as Readonly<Record<string, unknown>>;
                const command = watch.command(value, state);
                if (command === undefined) {
                    return;
                }
                emit({ indigoDeviceId: spec.indigoDeviceId, command: command.command, args: command.args });
            } catch (error) {
                try {
                    log(
                        `Dropped a ${watch.behavior}.${watch.attribute} change for endpoint ` +
                            `${spec.indigoDeviceId}: ${describeErrorWithStack(error)}`,
                    );
                } catch {
                    // The logger itself failed; there is nowhere left to report.
                }
            }
        };
        observable.on(handler);
        teardown.push(() => observable.off(handler));
    }

    return () => {
        for (const off of teardown) {
            off();
        }
        teardown.length = 0;
    };
}
