/**
 * §4.2 unit conversions and the echo-guard predicate.
 *
 * Kept separate from `endpoints.test.ts` (which builds real Matter endpoints)
 * because these are pure arithmetic: the round-trip properties are the contract
 * the plugin relies on, and they should fail loudly without a Matter stack in
 * the way.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
    celsiusToMatter,
    clampMireds,
    clampSetpoint,
    cubicMetresPerHourToMatter,
    degreesToMatterHue,
    HUE_DEGREES_MAX,
    HUMIDITY_CENTI_MAX,
    humidityToMatter,
    ILLUMINANCE_MAX,
    ILLUMINANCE_MIN,
    indigoDeviceIdFrom,
    isEcosystemChange,
    kilopascalsToMatter,
    luxToMatter,
    MATTER_LEVEL_MAX,
    matterHueToDegrees,
    matterToCelsius,
    matterToCubicMetresPerHour,
    matterToHumidity,
    matterToKilopascals,
    matterToLux,
    matterToPercent,
    matterToSystemMode,
    MIREDS_MAX,
    MIREDS_MIN,
    percent100thsToPosition,
    PERCENT100THS_CLOSED,
    PERCENT100THS_OPEN,
    percentToCurrentLevel,
    percentToMatter,
    positionToPercent100ths,
    SETPOINT_CENTI_MAX,
    SETPOINT_CENTI_MIN,
    systemModeToMatter,
    uniqueIdFor,
    TEMPERATURE_CENTI_MIN,
} from "../src/endpoints.js";

/** 0..100 inclusive — the whole §4.2 percent domain, not a sample of it. */
const ALL_PERCENTS = Array.from({ length: 101 }, (_, index) => index);
const ALL_MATTER_LEVELS = Array.from({ length: MATTER_LEVEL_MAX + 1 }, (_, index) => index);

describe("level and saturation conversion (§4.2)", () => {
    it("round-trips every one of the 101 percentages exactly", () => {
        for (const percent of ALL_PERCENTS) {
            assert.equal(matterToPercent(percentToMatter(percent)), percent, `percent ${percent}`);
        }
    });

    it("pins the endpoints of the range", () => {
        assert.equal(percentToMatter(0), 0);
        assert.equal(percentToMatter(100), MATTER_LEVEL_MAX);
        assert.equal(matterToPercent(0), 0);
        assert.equal(matterToPercent(MATTER_LEVEL_MAX), 100);
    });

    it("rounds half up", () => {
        // 50% is 127 exactly; the halves either side are what the rule decides.
        assert.equal(percentToMatter(50), 127);
        // 0.5 steps: matter 127.5 would come from 50.196…%; check the inverse
        // direction where a genuine .5 occurs (level 89 → 35.039…, level 127 →
        // 50.0). Exact halves exist for the hue scale, tested below.
        assert.equal(matterToPercent(127), 50);
        assert.equal(matterToPercent(128), 50);
    });

    it("clamps out-of-range inputs rather than producing illegal Matter values", () => {
        assert.equal(percentToMatter(-5), 0);
        assert.equal(percentToMatter(500), MATTER_LEVEL_MAX);
        assert.equal(matterToPercent(-1), 0);
        assert.equal(matterToPercent(9999), 100);
    });

    it("never emits a percentage outside 0-100 for any Matter level", () => {
        for (const level of ALL_MATTER_LEVELS) {
            const percent = matterToPercent(level);
            assert.ok(Number.isInteger(percent) && percent >= 0 && percent <= 100, `level ${level} → ${percent}`);
        }
    });

    it("writes currentLevel as at least 1, which still reads back as 0%", () => {
        // The Lighting feature constrains currentLevel to 1-254 on all four
        // lighting device types, so 0% cannot be written literally. 1 is the
        // lossless substitute: it converts back to 0.
        assert.equal(percentToCurrentLevel(0), 1);
        assert.equal(matterToPercent(percentToCurrentLevel(0)), 0);
        for (const percent of ALL_PERCENTS) {
            const written = percentToCurrentLevel(percent);
            assert.ok(written >= 1 && written <= MATTER_LEVEL_MAX, `percent ${percent} → ${written}`);
            assert.equal(matterToPercent(written), percent, `percent ${percent} did not survive the floor`);
        }
    });
});

describe("colour temperature conversion (§4.2)", () => {
    it("clamps to the advertised physical bounds", () => {
        assert.equal(clampMireds(100), MIREDS_MIN);
        assert.equal(clampMireds(153), MIREDS_MIN);
        assert.equal(clampMireds(320), 320);
        assert.equal(clampMireds(500), MIREDS_MAX);
        assert.equal(clampMireds(10_000), MIREDS_MAX);
    });

    it("rounds a fractional mired to an integer", () => {
        assert.equal(clampMireds(319.4), 319);
        assert.equal(clampMireds(319.5), 320);
    });
});

describe("hue conversion (§4.2)", () => {
    it("pins the endpoints of the range", () => {
        assert.equal(degreesToMatterHue(0), 0);
        assert.equal(degreesToMatterHue(HUE_DEGREES_MAX), MATTER_LEVEL_MAX);
        assert.equal(matterHueToDegrees(0), 0);
        assert.equal(matterHueToDegrees(MATTER_LEVEL_MAX), HUE_DEGREES_MAX);
    });

    it("round-trips every degree to within one Matter step", () => {
        // 361 degrees do not fit in 255 steps, so this is a bounded-error
        // property, not an identity — unlike level and saturation. One step is
        // 360/254 ≈ 1.42°, so the tolerance is 1° after rounding.
        for (let degrees = 0; degrees <= HUE_DEGREES_MAX; degrees++) {
            const back = matterHueToDegrees(degreesToMatterHue(degrees));
            assert.ok(Math.abs(back - degrees) <= 1, `hue ${degrees} came back as ${back}`);
        }
    });

    it("clamps out-of-range degrees", () => {
        assert.equal(degreesToMatterHue(-1), 0);
        assert.equal(degreesToMatterHue(720), MATTER_LEVEL_MAX);
    });

    it("never emits a hue outside 0-360 for any Matter value", () => {
        for (const hue of ALL_MATTER_LEVELS) {
            const degrees = matterHueToDegrees(hue);
            assert.ok(Number.isInteger(degrees) && degrees >= 0 && degrees <= HUE_DEGREES_MAX, `hue ${hue}`);
        }
    });
});

describe("temperature conversion (§4.2, E4)", () => {
    it("uses the 0.01 °C scale in both directions", () => {
        assert.equal(celsiusToMatter(21.5), 2150);
        assert.equal(celsiusToMatter(0), 0);
        assert.equal(matterToCelsius(2150), 21.5);
    });

    it("round-trips every tenth of a degree over a household range", () => {
        for (let tenths = -400; tenths <= 500; tenths++) {
            const celsius = tenths / 10;
            assert.equal(matterToCelsius(celsiusToMatter(celsius)), celsius, `${celsius} °C`);
        }
    });

    it("handles negatives without the half-up rule drifting", () => {
        // roundHalfUp is floor(v + 0.5), so a negative exact half rounds toward
        // zero: -0.005 °C is -0.5 centi and lands on 0, not -1. Pinned because
        // temperature is the first signed unit in the file.
        assert.equal(celsiusToMatter(-18.4), -1840);
        assert.equal(celsiusToMatter(-0.005), 0);
        // Half-UP (toward +∞), NOT half-away-from-zero, which the converter's
        // own comment used to claim on the grounds that nothing here was signed.
        // -5.005 °C is -500.5 centi: half-up gives -500, away-from-zero -501.
        assert.equal(celsiusToMatter(-5.005), -500);
    });

    it("clamps to the cluster's absolute-zero floor rather than emitting an illegal int16", () => {
        assert.equal(celsiusToMatter(-500), TEMPERATURE_CENTI_MIN);
        assert.equal(celsiusToMatter(100_000), 32767);
    });
});

describe("humidity conversion (§4.2, E4)", () => {
    it("uses the 0.01 % scale", () => {
        assert.equal(humidityToMatter(48), 4800);
        assert.equal(matterToHumidity(4800), 48);
    });

    it("round-trips every whole percent exactly", () => {
        for (let percent = 0; percent <= 100; percent++) {
            assert.equal(matterToHumidity(humidityToMatter(percent)), percent, `${percent} %`);
        }
    });

    it("clamps outside 0-100 %, which the cluster's 0-10000 domain requires", () => {
        assert.equal(humidityToMatter(-5), 0);
        assert.equal(humidityToMatter(140), HUMIDITY_CENTI_MAX);
    });
});

describe("illuminance conversion (§4.2, E4)", () => {
    it("implements the cluster's documented log scale", () => {
        // "MeasuredValue = 10,000 x log10(illuminance) + 1"
        assert.equal(luxToMatter(1), 1);
        assert.equal(luxToMatter(10), 10_001);
        assert.equal(luxToMatter(100), 20_001);
        assert.equal(luxToMatter(1000), 30_001);
    });

    it("round-trips a decade of readings to within the scale's own resolution", () => {
        // One Matter step is a factor of 10^(1/10000), i.e. ~0.023% — so the
        // property is relative, not absolute. A linear scale would fail this at
        // the bottom of the range and an inverted one everywhere.
        for (const lux of [1, 2, 5, 12.5, 50, 320, 1000, 12_000, 100_000]) {
            const back = matterToLux(luxToMatter(lux));
            assert.ok(Math.abs(back - lux) / lux < 0.001, `${lux} lx came back as ${back}`);
        }
    });

    it("is monotonic across the whole measurable band", () => {
        let previous = -1;
        for (let lux = 1; lux <= 100_000; lux *= 1.05) {
            const measured = luxToMatter(lux);
            assert.ok(measured >= previous, `${lux} lx went backwards`);
            assert.ok(measured >= ILLUMINANCE_MIN && measured <= ILLUMINANCE_MAX, `${lux} lx out of band`);
            previous = measured;
        }
    });

    it("reports sub-lux darkness as the cluster's 0 sentinel, not as 1 lx", () => {
        // log10(0) is -Infinity and the band starts at 1 lx, so 0 is the only
        // honest answer the cluster offers — "too low to be measured".
        assert.equal(luxToMatter(0), 0);
        assert.equal(luxToMatter(0.4), 0);
        assert.equal(luxToMatter(-3), 0);
        assert.equal(matterToLux(0), 0);
    });

    it("clamps a reading past the top of the band", () => {
        assert.equal(luxToMatter(1e9), ILLUMINANCE_MAX);
    });
});

describe("pressure and flow conversion (§4.2, E4)", () => {
    it("uses the 0.1 kPa scale for pressure, NOT kPa", () => {
        // "MeasuredValue = 10 x Pressure [kPa]" — the key is `pressureKPa` but
        // the wire unit is a tenth of that, which is the trap this pins.
        assert.equal(kilopascalsToMatter(101.3), 1013);
        assert.equal(matterToKilopascals(1013), 101.3);
    });

    it("uses the 0.1 m³/h scale for flow", () => {
        assert.equal(cubicMetresPerHourToMatter(1.25), 13);
        assert.equal(cubicMetresPerHourToMatter(1.2), 12);
        assert.equal(matterToCubicMetresPerHour(12), 1.2);
    });

    it("round-trips both at their own resolution", () => {
        for (let tenths = 0; tenths <= 1200; tenths++) {
            const value = tenths / 10;
            assert.equal(matterToKilopascals(kilopascalsToMatter(value)), value, `${value} kPa`);
            assert.equal(matterToCubicMetresPerHour(cubicMetresPerHourToMatter(value)), value, `${value} m³/h`);
        }
    });

    it("keeps flow inside uint16 — a negative flow is not representable", () => {
        assert.equal(cubicMetresPerHourToMatter(-5), 0);
        assert.equal(cubicMetresPerHourToMatter(1e9), 65535);
    });

    it("allows a negative pressure, which is a legal int16 reading", () => {
        assert.equal(kilopascalsToMatter(-2.5), -25);
    });
});

describe("window-covering position (§4.2, E4)", () => {
    it("INVERTS: §4.2 100 is open, Matter 0 is open", () => {
        // The single most dangerous conversion in E4 — both sides are "percent"
        // and they mean opposite things.
        assert.equal(positionToPercent100ths(100), PERCENT100THS_OPEN);
        assert.equal(positionToPercent100ths(0), PERCENT100THS_CLOSED);
        assert.equal(percent100thsToPosition(PERCENT100THS_OPEN), 100);
        assert.equal(percent100thsToPosition(PERCENT100THS_CLOSED), 0);
    });

    it("round-trips every one of the 101 positions exactly", () => {
        for (const position of ALL_PERCENTS) {
            assert.equal(percent100thsToPosition(positionToPercent100ths(position)), position, `position ${position}`);
        }
    });

    it("puts a partial position on the right side of half-open", () => {
        // 70% open is 30% closed. Getting the inversion backwards passes the
        // round-trip test above and fails this one.
        assert.equal(positionToPercent100ths(70), 3000);
        assert.equal(percent100thsToPosition(3000), 70);
        assert.equal(percent100thsToPosition(7500), 25);
    });

    it("clamps rather than emitting a percent100ths outside the cluster's range", () => {
        assert.equal(positionToPercent100ths(-10), PERCENT100THS_CLOSED);
        assert.equal(positionToPercent100ths(180), PERCENT100THS_OPEN);
        assert.equal(percent100thsToPosition(-1), 100);
        assert.equal(percent100thsToPosition(99_999), 0);
    });

    it("rounds like every sibling converter rather than emitting a fraction", () => {
        // `…LiftPercent100ths` is a uint16 and matter.js validates it, so a
        // fractional position — from a newer plugin, or one that stopped
        // rounding — used to fail the whole write instead of losing a
        // hundredth of a percent.
        assert.equal(positionToPercent100ths(70.004), 3000);
        assert.equal(positionToPercent100ths(69.996), 3000);
        assert.ok(Number.isInteger(positionToPercent100ths(33.333)), "emitted a fractional percent100ths");
    });
});

describe("thermostat setpoint limits (§4.2, PR #125 C2)", () => {
    it("advertises a band wide enough for the values a house actually uses", () => {
        // matter.js's own defaults are 7-30 °C heating and 16-32 °C cooling —
        // an American wall thermostat's range, imposed on a bridge that is
        // fronting somebody else's hardware. 5 °C frost protection and the 0 an
        // Indigo heat-only thermostat reports for `coolSetpoint` both sit
        // outside it, and both were rejected with CONSTRAINT_ERROR on every
        // push, forever.
        assert.ok(SETPOINT_CENTI_MIN <= 0, "0 °C must be inside the advertised band");
        assert.ok(SETPOINT_CENTI_MAX >= 3000, "the band must cover ordinary heating setpoints");
        assert.equal(clampSetpoint(5), 500);
        assert.equal(clampSetpoint(0), 0);
        assert.equal(clampSetpoint(21.5), 2150);
    });

    it("clamps outside the band it advertises, like clampMireds", () => {
        // A °F thermostat pushing 68 lands on the top of the band with a debug
        // line naming it, rather than failing the write or being believed.
        assert.equal(clampSetpoint(-40), SETPOINT_CENTI_MIN);
        assert.equal(clampSetpoint(68), SETPOINT_CENTI_MAX);
    });
});

describe("thermostat systemMode mapping (§4.2, E4)", () => {
    it("round-trips the whole four-value domain", () => {
        for (const mode of ["off", "heat", "cool", "auto"]) {
            const matter = systemModeToMatter(mode);
            assert.equal(typeof matter, "number", `${mode} has no Matter value`);
            assert.equal(matterToSystemMode(matter), mode);
        }
    });

    it("pins the enum integers, which are not contiguous", () => {
        // SystemModeEnum has no 2: Off=0, Auto=1, Cool=3, Heat=4.
        assert.equal(systemModeToMatter("off"), 0);
        assert.equal(systemModeToMatter("auto"), 1);
        assert.equal(systemModeToMatter("cool"), 3);
        assert.equal(systemModeToMatter("heat"), 4);
    });

    it("refuses a mode outside §4.2's domain in both directions", () => {
        assert.equal(systemModeToMatter("emergencyHeat"), undefined);
        assert.equal(systemModeToMatter(4), undefined);
        assert.equal(systemModeToMatter(undefined), undefined);
        // EmergencyHeat(5), Precooling(6), FanOnly(7), Dry(8), Sleep(9) are all
        // legal Matter but have no §4.2 spelling — reporting nothing is the
        // honest answer, not the nearest guess.
        for (const value of [2, 5, 6, 7, 8, 9]) {
            assert.equal(matterToSystemMode(value), undefined, `SystemMode ${value}`);
        }
    });
});

describe("echo guard (§6.4)", () => {
    it("treats a matter.js LocalActorContext as our own write", () => {
        assert.equal(isEcosystemChange({ offline: true }), false);
    });

    it("treats anything else as ecosystem-originated", () => {
        // RemoteActorContext declares `offline?: false`, so both the explicit
        // false and the absent property are network actions.
        assert.equal(isEcosystemChange({ offline: false }), true);
        assert.equal(isEcosystemChange({}), true);
        assert.equal(isEcosystemChange(undefined), true);
    });
});

describe("UniqueID ↔ indigoDeviceId (§6.3, issue #141)", () => {
    it("round-trips every device id it produced", () => {
        for (const indigoDeviceId of [0, 1, 42, 123456789, 2147483647]) {
            assert.equal(indigoDeviceIdFrom(uniqueIdFor(indigoDeviceId)), indigoDeviceId);
        }
    });

    it("refuses anything it did not produce, rather than inventing a device id", () => {
        // ⊗ `Number(uniqueId.slice(7))` would answer 1000 for "indigo-1e3" and
        // 0 for "indigo- " — and an invented device id is a NEW accessory in
        // every paired ecosystem, restored from the endpoint map at every start.
        for (const uniqueId of [
            "indigo-",
            "indigo-1e3",
            "indigo-2.5",
            "indigo- 1",
            "indigo-1 ",
            "indigo-0x10",
            "indigo-NaN",
            "matter-1",
            "1",
            "",
        ]) {
            assert.equal(indigoDeviceIdFrom(uniqueId), undefined, uniqueId);
        }
    });

    it("refuses an id too large to be an exact integer", () => {
        // JSON gives us whatever was written; a value past 2^53 comes back as a
        // different number than it went in as, which is the same invention by a
        // quieter route.
        assert.equal(indigoDeviceIdFrom("indigo-9007199254740993"), undefined);
    });
});
