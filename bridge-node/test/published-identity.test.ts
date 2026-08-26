/**
 * The published accessory identity as a pure function (issues #219/#240):
 * `publishedIdFor`, `parsePublishedId`, `supersedes`.
 *
 * **This file is one half of a two-language pair.** Its Python twin is
 * `tests/test_bridge_protocol_frames.py`'s `TestPublishedIdentity`, and the two
 * tables are deliberately the same cases in the same order. There is no
 * cross-language fixture mechanism for a pure function — §7's golden frames
 * only cover wire shapes — so keeping them in step is done by inspection, and
 * each file names the other so a change to one is a change somebody can find
 * from the other. A disagreement between the two derivations is not a test
 * failure in the field: it is a duplicate accessory in every paired ecosystem,
 * or an attach the node refuses outright with `malformed_args`.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { parsePublishedId, PUBLISHED_ID_MAX, publishedIdFor, supersedes } from "../src/protocol.js";

/** Mirrors `TestPublishedIdentity.VALID`, case for case. */
const VALID: [string, { deviceId: number; generation: number }][] = [
    ["indigo-1", { deviceId: 1, generation: 1 }],
    ["indigo-0", { deviceId: 0, generation: 1 }],
    ["indigo--5", { deviceId: -5, generation: 1 }],
    ["indigo-123~2", { deviceId: 123, generation: 2 }],
    ["indigo-123~99", { deviceId: 123, generation: 99 }],
    // `Number.MAX_SAFE_INTEGER` — a JavaScript language constant, which
    // `bridge_protocol.py` mirrors by hand because Python has no equivalent.
    ["indigo-9007199254740991", { deviceId: 9007199254740991, generation: 1 }],
    // Exactly PUBLISHED_ID_MAX (32) characters.
    ["indigo-9007199254740991~99999999", { deviceId: 9007199254740991, generation: 99999999 }],
];

/** Mirrors `TestPublishedIdentity.INVALID`, case for case. */
const INVALID = [
    "indigo-123~1", // generation must be >= 2
    "indigo-123~0",
    "indigo-123.2", // "." is not the generation separator
    "indigo-abc", // non-numeric device id
    "matter-1", // wrong prefix
    "indigo-1~", // no digits after ~
    "indigo-", // no device id at all
    "",
    "indigo-1 ", // trailing whitespace
    // Python's `$` also matches before a trailing newline; JS's does not. The
    // Python twin anchors with `\Z` so both refuse this.
    "indigo-1\n",
    // One past `Number.MAX_SAFE_INTEGER`.
    "indigo-9007199254740992",
    // One character over PUBLISHED_ID_MAX (33), otherwise lawful.
    "indigo-9007199254740991~999999999",
    // Python's `\d` matches every Unicode decimal digit; JS's matches [0-9].
    // The Python twin compiles with `re.ASCII` so both refuse these.
    "indigo-١٢٣", // Arabic-Indic digits
    "indigo-1٢", // ASCII and Arabic-Indic mixed
    "indigo-1~٢", // a Unicode-digit generation
    "indigo-１２３", // fullwidth digits
];

describe("published identity, the two-language parity table (issues #219/#240)", () => {
    for (const [value, expected] of VALID) {
        it(`parses ${JSON.stringify(value)}`, () => {
            assert.deepEqual(parsePublishedId(value), expected);
        });
    }

    for (const value of INVALID) {
        it(`refuses ${JSON.stringify(value)}`, () => {
            assert.equal(parsePublishedId(value), undefined);
        });
    }

    for (const [value, expected] of VALID) {
        it(`publishedIdFor is the inverse for ${JSON.stringify(value)}`, () => {
            assert.equal(publishedIdFor(expected.deviceId, expected.generation), value);
        });
    }

    it("defaults to generation 1", () => {
        assert.equal(publishedIdFor(42), "indigo-42");
    });

    it("caps at the measured UniqueID limit", () => {
        assert.equal(PUBLISHED_ID_MAX, 32);
        assert.equal(VALID.at(-1)![0].length, PUBLISHED_ID_MAX);
    });
});

describe("supersedes() — only a later generation retires an identity (issue #240)", () => {
    // The whole of the #240/#219 distinction lives in this predicate: both
    // node-side writers of `supersededBy` consult it, and `registry.ts`
    // chooses between two very different log lines on it. Every other test
    // that exercises it does so through a bridge; these are the cases it is
    // easiest to get wrong and hardest to see through one.

    it("is true for a generation bump", () => {
        assert.equal(supersedes("indigo-7", "indigo-7~2"), true);
    });

    it("is true for a LATER generation bump, not only the first", () => {
        // The gap #240 leaves open if the test is written as "does the new one
        // have a suffix": a second role change on an already-superseded
        // identity must retire indigo-7~2 as surely as the first retired
        // indigo-7.
        assert.equal(supersedes("indigo-7~2", "indigo-7~3"), true);
    });

    it("is FALSE for the same generation, or a different device's identity", () => {
        // Migrating an accessory onto an already-exported device (issue
        // #246) is also one removal plus one create for one device, and the
        // identity left behind must be an ordinary orphan, not a retired
        // one. Getting this wrong would resurrect an old-role accessory at
        // the next restart.
        assert.equal(supersedes("indigo-7", "indigo-7"), false);
        assert.equal(supersedes("indigo-7", "indigo-99"), false);
        assert.equal(supersedes("indigo-7~2", "indigo-7~2"), false);
    });

    it("is FALSE for a DOWNGRADE", () => {
        // A rollback, or a hand-edited store. `>` not `!==`: treating this as
        // a supersession would mark the LIVE identity retired.
        assert.equal(supersedes("indigo-7~3", "indigo-7~2"), false);
        assert.equal(supersedes("indigo-7~2", "indigo-7"), false);
    });

    it("is FALSE across different devices, whatever the generations", () => {
        assert.equal(supersedes("indigo-7", "indigo-8~2"), false);
        assert.equal(supersedes("indigo-7~2", "indigo-8~9"), false);
    });

    it("is FALSE when either side is not a lawful identity", () => {
        assert.equal(supersedes("indigo-1e3", "indigo-1e3~2"), false);
        assert.equal(supersedes("indigo-7", "made-up"), false);
        assert.equal(supersedes("", ""), false);
    });
});
