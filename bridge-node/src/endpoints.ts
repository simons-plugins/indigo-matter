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
 * * **Attribute-driven** roles (every sensor and the thermostat) report
 *   ecosystem intent through {@link WatchSpec}s on the cluster attributes an
 *   ecosystem writes — there is no other surface for them to speak through.
 * * **Invocation-driven** roles — `doorLock`, `windowCovering`, and, as of
 *   #143, the whole light/plug family (`onOff`, `levelControl` and
 *   `colorControl` together) — cannot rely on attributes: their
 *   ecosystem-facing surface is *commands*, and matter.js's stock servers
 *   answer those by writing the state themselves. Every one of them is
 *   overridden here so the write never happens — see
 *   {@link IndigoDoorLockServer}, {@link IndigoWindowCoveringServer},
 *   {@link IndigoOnOffServer}, {@link IndigoLevelControlServer} and
 *   {@link IndigoColorControlServer}/{@link IndigoColorTemperatureControlServer}
 *   — and they reach their `command` sink through {@link COMMAND_SINKS}.
 *
 * The `WATCH_*` specs for `onOff`/`levelControl`/`colorControl` still exist
 * (§2.5 of the #143 design keeps them as producerless **backstops**, not
 * because anything here still writes through them) — see their own docs for
 * why a duplicate frame from a defunct watcher is worth keeping over deleting
 * the regression detector.
 */

import { Endpoint, type EndpointType, Logger, type MaybePromise, type MutableEndpoint, VendorId } from "@matter/main";
import { BridgedDeviceBasicInformationServer } from "@matter/main/behaviors/bridged-device-basic-information";
import { ColorControlServer, xyToHsv } from "@matter/main/behaviors/color-control";
import { DoorLockServer } from "@matter/main/behaviors/door-lock";
import { LevelControlServer } from "@matter/main/behaviors/level-control";
import { OccupancySensingServer } from "@matter/main/behaviors/occupancy-sensing";
import { OnOffServer } from "@matter/main/behaviors/on-off";
import { PowerSourceServer } from "@matter/main/behaviors/power-source";
import { SmokeCoAlarmServer } from "@matter/main/behaviors/smoke-co-alarm";
import { ThermostatServer } from "@matter/main/behaviors/thermostat";
import { MovementDirection, MovementType, WindowCoveringServer } from "@matter/main/behaviors/window-covering";
import { ColorControl } from "@matter/main/clusters/color-control";
import { DoorLock } from "@matter/main/clusters/door-lock";
import { LevelControl } from "@matter/main/clusters/level-control";
import { SmokeCoAlarm } from "@matter/main/clusters/smoke-co-alarm";
import { Thermostat } from "@matter/main/clusters/thermostat";
import { ColorTemperatureLightDevice } from "@matter/main/devices/color-temperature-light";
import { ContactSensorDevice } from "@matter/main/devices/contact-sensor";
import { WaterLeakDetectorDevice } from "@matter/main/devices/water-leak-detector";
import { WaterFreezeDetectorDevice } from "@matter/main/devices/water-freeze-detector";
import { RainSensorDevice } from "@matter/main/devices/rain-sensor";
import { SmokeCoAlarmDevice } from "@matter/main/devices/smoke-co-alarm";
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
import { Status, StatusResponseError } from "@matter/main/types";

import {
    type CommandEventData,
    describeErrorWithStack,
    type EndpointSpec,
    ErrorCode,
    parsePublishedId,
    ProtocolError,
    publishedIdFor,
    Role,
    type RoleValue,
} from "./protocol.js";

/**
 * Clamping is silent by design at the protocol boundary (§4.2 says the node
 * owns bounds), but a value that had to be clamped is the fingerprint of a
 * plugin-side unit bug, so it is worth a debug line naming what moved.
 */
const logger = Logger.get("indigo-matter-endpoints");

/** The one prefix {@link serialNumberFrom} strips back off. */
const UNIQUE_ID_PREFIX = "indigo-";

/**
 * Bridged Device Basic Information `UniqueID`, stable across restarts —
 * `publishedIdFor`'s generation-1 shorthand (issues #219/#240), re-exported
 * here so every call site that does not yet know about generations keeps
 * calling this exactly as before, and gets exactly today's value.
 */
export function uniqueIdFor(indigoDeviceId: number): string {
    return publishedIdFor(indigoDeviceId);
}

/**
 * The inverse of `publishedIdFor`, or `undefined` for anything that is not a
 * lawful published identity — re-exported here for the same reason as
 * {@link uniqueIdFor}.
 *
 * §6.3's "identity flows one way" is about the *protocol*: nothing on the wire
 * is ever keyed on a derived value. `endpoint-map.json` is not on the wire — it
 * is keyed by `UniqueID` because that is what drift is reported against — so
 * restoring an endpoint from it (issue #141) needs the device id back. Since
 * PR5 this also accepts a generation suffix (`indigo-<id>~<gen>`) — documented
 * here because its only caller is restore-on-start's fallback.
 */
export function indigoDeviceIdFrom(uniqueId: string): number | undefined {
    return parsePublishedId(uniqueId)?.deviceId;
}

/**
 * The `Endpoint.id` a device gets **by default** — i.e. the generation-1
 * derivation, `indigo-<deviceId>`. Deliberately the *same* value as
 * {@link uniqueIdFor}: one derivation means the two can never drift apart, and
 * §6.3's one-way identity flow reads directly. matter.js keys persisted
 * endpoint numbers on this string alone (PRD §4.3), so it must never be reused
 * or mutated.
 *
 * **It is NOT where a live endpoint's id comes from, and reverting it to that
 * would be silent.** Since issues #219/#240 an `Endpoint.id` is built from
 * `spec.publishedAs` (`createEndpoint`), which merely DEFAULTS to this
 * derivation — a re-adopted or role-changed accessory publishes something
 * else, and rebuilding the id from `indigoDeviceId` here would hand it a
 * different `Endpoint.id`, i.e. a different matter.js endpoint number, i.e. a
 * duplicate accessory in every paired ecosystem. Neither this nor
 * {@link uniqueIdFor} has a single caller left in `src/` for that reason; both
 * survive as the named default and for tests. `restore.test.ts`'s "the SAME
 * `Endpoint.id` gets the SAME number back" case is the mutation-probed pin
 * that catches a revert.
 */
export const endpointIdFor = uniqueIdFor;

/**
 * `SerialNumber` must differ from `UniqueID` (Matter rejects equal values), so
 * the `indigo-` prefix is stripped here and kept there. Takes the *published*
 * identity, not the driving device id (issues #219/#240): a re-adopted
 * accessory keeps publishing the OLD serial number, because §6.3's identity
 * flow now runs through `publishedAs`, not `indigoDeviceId`, directly.
 */
export function serialNumberFrom(publishedAs: string): string {
    return publishedAs.slice(UNIQUE_ID_PREFIX.length);
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

/**
 * The scale `currentX`/`currentY` are carried on the wire at: matter.js's own
 * `ColorControlServer`'s `x`/`y` convenience getters divide by this exact
 * value (`#returnAsXyValue`, `ColorControlServer.js:1449`), which is why
 * {@link IndigoColorControlServer.moveToColorLogic} divides by it too before
 * handing a CIE fraction to `xyToHsv`.
 */
export const CIE_XY_SCALE = 65536;

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
 * Round half **up**, meaning toward +∞ — `Math.floor(v + 0.5)`, which is also
 * exactly what `Math.round` does. §4.2's "round-half-up" for level/position is
 * the same rule, so the two agree on every value.
 *
 * The distinction is not academic any more: an earlier version of this comment
 * called it "round half away from zero" and excused itself on the grounds that
 * "every quantity here is non-negative". Since E4 that is false —
 * {@link celsiusToMatter} carries signed temperatures, and a signed unit is the
 * only place the two rules differ. Half-up means a negative exact-half rounds
 * *toward zero*: −5.005 °C is −500.5 in centidegrees and comes out as **−500**
 * (−5.00 °C), not −501. That is a hundredth of a degree in the warmer
 * direction, which is the behaviour, not an accident — but it must be the
 * documented behaviour, because a reader who believed the old comment would
 * expect −501 and go looking for a bug.
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
 * PowerSource's `batPercentRemaining`, in half-percent units (0-200), so 100%
 * is 200 and the attribute can express a half-percent Indigo itself cannot.
 */
export const BATTERY_REMAINING_MAX = 200;

/**
 * §4.2 `batteryLevel` (1-100) → `batPercentRemaining` (0-200, half-percent
 * units — 24% is 48). The clamp is load-bearing, not defensive: `endpoint.set`
 * with a raw 201 throws a Matter `Constraint` error at 0.17.8 (measured, §0),
 * and that would fail the whole `set_state`/`createEndpoint` write over one
 * out-of-range battery reading rather than the residual state alongside it.
 */
export function percentToBatteryRemaining(percent: number): number {
    return clampLogged(roundHalfUp(percent * 2), 0, BATTERY_REMAINING_MAX, "battery percent");
}

/**
 * The value actually written to `currentLevel`.
 *
 * The Lighting feature constrains `currentLevel` to 1-254 on every role this
 * bridge builds — not because of a per-device-type `alter` (the three
 * converted roles use a bare `.with("Lighting", "OnOff")`, which drops
 * whatever `currentLevel` alter the stock device definitions carry), but
 * because `LevelControlServer` itself derives `minLevel` as `1` whenever the
 * Lighting feature is enabled and enforces `currentLevel` against it at
 * construction (probed: matter.js 0.17.8 refuses anything else with "The
 * minLevel value of 0 is invalid according to Matter specification"). So a
 * literal 0 still fails validation, and writing 1 instead is still lossless
 * at the protocol boundary: {@link matterToPercent}(1) is 0.
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

/**
 * The setpoint band this bridge advertises, in Matter centidegrees: **0 °C to
 * 50 °C**.
 *
 * matter.js applies the Thermostat cluster's own defaults when these limits are
 * not seeded, and those defaults are a *product* claim, not a protocol one:
 * heating 7–30 °C and cooling 16–32 °C, i.e. the range of a typical American
 * wall thermostat. A bridge has no such range — it is fronting whatever the
 * user exported — and the defaults reject values that are entirely routine in
 * the houses this plugin runs in:
 *
 * * **5 °C frost protection.** Below `minHeatSetpointLimit` (700), so every
 *   push of it failed `CONSTRAINT_ERROR` (status 135);
 * * **a heat-only thermostat.** Indigo reports `coolSetpoint` 0 on one, which
 *   is below `minCoolSetpointLimit` (1600), so *every* `set_state` from such a
 *   device failed — the common case, not an edge.
 *
 * And the failure did not settle: the write threw, nothing in Indigo changed,
 * so the next `deviceUpdated` computed the same diff and pushed the same
 * rejected value again, forever.
 *
 * 0–50 °C spans domestic heating, cooling and frost protection with room to
 * spare, and is narrow enough that {@link clampSetpoint} still catches a
 * genuine unit error (a °F thermostat pushing 68 lands on 50 with a debug line
 * naming it, rather than being silently believed).
 */
export const SETPOINT_CENTI_MIN = 0;
export const SETPOINT_CENTI_MAX = 5000;

/**
 * A §4.2 setpoint, in the band this bridge advertises.
 *
 * Mirrors {@link clampMireds}: the limits above are what the cluster *says* it
 * accepts, and a value outside them is refused by matter.js with a
 * `CONSTRAINT_ERROR` that fails the whole `set_state`. Clamping keeps the
 * ecosystem showing the nearest thing to the truth we are allowed to say, and
 * the debug line is the fingerprint of the plugin-side unit bug that produced
 * it.
 */
export function clampSetpoint(celsius: number): number {
    return clampLogged(
        celsiusToMatter(celsius),
        SETPOINT_CENTI_MIN,
        SETPOINT_CENTI_MAX,
        "thermostat setpoint (0.01 °C)",
    );
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
 *
 * Rounded like every sibling converter. §4.2 types `position` as an integer, so
 * the multiply is exact for every lawful input — but `…LiftPercent100ths` is a
 * `uint16` and matter.js validates it, so a fractional position arriving from a
 * newer plugin (or from a handler that stopped rounding) would fail the write
 * outright rather than losing a fraction of a percent. Rounding here is the
 * same "the node owns the wire conversion" rule the other twelve follow.
 */
export function positionToPercent100ths(position: number): number {
    return roundHalfUp((100 - clampLogged(position, 0, 100, "covering position")) * 100);
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
const SMOKE_CO_ALARM = "smokeCoAlarm";
const TEMPERATURE_MEASUREMENT = "temperatureMeasurement";
const RELATIVE_HUMIDITY_MEASUREMENT = "relativeHumidityMeasurement";
const ILLUMINANCE_MEASUREMENT = "illuminanceMeasurement";
const PRESSURE_MEASUREMENT = "pressureMeasurement";
const FLOW_MEASUREMENT = "flowMeasurement";
const THERMOSTAT = "thermostat";
const DOOR_LOCK = "doorLock";
const WINDOW_COVERING = "windowCovering";
/** Issue #220 — PowerSource, added optionally per §4.1's `battery` flag. */
const POWER_SOURCE = "powerSource";

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
 * always reach: `this.endpoint.id`, which IS the endpoint's `spec.publishedAs`
 * (see {@link createEndpoint}) — the CommandSink value carries `indigoDeviceId`
 * separately (issues #219/#240) precisely because, under a re-adopted
 * identity, the two are no longer a pure function of one another.
 *
 * Registration and teardown both live in {@link watchCommands} so the lifetime
 * is identical to the observable listeners': one place removes both, and a
 * removed endpoint cannot keep reporting.
 */
const COMMAND_SINKS = new Map<string, CommandSink>();

/**
 * Raise a §5 `command` from inside a behaviour override.
 *
 * Never throws once a sink exists. This runs inside matter.js's handling of a
 * remote invocation; a throw there would fail the ecosystem's command — which
 * is arguably correct — but the failure modes worth guarding are "the endpoint
 * was removed a microsecond ago" and "the plugin's socket closed mid-command",
 * neither of which the ecosystem can do anything with. Mirrors the same
 * decision in {@link watchCommands}.
 *
 * **`failWithoutSink` is the deliberate exception, and it is for `doorLock`
 * only.** A missing sink means the plugin is gone (Indigo reloading it, the
 * socket down mid-invocation) so the command reaches nothing at all. For a lamp
 * that is a warn-and-return: the ecosystem shows the light on, the next attach
 * pushes the truth back, and nobody was misled about anything that matters. For
 * a **lock** it is different in kind — Home reports "Unlocked", the user walks
 * away, and the bolt never moved. There is no protocol frame for "your command
 * was dropped" (the plugin cannot be told about invocations that arrived while
 * it was away; that would be a §3.1 protocol addition, deliberately not made
 * here), so failing the invocation is the only way the truth reaches the
 * person standing at the door. A Matter status error makes
 * Home say the accessory did not respond, which is exactly what happened.
 */
function emitCommand(
    endpointId: string,
    command: string,
    args: Record<string, unknown>,
    failWithoutSink = false,
): void {
    const sink = COMMAND_SINKS.get(endpointId);
    if (sink === undefined) {
        logger.warn(`Dropped ${command} for ${endpointId}: no command sink is registered for that endpoint`);
        if (failWithoutSink) {
            throw new StatusResponseError(
                `${command} cannot be delivered: the Indigo plugin is not connected to this bridge`,
                Status.Failure,
            );
        }
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
 * OnOff, with the plugin's own push routed around the emitting overrides
 * (#143, #201).
 *
 * `.with("Lighting")` is **mandatory**, not a default left in place for
 * tidiness. Every onOff-bearing device type in matter.js 0.17.8 declares the
 * identical `OnOffServer.with("Lighting")` (verified against
 * `on-off-light.js`, `on-off-plug-in-unit.js`, `dimmable-light.js`,
 * `color-temperature-light.js` and `extended-color-light.js`), and the
 * Lighting feature is what makes `onTime`/`offWaitTime`/`globalSceneControl`/
 * `startUpOnOff` exist at all. A bare `extends OnOffServer` would strip the
 * feature from all five roles and make `OnWithTimedOff` unimplementable — do
 * not "simplify" this away.
 *
 * **There is no context guard in `on()`/`off()`, and there must not be one.**
 * The obvious echo guard — `isEcosystemChange(this.context)`, mirroring the
 * attribute watcher — is broken by a *third* caller of `off()` that is also
 * offline: the countdown timer's own expiry. `OnOffServer.js:163` builds the
 * timer as `this.callback(this.#timedOnTick, { lock: true })`;
 * `Behavior.callback` hands back a bare emitter with no context argument, so
 * inside matter.js's `Reactors` the trailing-context lookup finds nothing and
 * the reaction falls through to `LocalActorContext.act(...)`, which freezes
 * `offline: true`. So `#timedOnTick`'s `await this.off()`
 * (`OnOffServer.js:178`) runs with `this.context.offline === true` —
 * indistinguishable from a plugin push under {@link isEcosystemChange}.
 * Guarding `off()` on the context would silently swallow the
 * countdown-expiry emission. (`hasRemoteActor`, the guard stock
 * `offWithEffect` uses at `OnOffServer.js:112`, fails the same way on the
 * expiry tick, and on the *delayed* scene-apply off at `OnOffServer.js:229`,
 * which runs via `this.callback` in the same offline reactor shape. The
 * *immediate* scene-apply at `OnOffServer.js:204` would survive such a guard —
 * `ScenesManagementServer` invokes it through `agentFor(this.context)`, i.e.
 * the recalling command's own remote context — but one surviving caller does
 * not rescue the guard from the other two.)
 *
 * The guard that actually works is structural, not contextual:
 * {@link applyIndigoOnOff} — the plugin's only entry point — calls
 * `super.on()`/`super.off()`, which is static dispatch straight to
 * `OnOffBaseServer.prototype.on`/`off`. The overrides below are simply never
 * entered by our own push, in any context, so there is nothing to guard
 * against and no interleaving to get wrong.
 *
 * Nothing here writes `this.state.onOff` or calls `super`: the attribute
 * moves only from {@link applyIndigoOnOff}'s `super` calls, once Indigo has
 * confirmed it — mirroring {@link IndigoDoorLockServer} and
 * {@link IndigoWindowCoveringServer}. `toggle()`, `onWithTimedOff()`,
 * `offWithEffect()` and `onWithRecallGlobalScene()` are deliberately left
 * stock: matter.js's own method doc on `toggle()` says "it is enough to
 * override on() and off() with custom control logic" (`OnOffServer.js:94-98`,
 * repeated on `onWithTimedOff` at `:132-136`), and when they act, all four
 * funnel into `this.on()`/`this.off()` — virtual dispatch to the overrides
 * below — so overriding them too would duplicate the logic and risk a
 * double emission.
 *
 * **Two stock `onWithTimedOff` branches silently discard the command** —
 * accepted, matter.js 0.17.8 (`OnOffServer.js:137-147`), revisit on a field
 * report of a lost countdown:
 *
 * * `acceptOnlyWhenOn` (`:138-140`): a controller asking "timed-off, but only
 *   if the light is on" is answered from the *attribute*, which under
 *   no-auto-confirm stays `false` for the whole tap-to-Indigo-confirmation
 *   window — so an "on, then timed-off-if-on" pair sent quickly loses its
 *   countdown, with success reported to the controller and nothing emitted.
 * * the `offWaitTime > 0` hold-window branch (`:141-147`): attribute-`false`
 *   is read as "in the delayed-off hold", which the confirmation window now
 *   also looks like; the command adjusts hold timers and returns without
 *   reaching `this.on()`, so nothing is emitted.
 *
 * Both are spec-shaped behaviour for a real device; the bridge merely widens
 * the attribute-`false` window. No known controller sends either shape at a
 * tile (Apple Home does not use `OnWithTimedOff` there), and overriding
 * `onWithTimedOff` to special-case "unconfirmed" would duplicate stock logic
 * this class otherwise leaves alone.
 *
 * **This is now FIXED, not a known gap.** `LevelControlServer`'s `couple()`
 * used to write `onOff` optimistically ahead of Indigo's confirmation
 * whenever a dimmer command carried `WithOnOff` — a real PRD §7 violation,
 * reachable only from LevelControl, not from this class. #143's LevelControl
 * conversion ({@link IndigoLevelControlServer}) closes it by never calling
 * `couple()` at all: the overridden `transition()` replaces the whole method
 * body, so nothing downstream of a dimmer command ever reaches
 * `agent.get(OnOffServer).state.onOff = …` any more. The coupling went
 * invocation-driven under the same issue (#143) as `onOff` itself, but two
 * releases later: `onOff` shipped in 2026.17.0, this conversion in 2026.19.0.
 *
 * **`#applySceneValues`'s own equality guard is pre-existing, and it is
 * identical to {@link IndigoLevelControlServer}'s.** `OnOffServer.js:197`:
 * `this.state.onOff === onOff` returns before the scene ever reaches
 * `on()`/`off()`. Under no-auto-confirm that attribute is Indigo's last
 * CONFIRMED value, not the device's real one, so a scene recalling the
 * currently-confirmed on/off state emits nothing even if the device has
 * since drifted. Private method, not interceptable from here; noted rather
 * than fixed — see {@link IndigoLevelControlServer}'s own class doc for the
 * fuller version of this same risk.
 */
class IndigoOnOffServer extends OnOffServer.with("Lighting") {
    /**
     * The plugin's own push, and the only writer of the `onOff` attribute.
     *
     * Deliberately not named `on`/`off` and not an override: routing through
     * `super.on()`/`super.off()` is what keeps this method from ever reaching
     * the emitting overrides below, in any context — see the class doc.
     *
     * A no-op when the pushed value already matches the attribute — **not**
     * a tidiness filter. `super.off()` unconditionally stops `timedOnTimer`
     * (`OnOffServer.js:85-88`), and `registry.update()` pushes the full state
     * set on every attach/upsert (`registry.ts:413`). Without this guard, a
     * plugin restart landing between an ecosystem `OnWithTimedOff` and
     * Indigo's own confirmation — attribute still `false`, timer already
     * running — would have the restart's replayed push read as "turn it
     * off" and silently kill a countdown nothing asked to cancel.
     *
     * **The ghost case this guard used to be ambiguous about is closed.**
     * Before #143's LevelControl conversion, `LevelControlServer.couple()`
     * could move the attribute directly (a dimmer `*WithOnOff` command wrote
     * `state.onOff`, never through `off()`), which made an Indigo
     * off-confirmation arriving with `value === state.onOff` while
     * `timedOnTimer` was still live genuinely ambiguous: a harmless replay
     * and a ghost countdown were byte-identical here. `couple()` is never
     * called any more ({@link IndigoLevelControlServer}'s `transition()`
     * override replaces its whole call site), so the replay case is the
     * *only* remaining explanation for this state, and the branch below is
     * debug-level bookkeeping rather than a warning about something that
     * might be wrong. (#203 — a bridge-node *restart* dropping a running
     * countdown, since the timer is in-memory only — is a separate, still
     * open half of countdown correctness; this guard only concerns the
     * replayed push that follows such a restart.)
     */
    applyIndigoOnOff(value: boolean): MaybePromise {
        if (value === this.state.onOff) {
            // The `.with("Lighting")` composed type loses the base's `protected
            // internal` declaration (matter.js 0.17.8 .d.ts quirk); the runtime
            // shape is the stock one #stopHeldTimer itself reads, pinned by the
            // countdown tests.
            const internal = (this as unknown as { internal?: { timedOnTimer?: { isRunning?: boolean } } }).internal;
            if (!value && internal?.timedOnTimer?.isRunning) {
                logger.debug(
                    `Endpoint ${this.endpoint.id}: an off push matched an already-off onOff attribute while a ` +
                        "timed-on countdown is still running — most likely a replayed push racing its own " +
                        "confirmation (harmless; the countdown must survive). Two other reachable causes: a " +
                        "countdown whose ON command was dropped while the plugin was detached (the command sink " +
                        "outlives the socket; ws-server's sendEvent drops that frame per-attempt when nobody is " +
                        "attached — this IS a genuine ghost, and the countdown's own expiry can turn off a lamp " +
                        "someone manually re-lit in the meantime), and a spec upsert landing during a " +
                        "confirmation window. #143 removed a fourth explanation for this state (LevelControl's " +
                        "onOff coupling writing ahead of Indigo) but is not the only one left.",
                );
            }
            return;
        }
        return value ? super.on() : super.off();
    }

    override on(): MaybePromise {
        emitCommand(this.endpoint.id, "onOff", { value: true });
    }

    override off(): MaybePromise {
        emitCommand(this.endpoint.id, "onOff", { value: false });
    }
}

/**
 * LevelControl, with every write-producing command converted to a `command`
 * event (#143's LevelControl half — ColorControl's is
 * {@link IndigoColorControlServer}).
 *
 * **One override closes the whole command family.** `moveToLevel(WithOnOff)`,
 * `move(WithOnOff)` and `step(WithOnOff)` all dispatch through their own
 * `*Logic` method into {@link transition} (`LevelControlServer.js:172,182,
 * 214,265,315`), and so does `ScenesManagement`'s scene-apply path
 * (`#applySceneValues`, `:453-459`) — so one override, not five or six, is
 * the whole surface (measured, §0/P-1). `stop`/`stopWithOnOff` never reach
 * `transition()` at all — `stopLogic` only halts a running *managed*
 * transition (`LevelControlServer.js:331-333`) — and with managed
 * transitions off (see {@link LEVEL_CONTROL_INITIAL}) there is nothing
 * running to stop and nothing to report, so they are left stock.
 *
 * **`#applySceneValues`'s own equality guard runs BEFORE the funnel above,
 * and it compares against the LAGGING attribute.** `LevelControlServer.js:
 * 455`: `this.state.currentLevel === level` returns without ever reaching
 * `transition()`. Under no-auto-confirm that attribute is Indigo's last
 * CONFIRMED level, not the device's real one, so a scene recalling the
 * currently-confirmed level emits nothing even if the device has since
 * drifted — and a recall landing inside a slider's confirmation window can
 * silently skip the level half of the scene while colour/onOff (whose own
 * guards read the same lagging attribute, but happen to differ at that
 * moment) still apply, a partial scene. This is a private method, so it
 * cannot be intercepted or overridden from here; noted rather than fixed,
 * and worth revisiting upstream if it bites in the field.
 *
 * **`_changePerS` is deliberately unused.** With
 * `managedTransitionTimeHandling: false`, `Transitions.start` collapses
 * every rate and every transition time to ONE immediate write of the final
 * value (`Transitions.js:69-79` — measured, §0/P-1): a stepped emission here
 * would invent a `setLevel` cadence Indigo has never been asked to support,
 * for a signal no ecosystem can currently request faster than "as fast as
 * possible".
 *
 * **The clamp is load-bearing, not defensive.** `move` arrives with
 * `targetLevel` of `±Infinity` (`moveLogic`, `LevelControlServer.js:257-264`)
 * and `step` can arrive below `minLevel`/above `maxLevel` (`stepLogic` adds
 * `stepSize` to `currentLevel` with no crop, `:315`). Stock
 * `Transitions.start` bounds every target against the configured min/max
 * before it ever writes an attribute (`Transitions.js:71-82`); overriding
 * `transition()` bypasses that clamp entirely, so skipping it here would let
 * an `Infinity` reach {@link matterToPercent} and, from there, Indigo.
 *
 * **The `WithOnOff` emission order mirrors today's measured attribute-write
 * order**, so the two `command` events an ecosystem sees carry the same
 * meaning stock's two attribute writes did: turning the light off at the
 * minimum level writes `currentLevel` first and `onOff` second (`couple`'s
 * `preCommit` participant runs at commit, after the level write,
 * `LevelControlServer.js:369-381`); turning it on at any other level writes
 * `onOff` first, immediately (`:382-397`), so `setLevel` follows.
 *
 * **The boundary decision is the CLAMPED TARGET, never `state.onOff`.**
 * Gating on the attribute would repeat exactly the defect #143 closes: under
 * no-auto-confirm the attribute lags whatever Indigo has actually confirmed,
 * so "is the light on" read from it is stale for the whole confirmation
 * window. The clamped target also fixes a stock bug in passing:
 * `moveWithOnOff(Down)` reaches stock `couple()` with `targetLevel =
 * -Infinity`, which is `!== minLevel` *before* clamping, so stock's own logic
 * turns the light ON for a command asking it to dim toward off
 * (`LevelControlServer.js:363,369,382` — 363 is `couple()` itself, 369 the
 * `targetLevel === this.minLevel` comparison this paragraph is about, 382
 * the immediate-write `else` branch) — comparing the already-clamped value
 * to `minLevel` cannot make that mistake.
 *
 * **The `onOff` half emits unconditionally**, even when the pushed value
 * already matches what the attribute (still) says — the same "a frequency
 * change is still a report" reasoning {@link IndigoOnOffServer}'s class doc
 * and `BRIDGE_PROTOCOL.md` §4.2 already make for the plain onOff commands:
 * two `WithOnOff` invocations five seconds apart both mean something to
 * Indigo, even when nothing here moved between them.
 *
 * `handleOnOffChange` and `blockOnOffCouplingOnce` are inherited unmodified
 * from `LevelControlBaseServer` and are inert on every role this bridge
 * builds: `onLevel` is `null` on every one of them (never seeded, never
 * pushed — §4.2 has no such key), `handleOnOffChange` returns immediately
 * whenever `state.onLevel === null` (`LevelControlServer.js:430`), and
 * `blockOnOffCouplingOnce` is set only inside `couple()` when `onLevel` is
 * non-null (`:388`), so it never becomes `true` either. Noted here only so a
 * reader chasing `onLevel` down from the Matter spec does not go looking for
 * where this bridge sets it.
 */
class IndigoLevelControlServer extends LevelControlServer.with("Lighting", "OnOff") {
    override transition(
        targetLevel?: number,
        _changePerS?: number | null | ((currentLevel: number) => number),
        withOnOff = false,
        _options: LevelControl.Options = {},
    ): MaybePromise {
        // `this.currentLevel` (the getter, not `this.state.currentLevel`)
        // THROWS a StatusResponseError if the attribute is ever null, where
        // stock's own transition() silently no-ops instead. Unreachable
        // today — LEVEL_CONTROL_INITIAL seeds 1, and every stock caller here
        // passes a number for targetLevel — but recorded so a future change
        // that makes it reachable fails loudly instead of surprising.
        const level = clamp(targetLevel ?? this.currentLevel, this.minLevel, this.maxLevel);
        const setLevel = (): void => emitCommand(this.endpoint.id, "setLevel", { level: matterToPercent(level) });
        if (!withOnOff) {
            setLevel();
            return;
        }
        if (level === this.minLevel) {
            // Level-then-off — mirrors couple()'s preCommit ordering (see
            // class doc).
            setLevel();
            emitCommand(this.endpoint.id, "onOff", { value: false });
        } else {
            // Level ONLY — no onOff frame at all, deliberately NOT couple()'s
            // on-then-level order (issue #239, Simon's ruling). Indigo's
            // turn-on-to-level semantics make the level command do the
            // turn-on itself, reporting the right level; every role carrying
            // LevelControl maps to an Indigo dimmer, so there is no
            // non-dimmer driver for a belt-and-braces onOff to serve. The
            // old leading `turnOn` restored the dimmer's stored level (0
            // after a slider-to-zero) and its stale Z-Wave report arrived
            // AFTER setBrightness's — Indigo's brightness state landed at 0
            // and the bridge then faithfully mirrored the wrong truth back
            // to every ecosystem (~10s later in Apple Home, live-observed).
            // The min-level branch above keeps its explicit off: "off" is a
            // state change setBrightness(0) implies but an off frame states,
            // and it is the direction that was observed working correctly.
            setLevel();
        }
    }
}

/**
 * {@link addValueWithOverflow} from `@matter/general`, mirrored locally.
 *
 * `@matter/general` is not a direct dependency (only `@matter/main`/
 * `@matter/nodejs` are, per `package.json`) — it is a transitive one matter.js
 * itself imports the real function from (`Number.js:43-50`) to give
 * `stepHueLogic` its cyclic wraparound. The body below is branch-for-branch
 * the same three branches (variable names differ); duplicated rather than
 * reached into `node_modules` for, because a two-line arithmetic helper is
 * not worth a new declared dependency.
 */
function addWithOverflow(value: number, add: number, min: number, max: number): number {
    const next = value + add;
    if (next < min) {
        return next - min + max;
    }
    if (next > max) {
        return next - max + min;
    }
    return next;
}

/**
 * ColorControl, with every write-producing command converted to a `command`
 * event (#143's ColorControl half — LevelControl's is
 * {@link IndigoLevelControlServer}).
 *
 * **There is no single funnel here** — unlike LevelControl's `transition()`,
 * ColorControl's `#startTransition` is a private method (P-4), so the
 * overridable surface is the `*Logic` methods each command dispatches to.
 * The dispatch map, measured against 0.17.8's source: `moveToHue` →
 * `moveToHueLogic` (+ a `switchColorMode` call when the mode differs);
 * `moveHue`/`stepHue` → `moveHueLogic`/`stepHueLogic`, same pattern for
 * saturation; `moveToHueAndSaturation` → COMPOSES `moveToHueLogic` and
 * `moveToSaturationLogic` (overridden as ONE method below — see there for
 * why); `moveToColor`/`moveColor`/`stepColor` → their `*Logic`s (+
 * `switchColorMode`); `moveToColorTemperature`/`moveColorTemperature`/
 * `stepColorTemperature` → their `*Logic`s; `stopMoveStep` →
 * `stopMoveStepLogic`.
 *
 * **Enhanced-hue and ColorLoop are unreachable on the role this class itself
 * serves (`extendedColorLight`), and on the CT twin's role
 * (`colorTemperatureLight`, {@link IndigoColorTemperatureControlServer}) —
 * this class serves exactly ONE role, not two.** `colorTemperatureLight`
 * enables only the `ColorTemperature` feature and `extendedColorLight`
 * enables `HueSaturation`+`Xy`+`ColorTemperature`, never `EnhancedHue`/
 * `ColorLoop`. That absence is the one respect in which this class's
 * feature set matches the stock device type's own (HR-1) — `HueSaturation`
 * itself is a deliberate ADDITION over bare stock, not a match (stock builds
 * `extendedColorLight` with `Xy`+`ColorTemperature` only; see
 * {@link ROLE_DEFINITIONS}'s own `extendedColorLight` entry) — so
 * `isEnhancedHue` is always `false` when these overrides run;
 * `moveToHueLogic`/`stepHueLogic` still
 * take the parameter (shared with `enhancedMoveToHue`/`enhancedStepHue` in
 * the base class) and return early if it is ever seen `true`, defensively.
 *
 * **`switchColorMode` is overridden to a no-op, deliberately dropping a
 * measured 0.17.8 bug along with it.** Its stock job is optimistically
 * rewriting the OTHER colour representation's attributes locally when the
 * ecosystem switches which one it drives — an unconfirmed claim about the
 * lamp that Indigo never made, and measurably WRONG in one direction: the
 * xy→HS branch calls `hsvToXy` where `xyToHsv` is needed
 * (`ColorControlServer.js:1244-1246`, version-scoped to 0.17.8), which
 * silently moved a measured `currentSaturation` from 200 to 82 in the probe
 * that found it. `setColorMode`/`setEnhancedColorMode` themselves stay
 * stock — `colorMode` is a statement about which channel the ecosystem is
 * currently driving, and `colorPatch` (this file) rewrites it correctly on
 * Indigo's next push regardless of what `switchColorMode` would have guessed.
 *
 * **Continuous `move*` commands with no target value write nothing today,
 * confirmed by probe, and are left stock**: `moveHueLogic`, `moveSaturationLogic`
 * and `moveColorLogic` each call `#startTransition` with no `targetValue`,
 * and `Transitions.start` (unmanaged) returns immediately when the target is
 * absent (`Transitions.js:72-74`) — there is nothing to report because
 * nothing happened. `stepColorLogic` looked like it belonged on that list too
 * (§4.2 P-4 grouped it there) but a probe of this exact matter.js version
 * proved otherwise — it passes `targetValue: currentX + stepX`, which DOES
 * reach `transitionImmediately` and silently move `currentX`/`currentY`
 * (nothing watches them) — so it is overridden to a debug-logged no-op below,
 * per HR-6, rather than left stock on the strength of the general pattern.
 *
 * `stopMoveStepLogic`/`stopHueAndSaturationMovement`/`stopAllColorMovement`
 * stay stock: with nothing here ever starting a transition (every write path
 * is overridden to `emitCommand` instead of `#startTransition`), there is
 * nothing running for them to stop.
 *
 * `syncColorTemperatureWithLevelLogic` is unreachable and left stock: its
 * only caller is `LevelControlServer.couple()` (`LevelControlServer.js:
 * 400-412`), which {@link IndigoLevelControlServer}'s `transition()` override
 * never calls.
 *
 * **`.alter({ attributes: { remainingTime: { optional: false } } })` is a
 * genuine stock mandate this class was missing, not a new requirement this
 * PR invents.** Every `extendedColorLight`-shaped ColorControl server the
 * Matter spec defines carries `RemainingTime` as non-optional
 * (`extended-color-light.js:32`, HR-1); the bare `.with(…)` above it had no
 * such alter, so `remainingTime` was silently absent from `attributeList` —
 * a pre-existing HR-1 deviation the self-referential fence test used to mask
 * (see the cluster-composition test's own doc) rather than a regression this
 * PR introduces. Attribute addition only: no accessory recreation, no
 * migration, matching {@link IndigoColorTemperatureControlServer}'s own copy
 * of the same alter.
 */
class IndigoColorControlServer extends ColorControlServer.with("HueSaturation", "Xy", "ColorTemperature").alter({
    attributes: { remainingTime: { optional: false } },
}) {
    override moveToHueLogic(
        targetHue: number,
        _direction: ColorControl.Direction,
        _transitionTime: number,
        isEnhancedHue = false,
    ): MaybePromise {
        if (isEnhancedHue) {
            return;
        }
        const hue = clamp(targetHue, 0, MATTER_LEVEL_MAX);
        emitCommand(this.endpoint.id, "setColor", {
            hue: matterHueToDegrees(hue),
            saturation: matterToPercent(numberOr(this.state.currentSaturation, 0)),
        });
    }

    override stepHueLogic(
        stepMode: ColorControl.StepMode,
        stepSize: number,
        _transitionTime: number,
        isEnhancedHue = false,
    ): MaybePromise {
        if (isEnhancedHue) {
            return;
        }
        const direction = stepMode === ColorControl.StepMode.Up ? 1 : -1;
        // Cyclic wraparound — mirrors stock's own addValueWithOverflow call
        // here (ColorControlServer.js:402), see {@link addWithOverflow}.
        const hue = addWithOverflow(numberOr(this.state.currentHue, 0), stepSize * direction, 0, MATTER_LEVEL_MAX);
        emitCommand(this.endpoint.id, "setColor", {
            hue: matterHueToDegrees(hue),
            saturation: matterToPercent(numberOr(this.state.currentSaturation, 0)),
        });
    }

    override moveToSaturationLogic(targetSaturation: number, _transitionTime: number): MaybePromise {
        const saturation = clamp(targetSaturation, 0, MATTER_LEVEL_MAX);
        emitCommand(this.endpoint.id, "setColor", {
            hue: matterHueToDegrees(numberOr(this.state.currentHue, 0)),
            saturation: matterToPercent(saturation),
        });
    }

    override stepSaturationLogic(
        stepMode: ColorControl.StepMode,
        stepSize: number,
        _transitionTime: number,
    ): MaybePromise {
        const direction = stepMode === ColorControl.StepMode.Up ? 1 : -1;
        const saturation = clamp(numberOr(this.state.currentSaturation, 0) + stepSize * direction, 0, MATTER_LEVEL_MAX);
        emitCommand(this.endpoint.id, "setColor", {
            hue: matterHueToDegrees(numberOr(this.state.currentHue, 0)),
            saturation: matterToPercent(saturation),
        });
    }

    /**
     * Overridden as ONE method, not left to compose {@link moveToHueLogic}
     * and {@link moveToSaturationLogic} the way stock does
     * (`ColorControlServer.js:577-582`). Composing them here would emit TWO
     * `setColor` frames, and the second would read the STALE hue back out of
     * `this.state.currentHue` — nothing here writes the attribute any more,
     * so "the hue moveToHueLogic just reported" and "the hue
     * moveToSaturationLogic reads" would permanently disagree. One frame
     * carrying both requested values is also strictly less than stock's own
     * double emission — the pre-#143 shape {@link WATCH_HUE}'s doc now
     * records as backstop history.
     */
    override moveToHueAndSaturationLogic(
        targetHue: number,
        targetSaturation: number,
        _transitionTime: number,
    ): MaybePromise {
        const hue = clamp(targetHue, 0, MATTER_LEVEL_MAX);
        const saturation = clamp(targetSaturation, 0, MATTER_LEVEL_MAX);
        emitCommand(this.endpoint.id, "setColor", {
            hue: matterHueToDegrees(hue),
            saturation: matterToPercent(saturation),
        });
    }

    /**
     * CIE xy → §4.2 `setColor` (#143 commit 6). There is no xy vocabulary in
     * §4.2 — `setColor` only ever carries hue/saturation — so an ecosystem
     * driving the light in its xy representation is converted through
     * `xyToHsv`, the cluster's own conversion utility for exactly this
     * direction (`ColorConversionUtils.js`, re-exported from
     * `@matter/main/behaviors/color-control`) — NOT the conversion
     * `switchColorMode` actually calls today (that direction is the measured
     * 0.17.8 bug {@link switchColorMode}'s override drops, see the class
     * doc), but the one it was meant to use, and the one this file would
     * otherwise have to invent a duplicate of.
     *
     * `targetX`/`targetY` arrive as raw Matter `uint16` values (0-65279, see
     * `MAX_CIE_XY_VALUE`); `xyToHsv` expects the 0..1 CIE fraction the same
     * way the class getters `x`/`y` do (`#returnAsXyValue`,
     * `ColorControlServer.js:1449`: `value / 65536`) — dividing by
     * {@link CIE_XY_SCALE} before calling it is not optional, it is the unit
     * `xyToHsv` was written against.
     */
    override moveToColorLogic(targetX: number, targetY: number, _transitionTime: number): MaybePromise {
        const [hueDegrees, saturationFraction] = xyToHsv(targetX / CIE_XY_SCALE, targetY / CIE_XY_SCALE);
        emitCommand(this.endpoint.id, "setColor", {
            hue: clamp(roundHalfUp(hueDegrees), 0, HUE_DEGREES_MAX),
            saturation: clamp(roundHalfUp(saturationFraction * 100), 0, 100),
        });
    }

    override stepColorLogic(stepX: number, stepY: number, _transitionTime: number): MaybePromise {
        // See the class doc: unlike moveColorLogic, this DOES write today
        // (probe-confirmed) — currentX/currentY that nothing watches. Left
        // unconverted in this PR (commit 6 only lands moveToColorLogic); a
        // silent write is not an acceptable alternative (HR-6), so it is a
        // debug-logged no-op instead.
        logger.debug(
            `Endpoint ${this.endpoint.id}: dropped stepColor (stepX=${stepX}, stepY=${stepY}) — CIE xy step ` +
                "commands are not yet converted to §4.2 (only moveToColor is, #143)",
        );
    }

    override moveToColorTemperatureLogic(targetMireds: number, _transitionTime: number): MaybePromise {
        emitCommand(this.endpoint.id, "setColorTemp", { colorTempMireds: clampMireds(targetMireds) });
    }

    override moveColorTemperatureLogic(
        moveMode: ColorControl.MoveMode,
        _rate: number,
        colorTemperatureMinimumMireds: number,
        colorTemperatureMaximumMireds: number,
    ): MaybePromise {
        const target =
            moveMode === ColorControl.MoveMode.Up ? colorTemperatureMaximumMireds : colorTemperatureMinimumMireds;
        emitCommand(this.endpoint.id, "setColorTemp", { colorTempMireds: clampMireds(target) });
    }

    override stepColorTemperatureLogic(
        stepMode: ColorControl.StepMode,
        stepSize: number,
        _transitionTime: number,
        colorTemperatureMinimumMireds: number,
        colorTemperatureMaximumMireds: number,
    ): MaybePromise {
        const direction = stepMode === ColorControl.StepMode.Up ? 1 : -1;
        const target = clamp(
            numberOr(this.state.colorTemperatureMireds, MIREDS_MIN) + stepSize * direction,
            colorTemperatureMinimumMireds,
            colorTemperatureMaximumMireds,
        );
        emitCommand(this.endpoint.id, "setColorTemp", { colorTempMireds: clampMireds(target) });
    }

    override switchColorMode(_oldMode: ColorControl.ColorMode, _newMode: ColorControl.ColorMode): MaybePromise {
        // No-op — see class doc.
    }
}

/**
 * The `colorTemperatureLight` twin of {@link IndigoColorControlServer},
 * carrying only the `ColorTemperature` feature and the same `remainingTime`
 * mandatory-alter — matching the stock device type's own composition
 * byte-for-byte: `ColorControlServer.with("ColorTemperature").alter({
 * attributes: { remainingTime: { optional: false } } })`
 * (`color-temperature-light.js:32`, HR-1 — line 24 of the same requirements
 * table is the *LevelControl* alter that seeds `currentLevel: {min: 1, max:
 * 254}`; {@link IndigoLevelControlServer} deliberately does not mirror it,
 * relying instead on `LevelControlServer`'s own Lighting-feature `minLevel`
 * default and construction-time validation for the same 1-254 constraint —
 * see {@link percentToCurrentLevel}). Before this PR, {@link
 * IndigoColorControlServer} carried a BARE `ColorControlServer.with(…)` with
 * no `remainingTime` alter — a pre-existing HR-1 deviation this same commit
 * closes (see that class's own doc) — so `colorTemperatureLight` never had a
 * ColorControl override before #143 at all, and it is this class, not a
 * pre-existing one, whose composition must be pinned exactly or HR-1
 * regresses silently (caught by the cluster-composition test: without the
 * `.alter()`, `remainingTime` drops out of `attributeList` entirely).
 * `HueSaturation`/`Xy` do not exist on this role at all, so there is no
 * hue/saturation/xy surface to override: only the three colour-temperature
 * `*Logic` methods and `switchColorMode` (which can still fire between CT
 * and whatever mode the attribute started in, so it gets the same no-op for
 * the same reason as the full class).
 */
class IndigoColorTemperatureControlServer extends ColorControlServer.with("ColorTemperature").alter({
    attributes: { remainingTime: { optional: false } },
}) {
    override moveToColorTemperatureLogic(targetMireds: number, _transitionTime: number): MaybePromise {
        emitCommand(this.endpoint.id, "setColorTemp", { colorTempMireds: clampMireds(targetMireds) });
    }

    override moveColorTemperatureLogic(
        moveMode: ColorControl.MoveMode,
        _rate: number,
        colorTemperatureMinimumMireds: number,
        colorTemperatureMaximumMireds: number,
    ): MaybePromise {
        const target =
            moveMode === ColorControl.MoveMode.Up ? colorTemperatureMaximumMireds : colorTemperatureMinimumMireds;
        emitCommand(this.endpoint.id, "setColorTemp", { colorTempMireds: clampMireds(target) });
    }

    override stepColorTemperatureLogic(
        stepMode: ColorControl.StepMode,
        stepSize: number,
        _transitionTime: number,
        colorTemperatureMinimumMireds: number,
        colorTemperatureMaximumMireds: number,
    ): MaybePromise {
        const direction = stepMode === ColorControl.StepMode.Up ? 1 : -1;
        const target = clamp(
            numberOr(this.state.colorTemperatureMireds, MIREDS_MIN) + stepSize * direction,
            colorTemperatureMinimumMireds,
            colorTemperatureMaximumMireds,
        );
        emitCommand(this.endpoint.id, "setColorTemp", { colorTempMireds: clampMireds(target) });
    }

    override switchColorMode(_oldMode: ColorControl.ColorMode, _newMode: ColorControl.ColorMode): MaybePromise {
        // No-op — see IndigoColorControlServer's class doc.
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
 *
 * Two things the stock server does that this one still must, and does
 * elsewhere: it **fails** an invocation it cannot deliver (the `true` argument
 * to {@link emitCommand} — see there for why a lock is the one role that gets
 * that), and it emits the spec-mandatory `LockOperation` event. The event is
 * raised from {@link emitLockOperation}, on the plugin's `set_state` rather
 * than on the invocation, because that is when the bolt actually moved.
 */
class IndigoDoorLockServer extends DoorLockServer.with() {
    override lockDoor(): MaybePromise {
        emitCommand(this.endpoint.id, "lock", {}, true);
    }

    override unlockDoor(): MaybePromise {
        emitCommand(this.endpoint.id, "unlock", {}, true);
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
 *
 * **What a stop does here, end to end, because it is not what it looks like.**
 * `handleStopMovement` settles `targetPosition…` onto `currentPosition…` so the
 * ecosystem stops animating the blind, and reports `stopMotion` to the plugin.
 * That settled position is the *last one Indigo told us*, not where the blind
 * came to rest — the node has no way to know that, and neither does Indigo
 * until the driver reports the move finished. The true position arrives on the
 * next `set_state`, which is also when the accessory stops showing a number
 * that was only ever a statement about the request. And on the plugin side a
 * dimmer-modelled covering has no stop action at all
 * (`export_handlers.WindowCoveringExport._stop_motion`), so for the common case
 * the blind keeps travelling to its original target and the "settled" position
 * is superseded within seconds.
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
 *
 * **`options: { executeIfOff: true }` — Simon's #235 ruling: colour-while-off
 * forwards, exactly like level-while-off.** The rule is one sentence: the
 * bridge sends, Indigo decides. Withholding the command loses the colour for
 * nothing; and when the write's driver-level consequence on an RGB-channel
 * lamp (Hue-class) is turning the light on — `_set_color` computes a
 * full-vibrance RGB write — that is Indigo/driver semantics doing what it
 * does for ANY colour write, not something the bridge should second-guess
 * ("if we send and Indigo turns on then no point handling it ourselves").
 * Two deliberate asymmetries survive plugin-side, both Indigo's own choices
 * rather than this gate's: `_set_color_temp` preserves an off lamp's
 * `whiteLevel` where one exists (a CT change on a white-channel lamp
 * normally stores without turning on — its docstring says why; RGB-only
 * lamps refuse it with a logged reason), while `_set_color` on RGB hardware
 * lights the lamp. Stock's
 * `#optionsAllowExecution` gate (`ColorControlServer.js:1444-1446`) would
 * otherwise silently drop direct colour commands while the accessory is
 * unconfirmed-off — under no-auto-confirm the `onOff` attribute means
 * "Indigo's last confirmation", so the gate's premise is broken here for the
 * same reason as on LevelControl. Scene recalls always bypassed the gate;
 * this seed makes direct commands consistent with them. `options` is a
 * writable attribute — an ecosystem writing it back to `false` restores the
 * stock drop, same known limit as the level seed.
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
        options: { executeIfOff: true },
        ...(hueSaturation ? { currentHue: 0, currentSaturation: 0 } : {}),
    };
}

/**
 * The LevelControl state every level-bearing role starts from.
 *
 * **`managedTransitionTimeHandling: false`.** Originally pinned so the echo
 * guard ({@link isEcosystemChange}) could tell "us" from "an ecosystem": with
 * managed transitions ON, matter.js drives a `moveToLevel` to its target in
 * steps of its own making, written offline, which the guard would have
 * swallowed as if the plugin had written them, and an ecosystem dimming a
 * lamp would never reach Indigo at all. #143's conversion retires that
 * argument — {@link IndigoLevelControlServer} overrides `transition()`
 * outright, so nothing here reads `currentLevel`'s change events any more —
 * but the pin is kept anyway, now purely belt-and-braces: it keeps the
 * override on the one code path it was written against (`Transitions.start`'s
 * unmanaged branch, which is what collapses every rate/transition time to a
 * single final value — see that class's own doc). 0.17.8 already defaults it
 * to `false`, so the pin still changes no behaviour today; it is here so a
 * future default flip cannot silently sever the whole `setLevel` family.
 * `ColorControl` defaults the same field `false` too, and it is deliberately
 * NOT pinned in {@link colorControlDefaults} — but that omission is a real
 * gap, not a non-issue the way it might read. `moveHueLogic`/
 * `moveSaturationLogic`/`moveColorLogic` are left STOCK (see
 * {@link IndigoColorControlServer}'s class doc) and are exactly the commands
 * this flag gates: today, with no target value, `Transitions.start`'s
 * unmanaged branch returns immediately and they write nothing
 * (probe-confirmed). A future matter.js default flip to `true` would start a
 * MANAGED transition instead, writing `currentHue`/`currentSaturation`/
 * `currentX`/`currentY` in an offline context {@link isEcosystemChange}
 * swallows as if the plugin itself had written it — silent local drift
 * between the attribute and what the plugin believes, with no `command`
 * event to catch it. Left unpinned rather than fixed here: unlike
 * {@link LEVEL_CONTROL_INITIAL}'s pin, which protects a path this bridge
 * actively overrides (`transition()`), there is no equivalent single
 * override on the ColorControl side to hang the same protection off.
 *
 * **`options: { executeIfOff: true, coupleColorTempToLevel: false }`.**
 * In Indigo, setting a brightness level on a device that is off natively
 * means "turn it on at that level" — that is not an edge case worth
 * refusing, it is the ordinary meaning of the command, and Indigo is the one
 * system here that actually knows it. `executeIfOff` is matter.js's own gate
 * for exactly this case (`#optionsAllowExecution`, `LevelControlServer.js:
 * 443-445`): left at the cluster's own default (`false`), a
 * `moveToLevel`/`move`/`step` sent while the OnOff *attribute* still reads
 * `false` is silently dropped before {@link IndigoLevelControlServer} ever
 * sees it. Seeding the option `true` lets the command reach the override,
 * which reports it as `setLevel`; the confirmation loop is the same
 * no-auto-confirm shape as everywhere else in this file — Indigo turns the
 * device on, pushes `onOff: true` alongside the level, and the tile catches
 * up once that push lands, not before. KNOWN LIMITS: `options` is itself a
 * writable attribute (an ecosystem could in principle write it back to
 * `false` — pinned as a real test, not just a comment, in "executeIfOff is
 * itself writable…"), and a *plain* `MoveToLevel` (no `WithOnOff`) on a
 * genuinely-off lamp now forwards where it used to be dropped — harmless in
 * practice, since Apple's brightness slider sends `MoveToLevelWithOnOff`,
 * which was never gated in the first place, so the common path is
 * unaffected either way.
 * `coupleColorTempToLevel: false` is the cluster's own default restated, not
 * a behaviour change — it is here so a reader chasing why `couple()`'s
 * colour-temperature branch (`LevelControlServer.js:400-412`) never fires
 * does not have to go looking: it is off, on purpose, because this bridge
 * has no notion of "colour temperature follows brightness" to offer.
 */
const LEVEL_CONTROL_INITIAL = {
    currentLevel: 1,
    managedTransitionTimeHandling: false,
    options: { executeIfOff: true, coupleColorTempToLevel: false },
};

/**
 * The PowerSource state every battery-bearing endpoint starts from (issue
 * #220).
 *
 * **`batPercentRemaining: null` is REQUIRED, not a style choice — this is the
 * measured trap (§0).** An endpoint built WITHOUT this key never gets
 * attribute 12 (`batPercentRemaining`) added to its `attributeList` at all: a
 * LATER `endpoint.set({powerSource: {batPercentRemaining: 48}})` SUCCEEDS
 * SILENTLY and reads back 48 on this process, but the attribute was never
 * declared, so no controller — Apple, Alexa, anyone — can ever read it. `null`
 * (meaning "has a battery, reading unknown") seeds the attribute's existence;
 * a real percentage arrives with the first `set_state`/`createEndpoint`
 * `states` that carries one.
 *
 * `batReplacementNeeded: false` is pinned for the same reason
 * {@link LEVEL_CONTROL_INITIAL} pins `managedTransitionTimeHandling`: 0.17.8
 * already defaults it to `false` (`PowerSourceBaseServer` seeds nothing for
 * it, so the cluster's own default applies), so this changes no behaviour
 * today — it is here so a future matter.js default flip cannot silently start
 * every exported battery device out claiming it needs a new cell.
 *
 * **`batChargeLevel` is deliberately ABSENT here, and never derived from the
 * percentage.** The enum (`Ok`/`Warning`/`Critical`) is the device's OWN
 * judgement against ITS OWN thresholds — Indigo has none, so inventing a
 * mapping (e.g. "below 20% is Warning") would be this bridge's opinion
 * standing in for a reading no Indigo device provides, and a wrong guess
 * triggers an ecosystem's "replace battery" alarm on a device that never
 * asked for one. Apple derives its own low-battery warning from the
 * percentage already, so leaving the enum unset costs nothing and keeps one
 * source of truth (the percentage) instead of two that can disagree.
 * `status`/`order`/`description`/`batReplaceability` are left to matter.js
 * entirely: all four were measured present with sane defaults on
 * `PowerSourceServer.with("Battery")` (§0 — `status: 0`, `order: 0`,
 * `description: "Battery power"`, `batReplaceability: 0`) and none of them
 * are Indigo's to set.
 */
const BATTERY_INITIAL = { batPercentRemaining: null, batReplacementNeeded: false };

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
     * Cluster *events* the role owes the ecosystem once a `set_state` has
     * landed, given the patch that was written and the behaviour state as it
     * was before. Only `doorLock` has one; see {@link emitLockOperation}.
     *
     * Separate from {@link statePatch} because an event is not a state: it
     * needs the endpoint, it needs the previous value to know whether anything
     * moved, and it must be raised *after* the write so an ecosystem reading
     * the attribute back in response sees the new value.
     */
    afterApply?: (
        endpoint: Endpoint,
        patch: Record<string, unknown>,
        before: Record<string, unknown>,
    ) => Promise<void>;
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
 * `currentLevel` → §4.2 `setLevel {"level": 0-100}`.
 *
 * **A backstop, not the producer, as of #143.** `moveToLevel`/`step`/`move`
 * and their `WithOnOff` twins are now reported by
 * {@link IndigoLevelControlServer}'s `transition()` override, which never
 * writes `currentLevel` at all — so under normal operation this observable
 * never fires. It stays wired for two reasons: (1) a regression detector — if
 * a future matter.js upgrade ever starts writing this attribute in a remote
 * context again (a rewritten `LevelControlServer`, a new code path this PR
 * did not anticipate), a duplicate `setLevel` frame that Indigo treats
 * idempotently is a far better failure than a silent, permanent loss of the
 * whole `setLevel` family; (2) {@link watchCommands}'s throw-on-missing-
 * observable check is itself a live contract test — deleting the watcher
 * would also delete the guarantee that `currentLevel$Changed` still exists on
 * this matter.js version.
 */
const WATCH_LEVEL: WatchSpec = {
    behavior: LEVEL_CONTROL,
    attribute: "currentLevel",
    command: value =>
        typeof value === "number" ? { command: "setLevel", args: { level: matterToPercent(value) } } : undefined,
};

/** A backstop, not the producer, as of #143 — same reasoning as {@link WATCH_LEVEL}. */
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
 * observable reads its partner out of the endpoint's current state.
 *
 * **A backstop, not the producer, as of #143 — same status as
 * {@link WATCH_LEVEL}, for the identical reason.** `moveToHue`/`stepHue`/
 * `moveToSaturation`/`stepSaturation`/`moveToHueAndSaturation` are now
 * reported by {@link IndigoColorControlServer}'s `*Logic` overrides, which
 * never write `currentHue`/`currentSaturation` at all, so under normal
 * operation neither observable fires. Kept wired for the same two reasons
 * {@link WATCH_LEVEL} is: a regression detector for a future matter.js
 * version that starts writing these attributes again in a remote context,
 * and because {@link watchCommands}'s throw-on-missing-observable check is
 * itself a live contract test that `currentHue$Changed`/
 * `currentSaturation$Changed` still exist on this matter.js version.
 *
 * Before #143's ColorControl conversion, this pair *was* the producer, and
 * Matter changing both in one transaction for `moveToHueAndSaturation` meant
 * two `setColor` emissions with the same final values for one ecosystem
 * command — harmless (the plugin applies the same colour twice), but worth
 * closing anyway; see {@link IndigoColorControlServer.moveToHueAndSaturationLogic}
 * for how the live producer now avoids it in one frame instead of two.
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
 * The SmokeCoAlarm patch (issue #179) — the one sensor patch that writes two
 * attributes from a single key.
 *
 * `expressedState` is a mandatory attribute and is the cluster's answer to "is
 * this alarming"; `smokeState`/`coState` carry which sensor said so. They must
 * move together — the specification is explicit that a non-`Normal`
 * `expressedState` requires the corresponding attribute to be non-`Normal`
 * too — and the plugin cannot send `expressedState` because Indigo has nothing
 * to derive it from, so this is where the pair is kept consistent.
 *
 * `alarming` maps to `Critical`, not `Warning`: Matter's middle tier has no
 * Indigo counterpart and choosing it would invent a severity nothing measured.
 */
function alarmStatePatch(
    key: string,
    attribute: string,
    expressed: SmokeCoAlarm.ExpressedState,
): (states: Record<string, unknown>) => Record<string, unknown> {
    return states =>
        typeof states[key] === "boolean"
            ? {
                  [SMOKE_CO_ALARM]: {
                      [attribute]: states[key] ? SmokeCoAlarm.AlarmState.Critical : SmokeCoAlarm.AlarmState.Normal,
                      expressedState: states[key] ? expressed : SmokeCoAlarm.ExpressedState.Normal,
                  },
              }
            : {};
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
        thermostat.occupiedHeatingSetpoint = clampSetpoint(states.heatingSetpointC);
    }
    if (typeof states.coolingSetpointC === "number") {
        thermostat.occupiedCoolingSetpoint = clampSetpoint(states.coolingSetpointC);
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

/**
 * Raise the spec-mandatory `LockOperation` event when a push moved the bolt.
 *
 * Matter requires a DoorLock server to report operations as events, and
 * ecosystems use them for their activity history and for lock notifications —
 * Home's "the front door was unlocked" line is this event, not the `lockState`
 * attribute. {@link IndigoDoorLockServer} removed matter.js's emission along
 * with its auto-confirmation, which was right (the stock server fires it the
 * instant the *command* arrives, i.e. before Indigo, the mesh or the bolt have
 * agreed on anything) but left the bridge silent where the spec says it must
 * speak.
 *
 * So it is raised **here**, on the plugin's `set_state`, which is the moment
 * the bolt is known to have actually moved. That is the honest timing and it is
 * the only timing a bridge can offer.
 *
 * `operationSource` is `Unspecified`, not `Manual` or `Remote`, and every id
 * field is null. By the time Indigo reports a lock state change, what caused it
 * — an ecosystem command we forwarded, a key in the door, a schedule in another
 * plugin — has been flattened into one boolean. The enum has a value for "we do
 * not know" and using it beats naming a source we would be guessing at; the
 * `fabricIndex`/`sourceNode` "shall NOT be null" rule applies only to
 * `operationSource: Remote`, which is exactly the claim we are declining to
 * make.
 */
async function emitLockOperation(
    endpoint: Endpoint,
    patch: Record<string, unknown>,
    before: Record<string, unknown>,
): Promise<void> {
    const next = (patch[DOOR_LOCK] as Record<string, unknown> | undefined)?.lockState;
    const previous = (before[DOOR_LOCK] as Record<string, unknown> | undefined)?.lockState;
    if (next === undefined || next === previous) {
        return;
    }
    await endpoint.act(agent => {
        agent.get(IndigoDoorLockServer).events.lockOperation.emit(
            {
                lockOperationType:
                    next === DoorLock.LockState.Locked
                        ? DoorLock.LockOperationType.Lock
                        : DoorLock.LockOperationType.Unlock,
                operationSource: DoorLock.OperationSource.Unspecified,
                userIndex: null,
                fabricIndex: null,
                sourceNode: null,
                credentials: null,
            },
            agent.context,
        );
    });
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
 *
 * **The setpoint limits and the deadband are seeded for the same reason the
 * enums are — matter.js's fallback is wrong for a bridge — but they fail far
 * more quietly, so they are worth their own paragraph.**
 *
 * The four `abs…SetpointLimit` attributes are a *manufacturer* claim about a
 * physical thermostat's capability, and the `min…`/`max…` pair is the user
 * narrowing it. Left unset, matter.js applies the cluster defaults (7–30 °C
 * heating, 16–32 °C cooling) and enforces them, which is a claim this bridge
 * has no business making about someone else's hardware — see
 * {@link SETPOINT_CENTI_MIN}. All eight are seeded so the advertised band and
 * {@link clampSetpoint} are the same band, stated once.
 *
 * `minSetpointDeadBand` is the AutoMode rule that heating and cooling stay at
 * least N apart, and its default is 2 °C. **matter.js does not refuse a write
 * that breaks it — it silently rewrites the other setpoint** (per the spec:
 * "the value of OccupiedHeatingSetpoint shall be adjusted"). Pushing a
 * thermostat Indigo reports as heat 21 / cool 21 therefore left the node
 * holding heat 19, an offline write that raises no `command` event, so the
 * plugin never learned and never corrected: the ecosystem showed a setpoint
 * two degrees below the real one, permanently, with nothing in any log. A
 * bridge does not schedule anything and has no HVAC of its own to protect from
 * short-cycling, so the honest deadband is 0 — report what Indigo says, exactly.
 */
function thermostatDefaults(): Record<string, unknown> {
    return {
        systemMode: Thermostat.SystemMode.Off,
        controlSequenceOfOperation: Thermostat.ControlSequenceOfOperation.CoolingAndHeating,
        absMinHeatSetpointLimit: SETPOINT_CENTI_MIN,
        absMaxHeatSetpointLimit: SETPOINT_CENTI_MAX,
        minHeatSetpointLimit: SETPOINT_CENTI_MIN,
        maxHeatSetpointLimit: SETPOINT_CENTI_MAX,
        absMinCoolSetpointLimit: SETPOINT_CENTI_MIN,
        absMaxCoolSetpointLimit: SETPOINT_CENTI_MAX,
        minCoolSetpointLimit: SETPOINT_CENTI_MIN,
        maxCoolSetpointLimit: SETPOINT_CENTI_MAX,
        minSetpointDeadBand: 0,
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
    // #143: every OnOff-bearing role below adds IndigoOnOffServer, and every
    // level/colour-bearing one also adds IndigoLevelControlServer and an
    // IndigoColorControlServer/IndigoColorTemperatureControlServer, so the
    // ecosystem's on/off/toggle/timed/level/colour commands all report
    // through the sink instead of writing an attribute themselves.
    // WATCH_ON_OFF/WATCH_LEVEL/WATCH_COLOR_TEMP/WATCH_HUE/WATCH_SATURATION
    // stay wired too, now purely as backstops — see their own docs.
    [Role.onOffPlugInUnit]: {
        deviceType: () => OnOffPlugInUnitDevice.with(BridgedDeviceBasicInformationServer, IndigoOnOffServer),
        initialState: () => ({}),
        statePatch: onOffPatch,
        watch: [WATCH_ON_OFF],
    },
    [Role.onOffLight]: {
        deviceType: () => OnOffLightDevice.with(BridgedDeviceBasicInformationServer, IndigoOnOffServer),
        initialState: () => ({}),
        statePatch: onOffPatch,
        watch: [WATCH_ON_OFF],
    },
    [Role.dimmableLight]: {
        deviceType: () =>
            DimmableLightDevice.with(BridgedDeviceBasicInformationServer, IndigoOnOffServer, IndigoLevelControlServer),
        initialState: () => ({ [LEVEL_CONTROL]: { ...LEVEL_CONTROL_INITIAL } }),
        statePatch: states => ({ ...onOffPatch(states), ...levelPatch(states) }),
        watch: [WATCH_ON_OFF, WATCH_LEVEL],
    },
    [Role.colorTemperatureLight]: {
        deviceType: () =>
            ColorTemperatureLightDevice.with(
                BridgedDeviceBasicInformationServer,
                IndigoOnOffServer,
                IndigoLevelControlServer,
                IndigoColorTemperatureControlServer,
            ),
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
                IndigoOnOffServer,
                IndigoLevelControlServer,
                IndigoColorControlServer,
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
        // OccupancyEvent makes matter.js auto-emit OccupancyChanged whenever
        // `occupancy` changes — the Occupancy Sensor device type's change
        // signal, and what event-driven controllers (Alexa — inferred from
        // #276's instrumentation) act on rather than polling the attribute.
        // Without it the accessory appears but never updates there (#276).
        // Wire delta is FeatureMap 0x0002→0x0202: the event enters the
        // cluster's event set and controllers discover it from that bit —
        // EventList (0xFFFA) is deprecated and matter.js never serves it, so
        // there is no EventList change on the wire. Not a cluster-set
        // change — the planner keys purely on role + battery from the spec
        // (reconcile.ts), so an upgrade under the same role is still an
        // ordinary update, not a recreate (ADR-0011 doctrine).
        deviceType: () =>
            OccupancySensorDevice.with(
                BridgedDeviceBasicInformationServer,
                OccupancySensingServer.with("PassiveInfrared", "OccupancyEvent"),
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
    // The leak family (issue #236). One cluster, three device types: each
    // ships BooleanState as a MANDATORY behaviour already present in the
    // device definition — unlike `occupancySensor`, whose cluster has to be
    // declared with a modality feature — so these are the smallest role
    // definitions in the file, and deliberately identical to each other.
    //
    // The polarity is NOT contactSensor's. `stateValue` in a detector device
    // type is documented "true = detected", where the same attribute in a
    // Contact Sensor is "true = closed"; §4.2 gives each its own state key so
    // the two readings can never be confused across a role change.
    [Role.waterLeakDetector]: {
        deviceType: () => WaterLeakDetectorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: booleanStatePatch(BOOLEAN_STATE, "leak", "stateValue", leak => leak),
        watch: [],
    },
    [Role.waterFreezeDetector]: {
        deviceType: () => WaterFreezeDetectorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: booleanStatePatch(BOOLEAN_STATE, "freeze", "stateValue", freeze => freeze),
        watch: [],
    },
    [Role.rainSensor]: {
        deviceType: () => RainSensorDevice.with(BridgedDeviceBasicInformationServer),
        initialState: () => ({}),
        statePatch: booleanStatePatch(BOOLEAN_STATE, "rain", "stateValue", rain => rain),
        watch: [],
    },
    // Smoke and CO (issue #179). One Matter device type (Smoke CO Alarm
    // 0x0076) whose sensing half is selected by cluster FEATURE, so the two
    // roles differ only in which feature they declare and which attribute
    // they drive. Two roles rather than one dual-feature role because an
    // Indigo sensor is a single boolean meaning a single thing: a smoke-only
    // sensor exported with both features would sit there telling every
    // ecosystem its CO reading is Normal, which is a claim nothing measured.
    //
    // Unlike every other sensor role, the patch writes TWO attributes. The
    // cluster's `expressedState` is mandatory and is what a controller reads
    // to answer "is this alarming"; leaving it at the server's seeded `Normal`
    // while `smokeState` went `Critical` would be an accessory that is
    // alarming and reports calm. The plugin never sends `expressedState` —
    // there is nothing in Indigo to send it from — so deriving it here is the
    // only place the two can be kept in lockstep.
    //
    // Severity collapses to Normal/Critical: Matter's middle `Warning` tier
    // has no Indigo counterpart, and picking it for some boolean would be
    // inventing a severity no device reported. `SelfTestRequest` is the
    // cluster's only command and its conformance is "O" — not declared, so
    // these stay read-only like every other exported sensor.
    [Role.smokeAlarm]: {
        deviceType: () =>
            SmokeCoAlarmDevice.with(
                BridgedDeviceBasicInformationServer,
                SmokeCoAlarmServer.with("SmokeAlarm"),
            ),
        initialState: () => ({}),
        statePatch: alarmStatePatch("smoke", "smokeState", SmokeCoAlarm.ExpressedState.SmokeAlarm),
        watch: [],
    },
    [Role.coAlarm]: {
        deviceType: () =>
            SmokeCoAlarmDevice.with(
                BridgedDeviceBasicInformationServer,
                SmokeCoAlarmServer.with("CoAlarm"),
            ),
        initialState: () => ({}),
        statePatch: alarmStatePatch("co", "coState", SmokeCoAlarm.ExpressedState.CoAlarm),
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
        afterApply: emitLockOperation,
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
 * A role's device type, optionally carrying PowerSource (§4.1 `battery`,
 * issue #220).
 *
 * Always `.with("Battery")`, never the bare `PowerSourceServer` — the pattern
 * every other role in this table already follows for a feature-narrowed
 * server (`OnOffServer.with("Lighting")`, `ColorControlServer.with(
 * "HueSaturation", …)`), and 0.18 readiness besides.
 *
 * `RoleDefinition.deviceType` is typed as the base {@link EndpointType} "on
 * purpose" (see that field's own doc) because each role's `.with()` produces a
 * different concrete type — but every one of them is, at runtime, the
 * {@link MutableEndpoint} every `@matter/main/devices/*` export actually is,
 * which is what lets a SECOND `.with()` be chained on here exactly the way
 * {@link createEndpoint} already assumes when it hands the result to
 * `new Endpoint(... as never, …)` two lines below. Measured (§0): chaining
 * onto an already-`.with()`-ed device type retains the endpoint number across
 * a close+re-add at the same `Endpoint.id`, adds 47 to `serverList` and
 * `{deviceType: 17}` (0x0011) to `deviceTypeList` (via
 * `PowerSourceServer#initialize`'s own `addDeviceTypes("PowerSource")`) — and
 * `bridged-node.js`'s optional server set carries PowerSource, which is the
 * sanctioned placement this composition relies on.
 */
function deviceTypeFor(definition: RoleDefinition, battery: boolean): EndpointType {
    const deviceType = definition.deviceType();
    if (!battery) {
        return deviceType;
    }
    return (deviceType as unknown as MutableEndpoint).with(PowerSourceServer.with("Battery"));
}

/**
 * {@link splitBattery}'s result: `battery` is the `powerSource` patch to merge
 * in, present only when `states` carried a consumable `batteryLevel`; `rest`
 * is everything else, unconsumed, for the role's own converter.
 */
interface BatterySplit {
    battery?: Record<string, unknown>;
    rest: Record<string, unknown>;
}

/**
 * Peel `batteryLevel` off a `states`/spec-`states` object and convert it to a
 * `powerSource` patch.
 *
 * **Role-independent by construction** (issue #220; `bridge_protocol.
 * SHARED_STATE_KEYS` is the Python mirror of this same rule): `batteryLevel`
 * is consumed HERE, before any role's own `statePatch` ever sees it, which is
 * what keeps it out of {@link ROLE_DEFINITIONS} entirely — fifteen identical
 * per-role additions would be exactly the copy-pasted-logic failure mode this
 * codebase's own conventions warn against.
 *
 * Only a `typeof === "number" && Number.isFinite` value is consumed —
 * deliberately NOT `typeof === "boolean"`, so `{"batteryLevel": true}` is left
 * alone. A non-numeric value (a string, a boolean) stays in `rest`, where no
 * role's converter can make sense of it either: it surfaces as an ordinary
 * rejected key through {@link rejectedStateKeys}/`statePatchOrRefuse`, the same
 * treatment a misspelt or mistyped key already gets — nothing here has to
 * duplicate that machinery.
 *
 * This function does NOT know or care whether the endpoint it is destined for
 * actually has a battery — it has no endpoint to ask. The composition check
 * (does *this* endpoint carry PowerSource) is the caller's job, because only
 * {@link createEndpoint} and {@link applyStates} have an endpoint/spec to
 * check it against; see {@link refuseBatteryLevelWithoutBattery}.
 */
function splitBattery(states: Record<string, unknown>): BatterySplit {
    const { batteryLevel, ...rest } = states;
    if (typeof batteryLevel !== "number" || !Number.isFinite(batteryLevel)) {
        return { rest: states };
    }
    return { battery: { batPercentRemaining: percentToBatteryRemaining(batteryLevel) }, rest };
}

/**
 * §4.1's refusal for a consumable `batteryLevel` against an endpoint that was
 * not created with a battery. Thrown BEFORE any write reaches matter.js — a
 * silent accept would write an attribute no controller can ever read, the same
 * lesson §0's measured trap teaches from the opposite direction (a write that
 * succeeds into an attribute that does not exist at all).
 */
function refuseBatteryLevelWithoutBattery(indigoDeviceId: number): never {
    throw new ProtocolError(
        ErrorCode.malformedArgs,
        `endpoint ${indigoDeviceId} was not created with a battery, so batteryLevel cannot be ` +
            "published; re-export the device (§4.1)",
    );
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

/**
 * Who the bridge says it is, on every child accessory it publishes.
 *
 * The values are the *bridge's* — they are not per-device, and there is nothing
 * per-device to put there: Indigo does not tell us who made a relay. Passed in
 * rather than imported so this module keeps no dependency on {@link ../node},
 * which imports it.
 */
export interface BridgedIdentity {
    vendorName: string;
    vendorId: number;
    productName: string;
    hardwareVersion: number;
    hardwareVersionString: string;
    softwareVersion: number;
    softwareVersionString: string;
}

/**
 * The Bridged Device Basic Information every child carries (PRD §5.3).
 *
 * **Every field below is optional in the cluster** (verified against
 * `@matter/types` 0.17.8's `bridged-device-basic-information.d.ts`: only
 * `reachable` is mandatory) — but populating the identity set is what
 * matterbridge does and what ecosystems render as an accessory's manufacturer,
 * model and firmware detail. Left off, those panes read blank or "Unknown",
 * which is a worse answer than "simons-plugins" for something that genuinely is
 * a software bridge.
 *
 * Note the cluster's optional set is NOT BasicInformation's: it has no
 * `productId`, no `partNumber`, no `capabilityMinima`. Nothing outside the list
 * above is added here for that reason.
 */
function bridgedInfoFor(spec: EndpointSpec, identity: BridgedIdentity): Record<string, unknown> {
    return {
        nodeLabel: spec.label,
        productName: identity.productName,
        productLabel: spec.label,
        serialNumber: serialNumberFrom(spec.publishedAs),
        uniqueId: spec.publishedAs,
        reachable: spec.reachable,
        // Branded, not a bare number: matter.js validates the tag at write time,
        // and the root node's BasicInformation already goes through VendorId().
        vendorName: identity.vendorName,
        vendorId: VendorId(identity.vendorId),
        // A bridge has no hardware, so this is a constant rather than an
        // invention — but the attributes are what an ecosystem reads for the
        // "Version" row, and an absent one renders as blank.
        hardwareVersion: identity.hardwareVersion,
        hardwareVersionString: identity.hardwareVersionString,
        // The bridge node's own version, same derivation the root node uses, so
        // an ecosystem showing a child's firmware and the bridge's own agree.
        softwareVersion: identity.softwareVersion,
        softwareVersionString: identity.softwareVersionString,
        // Optional on bridged devices, and matter.js only lets
        // `increaseConfigurationVersion` run when an initial value exists — so
        // it is seeded here rather than discovered as a throw on the first bump.
        //
        // **An INITIAL value, not the live one, and that distinction was
        // measured rather than assumed** (0.17.8, `registry.test.ts`
        // "restores a bridged accessory's ConfigurationVersion"). The data model
        // suggests otherwise: the bridged cluster's attribute carries no `N`
        // quality (`bridged-device-basic-information.element.js`), unlike the
        // root's, and `BridgedDeviceBasicInformationServer` extends only
        // `uniqueId` to be persistent — which reads as "matter.js will not
        // remember this, so every restart resets the accessory to 1". It does
        // remember it: the value is written to its own store as
        // `root.parts.aggregator.parts.<id>.bridgedDeviceBasicInformation.configurationVersion`
        // and restored over whatever is seeded here. So this constant applies
        // only the first time an endpoint id is ever created, and nothing needs
        // to persist it alongside the endpoint numbers.
        configurationVersion: 1,
    };
}

/**
 * Build the bridged child endpoint for one spec. Not yet added to a parent;
 * `Endpoint.id` is fixed at construction and is the identity matter.js keys its
 * persisted endpoint number on.
 */
export function createEndpoint(spec: EndpointSpec, identity: BridgedIdentity): Endpoint {
    const definition = definitionFor(spec.role);
    const { battery, rest } = splitBattery(spec.states);
    if (battery !== undefined && !spec.battery) {
        refuseBatteryLevelWithoutBattery(spec.indigoDeviceId);
    }
    // A consumed `batteryLevel` counts as consumption for refusal purposes
    // too, matching `applyStates`' version-skew tolerance below (the
    // "*partly* consumed `states` still succeeds" paragraph on that
    // function's own doc): without this, an initial `states` of
    // `{batteryLevel: 48, futureKey: 1}` left `rest = {futureKey: 1}`, which
    // `statePatchOrRefuse` refused WHOLESALE — a valid battery reading thrown
    // away alongside the stranger it arrived with, at construction time.
    const patch = battery !== undefined ? definition.statePatch(rest) : statePatchOrRefuse(spec.role, rest);
    return new Endpoint(deviceTypeFor(definition, spec.battery) as never, {
        id: spec.publishedAs,
        ...mergeBehaviors(
            { [BRIDGED_INFO]: bridgedInfoFor(spec, identity) },
            definition.initialState(),
            // `BATTERY_INITIAL` seeds `batPercentRemaining: null` even when
            // `battery` is undefined (no `batteryLevel` in the initial
            // `states`) — the measured trap (§0): an endpoint built without
            // this key never gets the attribute into `attributeList` at all,
            // so seeding it is not optional on a battery-flagged endpoint.
            spec.battery ? { [POWER_SOURCE]: { ...BATTERY_INITIAL, ...battery } } : {},
            patch,
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
    // `batteryLevel` is treated as always consumable here, regardless of
    // whether the (unknown, to this function) endpoint actually has a
    // battery: this is a pure function of role+states with no endpoint to
    // check composition against, and the golden fixtures carry a battery key
    // on ordinary roles' `attach`/`set_state` frames. The composition check
    // lives only in `createEndpoint`/`applyStates`, which do have one.
    const { rest } = splitBattery(states);
    const { statePatch } = definitionFor(role);
    return Object.keys(rest).filter(key => Object.keys(statePatch({ [key]: rest[key] })).length === 0);
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
    hasBattery: boolean,
    indigoDeviceId: number,
): Promise<void> {
    // Split FIRST, before `statePatchOrRefuse` sees `states` — the composition
    // check (does *this* endpoint have a battery) has to run before anything
    // is written, and `rest` (not `states`) is what the role's own converter
    // gets.
    const { battery, rest } = splitBattery(states);
    if (battery !== undefined && !hasBattery) {
        // Passed in by the caller (issues #219/#240) rather than derived from
        // `endpoint.id`: under a re-adopted published identity, `endpoint.id`
        // no longer parses back to the device driving it, and deriving from it
        // here would name the WRONG device — the old, deleted one — in the
        // refusal.
        refuseBatteryLevelWithoutBattery(indigoDeviceId);
    }
    // A consumed `batteryLevel` counts as consumption for refusal purposes
    // too — the version-skew tolerance this function's own doc describes
    // ("a *partly* consumed `states` still succeeds") is about ANY key this
    // bridge understood, not only the role's own vocabulary. Without this,
    // `{batteryLevel: 48, futureKey: 1}` on a battery endpoint left
    // `rest = {futureKey: 1}`, which `statePatchOrRefuse` refused wholesale —
    // a valid battery reading thrown away alongside the stranger it arrived
    // with. A push with no consumable battery key keeps the ordinary refusal.
    const patch = battery !== undefined ? definitionFor(role).statePatch(rest) : statePatchOrRefuse(role, rest);
    if (battery !== undefined) {
        // Rides the existing residual `endpoint.set()` below — a plain
        // attribute write, no command semantics, no `WatchSpec`, no third
        // transaction. A battery-only push has `rest = {}` (so `patch` is `{}`
        // here, lawfully — `statePatchOrRefuse`'s empty-states guard never saw
        // the peeled-off `batteryLevel` key); adding this key is what stops the
        // early-empty return just below from firing over a push that DOES have
        // something to say.
        patch[POWER_SOURCE] = battery;
    }
    if (Object.keys(patch).length === 0) {
        return;
    }

    // #201: the onOff attribute cannot be written directly any more (matter.js
    // 0.17.8 anchors throughout). A raw `endpoint.set()` write leaves
    // `timedOnTimer` running against a device Indigo has already turned off,
    // and `#stopHeldTimer` cannot help: it is wired only to
    // `onTime$Changed`/`offWaitTime$Changed` (OnOffServer.js:29-30), so a bare
    // onOff write never triggers it at all — and even a combined patch that
    // also zeroed `onTime` would miss, because its first branch requires
    // `state.onOff` to already be true (OnOffServer.js:43). Routing through
    // the behaviour instead reaches `off()`'s own `timedOnTimer.stop()`
    // (OnOffServer.js:85-88) and `on()`'s delayedOffTimer/offWaitTime cleanup
    // (OnOffServer.js:67-72). `applyIndigoOnOff` calls
    // `super.on()`/`super.off()`, so it can never re-enter the emitting
    // overrides in {@link IndigoOnOffServer} — see that class's doc for why no
    // context check is needed either. This is a second, separate transaction
    // from the `endpoint.set()` below: a combined `{onOff, level}` push
    // becomes two reports instead of one, which no reactor here minds.
    //
    // The two transactions can also fail independently — an act that landed
    // followed by a set that threw would leave on/off applied and level/colour
    // silently stale until the next push, which the single-set path could
    // never do. So a failed half never stops the other half from being
    // attempted, and the error names which half (or both) failed.
    const onOff = patch[ON_OFF] as { onOff?: boolean } | undefined;
    let onOffFailure: unknown;
    if (onOff !== undefined) {
        delete patch[ON_OFF];
        try {
            await endpoint.act(agent => agent.get(IndigoOnOffServer).applyIndigoOnOff(onOff.onOff === true));
        } catch (e) {
            onOffFailure = e;
        }
    }

    const { afterApply } = definitionFor(role);
    if (Object.keys(patch).length === 0) {
        // An onOff-only set_state is now fully handled above; `endpoint.set({})`
        // would be a pointless second write.
        if (onOffFailure !== undefined) {
            throw onOffFailure;
        }
        return;
    }
    const applyResidual = async (): Promise<void> => {
        if (afterApply === undefined) {
            await endpoint.set(patch as never);
            return;
        }
        // Read before the write, or "did this actually change anything" cannot be
        // answered — and only for the one role that asks, so no other push pays for
        // a state copy it would ignore. Keyed by behaviour like the patch itself,
        // and derived from the patch's own keys rather than a second table of
        // role → behaviour that could drift from `statePatch`.
        //
        // **Spread, not the object.** `stateOf` hands back a live view: hold onto it
        // across `set()` and "before" reads back as "after", every comparison finds
        // nothing changed, and the event this exists to raise never fires. Caught by
        // the LockOperation test, which is the only thing that would ever notice.
        const before: Record<string, unknown> = {};
        for (const behavior of Object.keys(patch)) {
            before[behavior] = { ...(endpoint.stateOf(behavior) as object) };
        }
        await endpoint.set(patch as never);
        await afterApply(endpoint, patch, before);
    };
    if (onOffFailure !== undefined) {
        try {
            await applyResidual();
        } catch (residualFailure) {
            throw new AggregateError(
                [onOffFailure, residualFailure],
                "set_state failed on both halves: the onOff act and the residual set",
            );
        }
        throw new Error(
            `set_state applied the residual attributes but the onOff half failed: ${String(onOffFailure)}`,
            { cause: onOffFailure },
        );
    }
    await applyResidual();
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
    spec: { indigoDeviceId: number; role: RoleValue; publishedAs: string },
    emit: (data: CommandEventData) => void,
    log: (message: string) => void = () => {},
): () => void {
    const teardown: (() => void)[] = [];
    // The published identity, not `endpointIdFor(spec.indigoDeviceId)`: under a
    // re-adopted identity (issue #219) the two differ, and `COMMAND_SINKS` is
    // keyed on `endpoint.id`/`Endpoint.id`, which IS `spec.publishedAs` (see
    // {@link createEndpoint}) — the sink has to be found under the same key.
    const endpointId = spec.publishedAs;
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
