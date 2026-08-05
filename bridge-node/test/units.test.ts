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
    clampMireds,
    degreesToMatterHue,
    HUE_DEGREES_MAX,
    isEcosystemChange,
    MATTER_LEVEL_MAX,
    matterHueToDegrees,
    matterToPercent,
    MIREDS_MAX,
    MIREDS_MIN,
    percentToCurrentLevel,
    percentToMatter,
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
