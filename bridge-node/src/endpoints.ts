/**
 * Role → matter.js endpoint construction, the §4.2 unit conversions, and the
 * cluster-change listeners that turn ecosystem writes into `command` events.
 *
 * Everything Matter-shaped about a *bridged child* lives here: one table entry
 * per role, carrying its device type, its `set_state` writer and its command
 * listeners, so the converter for a role sits next to the cluster it feeds
 * (§4.2's "exactly one converter per role, in the node").
 *
 * E3 implemented the relay/dimmer/colour roles; **E4 completes the v1 table**
 * with the sensors, `thermostat`, `doorLock` and `windowCovering`. Every §4.2
 * role now has a definition, and {@link SUPPORTED_ROLES} is derived from the
 * table rather than hand-kept, so the {@link UNSUPPORTED_ROLE_DETAILS} refusal
 * survives only as the gate a *future* enum addition would trip.
 *
 * Two kinds of role live here, and they use different machinery:
 *
 * * **Attribute-driven** roles (everything E3 built, plus every sensor and the
 *   thermostat) report ecosystem intent through {@link WatchSpec}s on the
 *   cluster attributes an ecosystem writes.
 * * **Invocation-driven** roles (`doorLock`, `windowCovering`) cannot: their
 *   ecosystem-facing surface is *commands*, and matter.js's stock servers
 *   answer those by writing the state themselves. Both are overridden here so
 *   the write never happens — see {@link IndigoDoorLockServer} and
 *   {@link IndigoWindowCoveringServer} — and they reach their `command` sink
 *   through {@link COMMAND_SINKS}.
 */

import { Endpoint, type EndpointType, Logger, type MaybePromise } from "@matter/main";
import { BridgedDeviceBasicInformationServer } from "@matter/main/behaviors/bridged-device-basic-information";
import { ColorControlServer } from "@matter/main/behaviors/color-control";
import { DoorLockServer } from "@matter/main/behaviors/door-lock";
import { OccupancySensingServer } from "@matter/main/behaviors/occupancy-sensing";
import { ThermostatServer } from "@matter/main/behaviors/thermostat";
import { MovementDirection, MovementType, WindowCoveringServer } from "@matter/main/behaviors/window-covering";
import { ColorControl } from "@matter/main/clusters/color-control";
import { DoorLock } from "@matter/main/clusters/door-lock";
import { Thermostat } from "@matter/main/clusters/thermostat";
import { ColorTemperatureLightDevice } from "@matter/main/devices/color-temperature-light";
import { ContactSensorDevice } from "@matter/main/devices/contact-sensor";
import { DimmableLightDevice } from "@matter/main/devices/dimmable-light";
import { DoorLockDevice } from "@matter/main/devices/door-lock";
import { ExtendedColorLightDevice } from "@matter/main/devices/extended-color-light";
import { FlowSensorDevice } from "@matter/main/devices/flow-sensor";
import { HumiditySensorDevice } from "@matter/main/devices/humidity-sensor";
import { LightSensorDevice } from "@matter/main/devices/light-sensor";
import { OccupancySensorDevice } from "@matter/main/devices/occupancy-sensor";
import { OnOffLightDevice } from "@matter/main/devices/on-off-light";
import { OnOffPlugInUnitDevice } from "@matter/main/devices/on-off-plug-in-unit";
import { PressureSensorDevice } from "@matter/main/devices/pressure-sensor";
import { TemperatureSensorDevice } from "@matter/main/devices/temperature-sensor";
import { ThermostatDevice } from "@matter/main/devices/thermostat";
import { WindowCoveringDevice } from "@matter/main/devices/window-covering";

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
// §4.2 unit conversions — E4 (sensors, thermostat, covering)
//
// Every scale below was read off the matter.js 0.17.8 typings rather than the
// Matter spec PDF, because two of them are NOT what the §4.2 key names suggest:
// `pressureKPa` is carried on the wire in **0.1 kPa** and `flowM3h` in
// **0.1 m³/h** ("MeasuredValue = 10 x value"), while temperature and humidity
// use the 100x scale. Getting those two backwards is a factor-of-ten error that
// still renders plausibly in an ecosystem, so the constants are named for the
// multiplier and the doc text is quoted at each one.
// ---------------------------------------------------------------------------

/** Matter's 100x scale: 0.01 °C (temperature) and 0.01 % (relative humidity). */
export const CENTI = 100;
/** Matter's 10x scale: 0.1 kPa (pressure) and 0.1 m³/h (flow). */
export const DECI = 10;

const INT16_MIN = -32768;
const INT16_MAX = 32767;
const UINT16_MAX = 65535;

/**
 * TemperatureMeasurement/Thermostat floor. The model's own
 * `MinMeasuredValue` default is -27315, i.e. -273.15 °C — which is also the
 * only place the 0.01 °C unit is stated at all, since the `.d.ts` doc comment
 * for `measuredValue` gives no unit.
 */
export const TEMPERATURE_CENTI_MIN = -27315;

/** RelativeHumidityMeasurement: "MeasuredValue = 100 x water content", 0-10000. */
export const HUMIDITY_CENTI_MAX = 10_000;

/**
 * IlluminanceMeasurement's measurable band.
 *
 * "MeasuredValue = 10,000 x log10(illuminance) + 1, where 1 lx <= illuminance
 * <= 3.576 Mlx, corresponding to a MeasuredValue in the range 1 to 0xFFFE …
 * 0 indicates a value of illuminance that is too low to be measured."
 */
export const ILLUMINANCE_MIN = 1;
export const ILLUMINANCE_MAX = 0xfffe;
/** The lux reading below which Matter's own answer is the 0 sentinel, not a value. */
export const ILLUMINANCE_LUX_MIN = 1;

/**
 * WindowCovering percent100ths. **0 is fully OPEN and 10000 is fully CLOSED** —
 * the exact inverse of §4.2's `position`, where 100 is open. See
 * {@link positionToPercent100ths}.
 */
export const PERCENT100THS_OPEN = 0;
export const PERCENT100THS_CLOSED = 10_000;

/** §4.2 `temperatureC`/`localTemperatureC`/setpoints → Matter 0.01 °C. */
export function celsiusToMatter(celsius: number): number {
    return clamp(roundHalfUp(celsius * CENTI), TEMPERATURE_CENTI_MIN, INT16_MAX);
}

/** Matter 0.01 °C → °C. Exact for every value {@link celsiusToMatter} produces. */
export function matterToCelsius(centi: number): number {
    return centi / CENTI;
}

/** §4.2 `humidityPct` → Matter 0.01 %, clamped to the cluster's 0-100 % domain. */
export function humidityToMatter(percent: number): number {
    return clamp(roundHalfUp(clampLogged(percent, 0, 100, "humidity (%)") * CENTI), 0, HUMIDITY_CENTI_MAX);
}

/** Matter 0.01 % → %. */
export function matterToHumidity(centi: number): number {
    return centi / CENTI;
}

/**
 * §4.2 `lux` → IlluminanceMeasurement's logarithmic `measuredValue`.
 *
 * Sub-lux readings become the literal 0 the cluster defines as "too low to be
 * measured" rather than being clamped up to 1: log10 of 0 is -Infinity, and 1
 * would be a claim of exactly 1 lx that the sensor never made. Above the band
 * the value clamps to 0xFFFE (≈3.576 Mlx), which no indoor sensor reaches.
 */
export function luxToMatter(lux: number): number {
    if (!(lux >= ILLUMINANCE_LUX_MIN)) {
        return 0;
    }
    return clamp(roundHalfUp(10_000 * Math.log10(lux) + 1), ILLUMINANCE_MIN, ILLUMINANCE_MAX);
}

/**
 * The inverse of {@link luxToMatter}, for the round-trip property test and for
 * anyone reading a captured frame. 0 has no lux to return — it is the
 * "too dark to measure" sentinel — so it maps to 0, not to 10^(-1/10000).
 */
export function matterToLux(measured: number): number {
    if (measured <= 0) {
        return 0;
    }
    return 10 ** ((clamp(measured, ILLUMINANCE_MIN, ILLUMINANCE_MAX) - 1) / 10_000);
}

/**
 * §4.2 `pressureKPa` → PressureMeasurement's **0.1 kPa** `measuredValue`
 * ("Indicates the pressure in kPa as follows: MeasuredValue = 10 x Pressure
 * [kPa]"). 0.1 kPa is 1 hPa, which is why the inbound handler
 * (`matter_handlers/sensors.py`) can call its own conversion an identity while
 * this one is a multiply — inbound reports hPa, §4.2 states kPa.
 */
export function kilopascalsToMatter(kilopascals: number): number {
    return clamp(roundHalfUp(kilopascals * DECI), INT16_MIN, INT16_MAX);
}

/** Matter 0.1 kPa → kPa. */
export function matterToKilopascals(deci: number): number {
    return deci / DECI;
}

/**
 * §4.2 `flowM3h` → FlowMeasurement's **0.1 m³/h** `measuredValue` ("Indicates
 * the flow in m^3/h as follows: MeasuredValue = 10 x Flow"). The attribute is
 * `uint16`, so a negative flow is not representable and clamps to 0.
 */
export function cubicMetresPerHourToMatter(flow: number): number {
    return clamp(roundHalfUp(flow * DECI), 0, UINT16_MAX);
}

/** Matter 0.1 m³/h → m³/h. */
export function matterToCubicMetresPerHour(deci: number): number {
    return deci / DECI;
}

/**
 * §4.2 `position` (0-100, **100 = open**) → Matter `…LiftPercent100ths`
 * (0 = fully open, 10000 = fully closed).
 *
 * **This is an inversion, and it is the single most dangerous line in E4.** The
 * two conventions are exact opposites and both are "percent", so a missing
 * inversion produces a blind that is fully shut when the ecosystem says open
 * and vice versa — a bug that looks like wiring, not like software. Verified
 * against the 0.17.8 typings rather than assumed: `upOrOpen()` documents
 * "TargetPositionLiftPercent100ths attribute shall be set to 0.00%",
 * `downOrClose()` documents 100.00%, and the server's own constants are
 * `WC_PERCENT100THS_MIN_OPEN = 0` / `WC_PERCENT100THS_MAX_CLOSED = 1e4`.
 *
 * §4.2's `position` is the *inbound* convention (`matter_handlers/
 * window_covering.py` maps the same Matter attribute onto an Indigo brightness
 * where 100 is open), so both directions of this plugin agree on what 100
 * means. Per-export polarity (`options.invert`) is applied **plugin-side**,
 * before the value reaches this protocol: by the time a `position` is on the
 * wire it always means "100 = open", whatever the physical device does.
 */
export function positionToPercent100ths(position: number): number {
    return (100 - clampLogged(position, 0, 100, "covering position")) * 100;
}

/** Matter `…LiftPercent100ths` → §4.2 `position` (100 = open). Inverse of the above. */
export function percent100thsToPosition(percent100ths: number): number {
    return clamp(100 - roundHalfUp(clamp(percent100ths, PERCENT100THS_OPEN, PERCENT100THS_CLOSED) / 100), 0, 100);
}

/**
 * §4.2 `systemMode` ⇄ Matter `SystemModeEnum`, the mapping §4.2 says the node
 * owns. `Auto` is 1 and there is **no 2** in the enum, so the table is written
 * out rather than derived from an index.
 */
const SYSTEM_MODE_TO_MATTER: Record<string, Thermostat.SystemMode> = {
    off: Thermostat.SystemMode.Off,
    heat: Thermostat.SystemMode.Heat,
    cool: Thermostat.SystemMode.Cool,
    auto: Thermostat.SystemMode.Auto,
};

/**
 * The reverse table.
 *
 * Deliberately **not** total over `SystemModeEnum`: an ecosystem may write
 * `EmergencyHeat`, `Precooling`, `FanOnly`, `Dry` or `Sleep`, none of which are
 * in §4.2's four-value domain. Those become `undefined` here and the watcher
 * reports nothing, which is the honest answer — inventing "heat" for
 * `EmergencyHeat` would have the plugin tell Indigo something the user did not
 * ask for.
 */
const MATTER_TO_SYSTEM_MODE = new Map<number, string>(
    Object.entries(SYSTEM_MODE_TO_MATTER).map(([mode, value]) => [value as number, mode]),
);

/** §4.2 `systemMode` string → the Matter enum, or `undefined` if not in the domain. */
export function systemModeToMatter(mode: unknown): Thermostat.SystemMode | undefined {
    return typeof mode === "string" ? SYSTEM_MODE_TO_MATTER[mode] : undefined;
}

/** Matter `SystemModeEnum` → §4.2 `systemMode`, or `undefined` outside the domain. */
export function matterToSystemMode(value: unknown): string | undefined {
    return typeof value === "number" ? MATTER_TO_SYSTEM_MODE.get(value) : undefined;
}

// ---------------------------------------------------------------------------
// The role table
// ---------------------------------------------------------------------------

/** Matter behaviour ids, as they key `Endpoint.state` / `Endpoint.eventsOf`. */
const ON_OFF = "onOff";
const LEVEL_CONTROL = "levelControl";
const COLOR_CONTROL = "colorControl";
const BRIDGED_INFO = "bridgedDeviceBasicInformation";
const OCCUPANCY_SENSING = "occupancySensing";
const BOOLEAN_STATE = "booleanState";
const TEMPERATURE_MEASUREMENT = "temperatureMeasurement";
const RELATIVE_HUMIDITY_MEASUREMENT = "relativeHumidityMeasurement";
const ILLUMINANCE_MEASUREMENT = "illuminanceMeasurement";
const PRESSURE_MEASUREMENT = "pressureMeasurement";
const FLOW_MEASUREMENT = "flowMeasurement";
const THERMOSTAT = "thermostat";
const DOOR_LOCK = "doorLock";
const WINDOW_COVERING = "windowCovering";

// ---------------------------------------------------------------------------
// Command sinks — the invocation-driven roles' route to `emit`
// ---------------------------------------------------------------------------

/** What a behaviour override needs in order to raise a §5 `command` event. */
interface CommandSink {
    indigoDeviceId: number;
    emit: (data: CommandEventData) => void;
    log: (message: string) => void;
}

/**
 * `Endpoint.id` → the §5 `command` sink for that endpoint.
 *
 * `doorLock` and `windowCovering` answer *invocations*, so their reporting path
 * runs inside a matter.js behaviour rather than an observable callback — and a
 * behaviour is constructed by matter.js from a type, with no way to close over
 * the per-endpoint `emit` the way {@link watchCommands} does for the attribute
 * roles. This map is that closure, keyed on the one identifier a behaviour can
 * always reach (`this.endpoint.id`, which {@link endpointIdFor} makes a pure
 * function of `indigoDeviceId`).
 *
 * Registration and teardown both live in {@link watchCommands} so the lifetime
 * is identical to the observable listeners': one place removes both, and a
 * removed endpoint cannot keep reporting.
 */
const COMMAND_SINKS = new Map<string, CommandSink>();

/**
 * Raise a §5 `command` from inside a behaviour override.
 *
 * Never throws. This runs inside matter.js's handling of a remote invocation;
 * a throw here would fail the ecosystem's command — which is arguably correct —
 * but the failure modes worth guarding are "the endpoint was removed a
 * microsecond ago" and "the plugin's socket closed mid-command", neither of
 * which the ecosystem can do anything with. Mirrors the same decision in
 * {@link watchCommands}.
 */
function emitCommand(endpointId: string, command: string, args: Record<string, unknown>): void {
    const sink = COMMAND_SINKS.get(endpointId);
    if (sink === undefined) {
        logger.warn(`Dropped ${command} for ${endpointId}: no command sink is registered for that endpoint`);
        return;
    }
    try {
        sink.emit({ indigoDeviceId: sink.indigoDeviceId, command, args });
    } catch (error) {
        try {
            sink.log(`Dropped ${command} for endpoint ${sink.indigoDeviceId}: ${describeErrorWithStack(error)}`);
        } catch {
            // The logger itself failed; there is nowhere left to report.
        }
    }
}

/**
 * DoorLock, with the auto-confirmation removed.
 *
 * matter.js's stock `lockDoor`/`unlockDoor` set `lockState` themselves and emit
 * a `LockOperation` event. For a *bridge* that is exactly the behaviour PRD §7
 * forbids — "never auto-confirm destructive state changes": the ecosystem would
 * show the door locked the instant it asked, whether or not Indigo, the Z-Wave
 * mesh and the actual bolt agreed. So neither override calls `super`, and
 * neither touches state. `lockState` moves only when the plugin pushes a
 * `set_state {"locked": …}` having watched the real Indigo device — which is
 * also why the lock's §4.2 vocabulary has no `command` for state.
 *
 * The features are stripped with a bare `.with()`. The class doc claims that is
 * already the default; it is not — the stock server enables `pinCredential`,
 * `rfidCredential`, `user`, the schedules and `unbolting`, which would have the
 * bridge advertise a credential database it has no way to honour.
 */
class IndigoDoorLockServer extends DoorLockServer.with() {
    override lockDoor(): MaybePromise {
        emitCommand(this.endpoint.id, "lock", {});
    }

    override unlockDoor(): MaybePromise {
        emitCommand(this.endpoint.id, "unlock", {});
    }
}

/**
 * WindowCovering, with the instant-snap movement replaced by a `command` event.
 *
 * matter.js's own doc says of the default: "The default implementation logs and
 * immediately updates current position to the target positions. This is
 * probably not desirable for a real device so do not invoke
 * `super.handleMovement()` from your implementation." A bridge is exactly that
 * case — the blind takes seconds and Indigo is the only thing that knows where
 * it actually got to — so this override reports and returns, and
 * `currentPositionLiftPercent100ths` follows the plugin's `set_state`.
 *
 * Only **Lift** is wired: §4.2 has one `position` key and no tilt vocabulary.
 */
class IndigoWindowCoveringServer extends WindowCoveringServer.with("Lift", "PositionAwareLift") {
    override handleMovement(
        type: MovementType,
        _reversed: boolean,
        direction: MovementDirection,
        targetPercent100ths?: number,
    ): MaybePromise {
        if (type !== MovementType.Lift) {
            return;
        }
        emitCommand(this.endpoint.id, "goToPosition", {
            position: movementPosition(direction, targetPercent100ths),
        });
    }

    override handleStopMovement(): MaybePromise {
        emitCommand(this.endpoint.id, "stopMotion", {});
        // Unlike handleMovement, super() is wanted here: all it does is settle
        // `targetPosition…` onto `currentPosition…` so the ecosystem stops
        // rendering the blind as in motion. That is a statement about the
        // *request*, not a claim about where the blind ended up.
        return super.handleStopMovement();
    }
}

/**
 * The §4.2 `position` a movement request means.
 *
 * **`targetPercent100ths` wins whenever it is present, and with
 * `PositionAwareLift` enabled it always is** — matter.js throws
 * "Target position must be defined for position aware lift" otherwise. The
 * `direction` argument is derived by matter.js from current-vs-target and is
 * therefore *not* a substitute: `goToLiftPercentage(3000)` on an open blind
 * arrives as `Close`, and reading the direction alone would report "shut it
 * completely" for a request to move it to 70% open. (This was the first
 * implementation, and the probe caught it.)
 *
 * `Open`/`Close` are the fallback for the non-position-aware feature set, which
 * this bridge does not enable but which a future `.with()` change could. They
 * are unambiguous ends of travel, so they map to 100 and 0 directly.
 */
function movementPosition(direction: MovementDirection, targetPercent100ths?: number): number {
    if (typeof targetPercent100ths === "number") {
        return percent100thsToPosition(targetPercent100ths);
    }
    return direction === MovementDirection.Open ? 100 : 0;
}

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

/**
 * Why a lawful §4.2 role can still be refused.
 *
 * `unknown_role` would be a lie (the role *is* in the v1 enum) and silently
 * skipping the export would be worse — the user selected the device and would
 * see nothing appear. So it is an `internal` refusal that names the version.
 *
 * **Unreachable for every v1 role as of E4**, and kept anyway: `ROLE_DEFINITIONS`
 * is `Record<RoleValue, …>`, so a role added to the enum without a definition is
 * a compile error here and a clean refusal in a node that was built before the
 * enum grew — which is precisely the PM-B skew case (§2), where an old node
 * serves a newer plugin.
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

/**
 * A sensor's whole `set_state` vocabulary: one key, one attribute, one scale.
 *
 * Sensors emit no commands (§4.2), so a role factory for one is entirely
 * described by "which behaviour, which key, which converter" — which is what
 * this builds, rather than five near-identical hand-written entries whose only
 * differences would be easy to transpose.
 */
function measuredValuePatch(
    behavior: string,
    key: string,
    convert: (value: number) => number,
): (states: Record<string, unknown>) => Record<string, unknown> {
    return states =>
        typeof states[key] === "number" && Number.isFinite(states[key])
            ? { [behavior]: { measuredValue: convert(states[key] as number) } }
            : {};
}

/** As {@link measuredValuePatch}, for the two boolean sensors. */
function booleanStatePatch(
    behavior: string,
    key: string,
    attribute: string,
    convert: (value: boolean) => unknown,
): (states: Record<string, unknown>) => Record<string, unknown> {
    return states =>
        typeof states[key] === "boolean" ? { [behavior]: { [attribute]: convert(states[key] as boolean) } } : {};
}

/**
 * The thermostat patch. Every key is independent (§3.4 state maps are partial),
 * and an unmappable `systemMode` is simply not consumed — which
 * {@link statePatchOrRefuse} turns into `malformed_args` when it was the only
 * key sent, and silently ignores when it rode alongside a setpoint.
 */
function thermostatPatch(states: Record<string, unknown>): Record<string, unknown> {
    const thermostat: Record<string, unknown> = {};
    if (typeof states.localTemperatureC === "number") {
        thermostat.localTemperature = celsiusToMatter(states.localTemperatureC);
    }
    if (typeof states.heatingSetpointC === "number") {
        thermostat.occupiedHeatingSetpoint = celsiusToMatter(states.heatingSetpointC);
    }
    if (typeof states.coolingSetpointC === "number") {
        thermostat.occupiedCoolingSetpoint = celsiusToMatter(states.coolingSetpointC);
    }
    const systemMode = systemModeToMatter(states.systemMode);
    if (systemMode !== undefined) {
        thermostat.systemMode = systemMode;
    }
    return Object.keys(thermostat).length > 0 ? { [THERMOSTAT]: thermostat } : {};
}

/**
 * The covering patch, and the one place the §4.2 → Matter inversion is applied
 * on the push side.
 *
 * `target` is written alongside `current` on purpose: an ecosystem renders a
 * covering as *moving* while the two differ, so pushing only the current
 * position would leave a blind that has finished travelling animating forever.
 */
function windowCoveringPatch(states: Record<string, unknown>): Record<string, unknown> {
    if (typeof states.position !== "number") {
        return {};
    }
    const percent100ths = positionToPercent100ths(states.position);
    return {
        [WINDOW_COVERING]: {
            currentPositionLiftPercent100ths: percent100ths,
            targetPositionLiftPercent100ths: percent100ths,
        },
    };
}

/** `locked: true` → `LockState.Locked`. Mirrors `matter_handlers/door_lock.py`. */
function doorLockPatch(states: Record<string, unknown>): Record<string, unknown> {
    return typeof states.locked === "boolean"
        ? { [DOOR_LOCK]: { lockState: states.locked ? DoorLock.LockState.Locked : DoorLock.LockState.Unlocked } }
        : {};
}

/** `occupiedHeatingSetpoint` → §4.2 `setHeatingSetpoint {"valueC": float}`. */
const WATCH_HEATING_SETPOINT: WatchSpec = {
    behavior: THERMOSTAT,
    attribute: "occupiedHeatingSetpoint",
    command: value =>
        typeof value === "number"
            ? { command: "setHeatingSetpoint", args: { valueC: matterToCelsius(value) } }
            : undefined,
};

const WATCH_COOLING_SETPOINT: WatchSpec = {
    behavior: THERMOSTAT,
    attribute: "occupiedCoolingSetpoint",
    command: value =>
        typeof value === "number"
            ? { command: "setCoolingSetpoint", args: { valueC: matterToCelsius(value) } }
            : undefined,
};

/**
 * `systemMode` → §4.2 `setSystemMode {"mode": str}`.
 *
 * A mode outside §4.2's four values reports nothing (see
 * {@link MATTER_TO_SYSTEM_MODE}). The attribute has still moved, so the
 * ecosystem and Indigo now disagree — but the plugin's next push corrects the
 * ecosystem, whereas a fabricated `"heat"` would have Indigo act on something
 * nobody asked for.
 */
const WATCH_SYSTEM_MODE: WatchSpec = {
    behavior: THERMOSTAT,
    attribute: "systemMode",
    command: value => {
        const mode = matterToSystemMode(value);
        return mode === undefined ? undefined : { command: "setSystemMode", args: { mode } };
    },
};

/**
 * The thermostat features we enable, and why each is needed.
 *
 * `Heating`/`Cooling` are what make `occupiedHeatingSetpoint` and
 * `occupiedCoolingSetpoint` exist at all; `AutoMode` is what makes
 * `SystemMode.Auto` — §4.2's `"auto"` — a legal value. Everything else is off:
 * the stock `ThermostatServer` turns on `Occupancy` and `Presets` as well, and
 * `Presets` in particular advertises a scheduling surface a bridge cannot honour.
 */
const THERMOSTAT_FEATURES = ["Heating", "Cooling", "AutoMode"] as const;

/**
 * Thermostat attributes matter.js will not default for us.
 *
 * `systemMode` and `controlSequenceOfOperation` are plain mandatory attributes
 * with no marker in the typings, but endpoint construction hard-fails without
 * them ("Conformance \"M\": Matter requires you to set this attribute") because
 * matter.js's `selectDefaultValue` has no fallback for enum metatypes. Same
 * class of trap as {@link colorControlDefaults}, found the same way — by
 * building the endpoint and reading the error.
 *
 * `CoolingAndHeating` is the sequence that matches the feature set above; the
 * initial `Off` is a statement about knowing nothing yet, and the plugin's
 * first `set_state` replaces it.
 */
function thermostatDefaults(): Record<string, unknown> {
    return {
        systemMode: Thermostat.SystemMode.Off,
        controlSequenceOfOperation: Thermostat.ControlSequenceOfOperation.CoolingAndHeating,
    };
}

/**
 * DoorLock attributes matter.js will not default for us.
 *
 * `lockType` and `operatingMode` are the same enum-default trap as the
 * thermostat's: mandatory, unmarked in the typings, and a hard
 * `Conformance "M"` failure at construction if absent.
 *
 * `lockType` is `Other` rather than `DeadBolt`: the bridge is exporting an
 * Indigo relay whose real mechanism nothing in the device model records, and
 * claiming a deadbolt would be a guess an ecosystem might render as fact.
 *
 * Note what is deliberately *not* here. With the stock (all-features)
 * `DoorLockServer`, `wrongCodeEntryLimit` and `userCodeTemporaryDisableTime`
 * default to `0` and fail their own `1 to 255` constraint, so they have to be
 * seeded — but under `DoorLockServer.with()` the PIN/RFID features are gone and
 * setting them fails the *opposite* way (`Conformance "PIN | RID": Matter does
 * not allow you to set this attribute`). Stripping the features is what makes
 * both attributes go away, and that is the only reason this function is short.
 */
function doorLockDefaults(): Record<string, unknown> {
    return {
        lockType: DoorLock.LockType.Other,
        operatingMode: DoorLock.OperatingMode.Normal,
        actuatorEnabled: true,
    };
}

const ROLE_DEFINITIONS: Record<RoleValue, RoleDefinition> = {
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

    // -- E4: sensors. Read-only in Matter, so every `watch` is empty by
    // design (§4.2 gives them no commands), not by omission.
    [Role.occupancySensor]: {
        // OccupancySensorDevice ships WITHOUT its cluster, and an
        // OccupancySensingServer with no modality feature logs "features need
        // to be set based on the detector type" and derives nothing. Indigo
        // does not record how a binary sensor detects, and PIR is what almost
        // every Indigo motion sensor is, so it is declared rather than left
        // blank — the alternative is an accessory ecosystems cannot categorise.
        deviceType: () =>
            OccupancySensorDevice.with(
                BridgedDeviceBasicInformationServer,
                OccupancySensingServer.with("PassiveInfrared"),
            ),
        initialState: () => ({}),
        // `occupancy` is a bitmap whose bit 0 is the whole meaning — matter.js
        // models it as `{occupied?: boolean}`, so the conversion is a wrap.
        statePatch: booleanStatePatch(OCCUPANCY_SENSING, "occupied", "occupancy", occupied => ({ occupied })),
        watch: [],
    },
    [Role.contactSensor]: {
        deviceType: () => ContactSensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        // §4.2 `contact` is "true = closed" and BooleanState's `stateValue` is
        // documented "in a Contact Sensor device type, FALSE=open or no
        // contact, TRUE=closed or contact" — the same polarity, so no inversion.
        statePatch: booleanStatePatch(BOOLEAN_STATE, "contact", "stateValue", contact => contact),
        watch: [],
    },
    [Role.temperatureSensor]: {
        deviceType: () => TemperatureSensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: measuredValuePatch(TEMPERATURE_MEASUREMENT, "temperatureC", celsiusToMatter),
        watch: [],
    },
    [Role.humiditySensor]: {
        // `HumiditySensorDevice`, not `RelativeHumiditySensorDevice` — the
        // device type is named for the reading, the behaviour id for the cluster.
        deviceType: () => HumiditySensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: measuredValuePatch(RELATIVE_HUMIDITY_MEASUREMENT, "humidityPct", humidityToMatter),
        watch: [],
    },
    [Role.lightSensor]: {
        deviceType: () => LightSensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: measuredValuePatch(ILLUMINANCE_MEASUREMENT, "lux", luxToMatter),
        watch: [],
    },
    [Role.pressureSensor]: {
        deviceType: () => PressureSensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: measuredValuePatch(PRESSURE_MEASUREMENT, "pressureKPa", kilopascalsToMatter),
        watch: [],
    },
    [Role.flowSensor]: {
        deviceType: () => FlowSensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: measuredValuePatch(FLOW_MEASUREMENT, "flowM3h", cubicMetresPerHourToMatter),
        watch: [],
    },

    // -- E4: the two-way roles.
    [Role.thermostat]: {
        // ThermostatDevice ships without its cluster, and the bare
        // ThermostatServer would enable Occupancy and Presets too.
        deviceType: () =>
            ThermostatDevice.with(BridgedDeviceBasicInformationServer, ThermostatServer.with(...THERMOSTAT_FEATURES)),
        initialState: () => ({ [THERMOSTAT]: thermostatDefaults() }),
        statePatch: thermostatPatch,
        // Ecosystems change a thermostat by *writing the setpoint attributes*,
        // not by invoking a command (`setpointRaiseLower` exists but Apple,
        // Google and Alexa all write), so the attribute watchers cover the
        // whole §4.2 command family — the same reason one `currentLevel`
        // watcher covers `moveToLevel`, `step` and `move`.
        watch: [WATCH_HEATING_SETPOINT, WATCH_COOLING_SETPOINT, WATCH_SYSTEM_MODE],
    },
    [Role.doorLock]: {
        deviceType: () => DoorLockDevice.with(BridgedDeviceBasicInformationServer, IndigoDoorLockServer),
        initialState: () => ({ [DOOR_LOCK]: doorLockDefaults() }),
        statePatch: doorLockPatch,
        // `lockState` is deliberately NOT watched. It only ever moves because
        // we pushed it (the overrides never write it), so a watcher would see
        // nothing an ecosystem did — and if it ever did fire it would echo our
        // own push straight back as a `lock`/`unlock`.
        watch: [],
    },
    [Role.windowCovering]: {
        deviceType: () => WindowCoveringDevice.with(BridgedDeviceBasicInformationServer, IndigoWindowCoveringServer),
        initialState: () => ({}),
        statePatch: windowCoveringPatch,
        // Same reasoning as doorLock: the position attributes move only under
        // our own `set_state`, because handleMovement no longer writes them.
        watch: [],
    },
};

/**
 * The §4.2 roles this bridge version can construct — **all of them, as of E4**.
 *
 * Derived from the table rather than listed, so the two cannot disagree: a role
 * added to the §4.2 enum without a definition is refused at runtime by
 * {@link definitionFor} and caught at compile time by `Record<RoleValue, …>`.
 */
export const SUPPORTED_ROLES: readonly RoleValue[] = Object.keys(ROLE_DEFINITIONS) as RoleValue[];

export function isSupportedRole(role: RoleValue): boolean {
    return ROLE_DEFINITIONS[role] !== undefined;
}

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
 * Subscribe the role's cluster-change listeners, register its command sink, and
 * return the teardown for both.
 *
 * The teardown matters: a removed endpoint whose observables still hold our
 * handler would keep emitting `command` events for a device the plugin no
 * longer exports, and would keep the closure (and the endpoint) alive. The
 * {@link COMMAND_SINKS} entry is registered and removed here for exactly the
 * same reason — the invocation-driven roles have no observables at all, so this
 * is the *only* thing standing between a removed lock and a `command` event for
 * a device the plugin has forgotten.
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
    const endpointId = endpointIdFor(spec.indigoDeviceId);
    COMMAND_SINKS.set(endpointId, { indigoDeviceId: spec.indigoDeviceId, emit, log });
    teardown.push(() => COMMAND_SINKS.delete(endpointId));

    const unwatch = (): void => {
        for (const off of teardown) {
            off();
        }
        teardown.length = 0;
    };

    try {
        subscribe(endpoint, spec, emit, log, teardown);
    } catch (error) {
        // The sink is already registered by this point, and the caller's only
        // recovery is to close the endpoint — which would leave the sink behind
        // holding a reference to a dead endpoint's emitter.
        unwatch();
        throw error;
    }

    return unwatch;
}

/** {@link watchCommands}'s observable half, extracted so its throws can unwind the sink. */
function subscribe(
    endpoint: Endpoint,
    spec: { indigoDeviceId: number; role: RoleValue },
    emit: (data: CommandEventData) => void,
    log: (message: string) => void,
    teardown: (() => void)[],
): void {
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
}
