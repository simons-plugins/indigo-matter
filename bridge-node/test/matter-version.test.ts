/**
 * A canary, not a behaviour test: several suites drive hand-built stand-ins for matter.js internals
 * (`fakeSession`/`hygieneSession` in session-hygiene.test.ts against `SessionManager`, the timer and
 * lock shapes in registry.test.ts). They pass or fail against whatever matter.js is installed, so a
 * silent upgrade can leave them green and wrong.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { matterJsVersion } from "../src/node.js";

describe("installed matter.js", () => {
    it("is the version the test doubles were audited against", () => {
        assert.equal(
            matterJsVersion,
            "0.18.0",
            `matter.js is now ${matterJsVersion}: re-audit fakeSession/hygieneSession (session-hygiene.test.ts) ` +
                "against SessionManager, and the DeviceCommissioner/OnOffServer shapes the registry and restore " +
                "tests rely on, THEN update this version.",
        );
    });
});
