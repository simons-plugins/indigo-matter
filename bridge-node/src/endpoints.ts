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

import { Endpoint, type EndpointType } from "@matter/main";
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
    type EndpointSpec,
    ErrorCode,
    ProtocolError,
    Role,
    type RoleValue,
} from "./protocol.js";

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
    return clamp(roundHalfUp((clamp(percent, 0, 100) * MATTER_LEVEL_MAX) / 100), 0, MATTER_LEVEL_MAX);
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
 * Zero brightness is expressed to ecosystems as OnOff = false, which is what
 * the Lighting feature intends.
 */
export function percentToCurrentLevel(percent: number): number {
    return Math.max(1, percentToMatter(percent));
}

/** §4.2: colour temperature is clamped to the advertised physical bounds. */
export function clampMireds(mireds: number): number {
    return clamp(roundHalfUp(mireds), MIREDS_MIN, MIREDS_MAX);
}

/** Indigo hue 0-360° → Matter `CurrentHue` 0-254. */
export function degreesToMatterHue(degrees: number): number {
    return clamp(
        roundHalfUp((clamp(degrees, 0, HUE_DEGREES_MAX) * MATTER_LEVEL_MAX) / HUE_DEGREES_MAX),
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
        initialState: () => ({ [LEVEL_CONTROL]: { currentLevel: 1 } }),
        statePatch: states => ({ ...onOffPatch(states), ...levelPatch(states) }),
        watch: [WATCH_ON_OFF, WATCH_LEVEL],
    },
    [Role.colorTemperatureLight]: {
        deviceType: () => ColorTemperatureLightDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({
            [LEVEL_CONTROL]: { currentLevel: 1 },
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
            [LEVEL_CONTROL]: { currentLevel: 1 },
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
            definition.statePatch(spec.states),
        ),
    } as never);
}

/** Apply a §3.4 `set_state` as a local (offline-context) write. */
export async function applyStates(
    endpoint: Endpoint,
    role: RoleValue,
    states: Record<string, unknown>,
): Promise<void> {
    const patch = definitionFor(role).statePatch(states);
    if (Object.keys(patch).length === 0) {
        return;
    }
    await endpoint.set(patch as never);
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
 */
export function watchCommands(
    endpoint: Endpoint,
    spec: { indigoDeviceId: number; role: RoleValue },
    emit: (data: CommandEventData) => void,
): () => void {
    const teardown: (() => void)[] = [];

    for (const watch of definitionFor(spec.role).watch) {
        const events = endpoint.eventsOf(watch.behavior) as Record<
            string,
            { on(handler: (...args: unknown[]) => void): void; off(handler: (...args: unknown[]) => void): void }
        >;
        const observable = events[`${watch.attribute}$Changed`];
        if (observable === undefined) {
            continue;
        }
        const handler = (...args: unknown[]): void => {
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
