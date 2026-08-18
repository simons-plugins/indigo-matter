/**
 * §3.1-§3.5 argument parsing and the reconcile planner — the refusals that are
 * decided before any Matter object is touched.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { type EndpointSpec, ErrorCode, ProtocolError, Role, type RoleValue } from "../src/protocol.js";
import {
    type LiveComposition,
    parseDeviceId,
    parseEndpointSpec,
    parseEndpointSpecs,
    parseReplaceAll,
    planReconcile,
} from "../src/reconcile.js";

function spec(
    indigoDeviceId: number, role: RoleValue = Role.onOffLight, label = "L", battery = false,
    publishedAs?: string,
): EndpointSpec {
    // Today's default derivation, spelled out locally rather than imported
    // from `endpoints.ts` — this module stays matter.js-free (see its own
    // header doc), and `publishedIdFor(id)` is `indigo-${id}` for every id
    // this suite uses. A supersede test overrides it with a generation suffix.
    return {
        indigoDeviceId, role, label, reachable: true, states: {}, options: {}, battery,
        publishedAs: publishedAs ?? `indigo-${indigoDeviceId}`,
    };
}

/**
 * Build a `live` map keyed on `publishedAs`, defaulting each entry to today's
 * derivation (`indigo-<id>`) the same way {@link spec} does. A test that cares
 * about a re-adopted/superseded identity passes one explicitly as the 5th
 * tuple element.
 */
function live(
    ...entries: [number, RoleValue, boolean?, string?][]
): Map<string, LiveComposition> {
    return new Map(
        entries.map(([indigoDeviceId, role, battery = false, publishedAs = `indigo-${indigoDeviceId}`]) => [
            publishedAs,
            { indigoDeviceId, role, battery },
        ]),
    );
}

function refusal(run: () => unknown): ProtocolError {
    try {
        run();
    } catch (error) {
        assert.ok(error instanceof ProtocolError, `expected a ProtocolError, got ${String(error)}`);
        return error;
    }
    throw new Error("expected a refusal");
}

describe("parseEndpointSpec (§4.1)", () => {
    it("accepts the golden shape and defaults the optional fields", () => {
        assert.deepEqual(parseEndpointSpec({ indigoDeviceId: 7, role: "onOffLight", label: "Lamp" }), {
            indigoDeviceId: 7,
            role: "onOffLight",
            label: "Lamp",
            // An absent `reachable` means "nothing said", not "unavailable" —
            // defaulting to false would grey out every accessory on a client
            // that simply omits the field.
            reachable: true,
            states: {},
            options: {},
            // The opposite default direction from `reachable` (issue #220):
            // absent means "no evidence this device has a battery", not
            // "nothing said" — inventing one would publish an unevidenced
            // PowerSource cluster on every accessory that omits the field.
            battery: false,
            // Issues #219/#240 — absent means "today's derivation", never a
            // bare omission: every `EndpointSpec` in memory has a real
            // published identity.
            publishedAs: "indigo-7",
        });
    });

    it("rejects a non-boolean battery as malformed_args", () => {
        const error = refusal(() =>
            parseEndpointSpec({ indigoDeviceId: 1, role: "onOffLight", label: "x", battery: "yes" }));
        assert.equal(error.code, ErrorCode.malformedArgs);
    });

    it("rejects a role outside §4.2 with unknown_role, not malformed_args", () => {
        const error = refusal(() => parseEndpointSpec({ indigoDeviceId: 1, role: "airPurifier", label: "x" }));
        assert.equal(error.code, ErrorCode.unknownRole);
        assert.equal(error.message, "role airPurifier is not in the v1 role enum (§4.2)");
    });

    it("does not accept an Object.prototype key as a role", () => {
        // `role in Role` would say yes to "constructor"; membership has to be an
        // own-property check or the enum has a hole in it.
        assert.equal(refusal(() => parseEndpointSpec({ indigoDeviceId: 1, role: "constructor", label: "x" })).code,
            ErrorCode.unknownRole);
    });

    for (const [name, value] of [
        ["a non-object", 7],
        ["a missing id", { role: "onOffLight", label: "x" }],
        ["a fractional id", { indigoDeviceId: 1.5, role: "onOffLight", label: "x" }],
        ["a non-string role", { indigoDeviceId: 1, role: 7, label: "x" }],
        ["a missing label", { indigoDeviceId: 1, role: "onOffLight" }],
        ["a non-boolean reachable", { indigoDeviceId: 1, role: "onOffLight", label: "x", reachable: 1 }],
        ["array states", { indigoDeviceId: 1, role: "onOffLight", label: "x", states: [] }],
    ] as [string, unknown][]) {
        it(`rejects ${name} as malformed_args`, () => {
            assert.equal(refusal(() => parseEndpointSpec(value)).code, ErrorCode.malformedArgs);
        });
    }
});

describe("parseEndpointSpecs / parseReplaceAll (§3.1)", () => {
    it("treats an absent endpoints array as an empty desired set", () => {
        assert.deepEqual(parseEndpointSpecs(undefined), []);
    });

    it("rejects a duplicated indigoDeviceId", () => {
        const error = refusal(() => parseEndpointSpecs([{ indigoDeviceId: 1, role: "onOffLight", label: "a" },
            { indigoDeviceId: 1, role: "onOffLight", label: "b" }]));
        assert.equal(error.code, ErrorCode.malformedArgs);
        assert.match(error.message, /twice/);
    });

    it("only honours the exact replace_all literal", () => {
        assert.equal(parseReplaceAll(undefined), false);
        assert.equal(parseReplaceAll("replace_all"), true);
        assert.equal(parseReplaceAll("REPLACE_ALL"), false);
        assert.equal(parseReplaceAll("replace-all"), false);
        assert.equal(refusal(() => parseReplaceAll(true)).code, ErrorCode.malformedArgs);
    });

    it("rejects a non-array endpoints", () => {
        assert.equal(refusal(() => parseEndpointSpecs("nope")).code, ErrorCode.malformedArgs);
    });

    it("rejects the same publishedAs twice, distinctly from a duplicated indigoDeviceId", () => {
        const error = refusal(() => parseEndpointSpecs([
            { indigoDeviceId: 1, role: "onOffLight", label: "a", publishedAs: "indigo-99" },
            { indigoDeviceId: 2, role: "onOffLight", label: "b", publishedAs: "indigo-99" },
        ]));
        assert.equal(error.code, ErrorCode.malformedArgs);
        assert.match(error.message, /publishedAs indigo-99 twice/);
    });
});

describe("publishedAs (§4.1, issues #219/#240)", () => {
    it("defaults publishedAs to indigo-<deviceId> when the wire omits it", () => {
        assert.equal(
            parseEndpointSpec({ indigoDeviceId: 42, role: "onOffLight", label: "x" }).publishedAs,
            "indigo-42",
        );
    });

    it("accepts a generation-suffixed publishedAs and round-trips it", () => {
        const parsed = parseEndpointSpec({
            indigoDeviceId: 42, role: "onOffLight", label: "x", publishedAs: "indigo-42~2",
        });
        assert.equal(parsed.publishedAs, "indigo-42~2");
    });

    for (const bad of ["indigo-42~1", "indigo-42~0", "indigo-1e3", "matter-42", "indigo-", "indigo-42."]) {
        it(`rejects "${bad}" as malformed_args`, () => {
            const error = refusal(() =>
                parseEndpointSpec({ indigoDeviceId: 42, role: "onOffLight", label: "x", publishedAs: bad }));
            assert.equal(error.code, ErrorCode.malformedArgs);
        });
    }

    it("rejects a non-string publishedAs as malformed_args", () => {
        const error = refusal(() =>
            parseEndpointSpec({ indigoDeviceId: 42, role: "onOffLight", label: "x", publishedAs: 42 }));
        assert.equal(error.code, ErrorCode.malformedArgs);
    });

    it("rejects a publishedAs longer than the 32-character UniqueID cap", () => {
        const tooLong = `indigo-${"1".repeat(26)}`; // 33 characters
        const error = refusal(() =>
            parseEndpointSpec({ indigoDeviceId: 42, role: "onOffLight", label: "x", publishedAs: tooLong }));
        assert.equal(error.code, ErrorCode.malformedArgs);
    });
});

describe("parseDeviceId", () => {
    it("takes an integer and nothing else", () => {
        assert.equal(parseDeviceId(123456789), 123456789);
        for (const bad of ["1", 1.5, null, undefined, {}]) {
            assert.equal(refusal(() => parseDeviceId(bad)).code, ErrorCode.malformedArgs);
        }
    });
});

describe("planReconcile (§3.1)", () => {
    it("creates everything against an empty live set", () => {
        const plan = planReconcile(live(), [spec(1), spec(2)], false);
        assert.deepEqual(plan.create.map(s => s.indigoDeviceId), [1, 2]);
        assert.deepEqual([plan.update, plan.recreate, plan.remove], [[], [], []]);
    });

    it("splits an incremental change into create / update / remove", () => {
        const plan = planReconcile(live([1, Role.onOffLight], [2, Role.onOffLight]), [spec(1), spec(3)], false);
        assert.deepEqual(plan.create.map(s => s.indigoDeviceId), [3]);
        assert.deepEqual(plan.update.map(s => s.indigoDeviceId), [1]);
        assert.deepEqual(plan.remove, ["indigo-2"]);
    });

    it("buckets a role change as a recreate rather than an update", () => {
        const plan = planReconcile(live([1, Role.onOffLight]), [spec(1, Role.dimmableLight)], false);
        assert.deepEqual(plan.recreate.map(s => s.indigoDeviceId), [1]);
        assert.deepEqual(plan.update, []);
    });

    it("does not count a recreate as a removal for the guard", () => {
        // The device stays exported; refusing this would make a single-export
        // bridge unable to correct a role at all.
        const plan = planReconcile(live([1, Role.onOffLight]), [spec(1, Role.dimmableLight)], false);
        assert.deepEqual(plan.remove, []);
    });

    it("refuses to empty a non-empty live set without the intent", () => {
        const error = refusal(() => planReconcile(live([1, Role.onOffLight], [2, Role.onOffLight]), [], false));
        assert.equal(error.code, ErrorCode.massRemovalRefused);
        assert.equal(error.message, "attach would remove all 2 live endpoints without intent: replace_all");
    });

    it("refuses a wholly disjoint desired set too, not just an empty one", () => {
        // §3.1 is about the effect: swapping every device out removes every
        // accessory from every paired ecosystem just as surely as `[]` does.
        assert.equal(
            refusal(() => planReconcile(live([1, Role.onOffLight]), [spec(9)], false)).code,
            ErrorCode.massRemovalRefused,
        );
    });

    it("allows the emptying once intent: replace_all is present", () => {
        const plan = planReconcile(live([1, Role.onOffLight], [2, Role.onOffLight]), [], true);
        assert.deepEqual(plan.remove, ["indigo-1", "indigo-2"]);
    });

    it("does not fire the guard when nothing was live", () => {
        assert.deepEqual(planReconcile(live(), [], false), {
            create: [], update: [], recreate: [], rekey: [], remove: [],
        });
    });

    it("does not fire when one endpoint survives", () => {
        const plan = planReconcile(live([1, Role.onOffLight], [2, Role.onOffLight]), [spec(1)], false);
        assert.deepEqual(plan.remove, ["indigo-2"]);
    });

    describe("published identity (issues #219/#240)", () => {
        it("plans a superseded identity as a removal and a create, not a recreate", () => {
            // A role change that DID bump the generation (an up-to-date
            // plugin): the old identity is simply absent from `desired`, the
            // new one is simply absent from `live` — a plain removal and a
            // plain create, never `recreate`, because matter.js gives the new
            // `Endpoint.id` a fresh number rather than reusing the retired one
            // (PR5 design F1).
            const plan = planReconcile(
                live([1, Role.onOffLight]),
                [spec(1, Role.dimmableLight, "L", false, "indigo-1~2")],
                false,
            );
            assert.deepEqual(plan.remove, ["indigo-1"]);
            assert.deepEqual(plan.create.map(s => s.publishedAs), ["indigo-1~2"]);
            assert.deepEqual(plan.recreate, []);
        });

        it("keeps an unchanged identity on the update path even when the device id repeats", () => {
            // The update decision is keyed on `publishedAs` alone: a live map
            // that (pathologically, e.g. PR5 design E7's half-finished supersede) holds
            // TWO keys for the same `indigoDeviceId` must not throw the
            // unrelated, unchanged key's routing off course.
            const twoKeysOneDevice = new Map<string, LiveComposition>([
                ["indigo-1", { indigoDeviceId: 1, role: Role.onOffLight, battery: false }],
                ["indigo-1~2", { indigoDeviceId: 1, role: Role.dimmableLight, battery: false }],
            ]);
            const plan = planReconcile(twoKeysOneDevice, [spec(1, Role.onOffLight)], false);
            assert.deepEqual(plan.update.map(s => s.publishedAs), ["indigo-1"]);
            assert.deepEqual(plan.remove, ["indigo-1~2"]);
        });

        it("a supersede does not trip the mass-removal guard on a single-export bridge", () => {
            // Without the `supersededCount` fix this throws
            // `mass_removal_refused`: the only live identity is being removed,
            // and the guard would not otherwise know the create replacing it
            // is the SAME device continuing to be exported.
            const plan = planReconcile(
                live([1, Role.onOffLight]),
                [spec(1, Role.dimmableLight, "L", false, "indigo-1~2")],
                false,
            );
            assert.deepEqual(plan.remove, ["indigo-1"]);
            assert.deepEqual(plan.create.map(s => s.indigoDeviceId), [1]);
        });

        it("still recreates in place when the role changed at an unchanged identity (pre-#240 skew)", () => {
            // PR5 design §1.3's version-skew fallback: a plugin old enough to never bump
            // `publishedAs` still gets the pre-#240 in-place recreate rather
            // than a refusal.
            const plan = planReconcile(live([1, Role.onOffLight]), [spec(1, Role.dimmableLight)], false);
            assert.deepEqual(plan.recreate.map(s => s.publishedAs), ["indigo-1"]);
            assert.deepEqual(plan.remove, []);
            assert.deepEqual(plan.create, []);
        });

        it("still recreates in place on a battery gain, identity unmoved", () => {
            // Owner decision 4 (§9): only a role change supersedes, and only
            // when the plugin says so. A battery gain never moves the
            // identity, superseded or not.
            const plan = planReconcile(
                live([1, Role.onOffLight, false]), [spec(1, Role.onOffLight, "L", true)], false,
            );
            assert.deepEqual(plan.recreate.map(s => s.publishedAs), ["indigo-1"]);
            assert.deepEqual(plan.remove, []);
            assert.deepEqual(plan.create, []);
        });
    });

    describe("battery composition (issue #220)", () => {
        it("recreates on a battery GAIN, same role", () => {
            const plan = planReconcile(
                live([1, Role.onOffLight, false]), [spec(1, Role.onOffLight, "L", true)], false,
            );
            assert.deepEqual(plan.recreate.map(s => s.indigoDeviceId), [1]);
            assert.deepEqual(plan.update, []);
        });

        it("updates (not recreate) on a battery LOSS — the cluster set is monotonic", () => {
            const plan = planReconcile(
                live([1, Role.onOffLight, true]), [spec(1, Role.onOffLight, "L", false)], false,
            );
            assert.deepEqual(plan.update.map(s => s.indigoDeviceId), [1]);
            assert.deepEqual(plan.recreate, []);
        });

        it("updates when battery is unchanged in either direction", () => {
            assert.deepEqual(
                planReconcile(live([1, Role.onOffLight, true]), [spec(1, Role.onOffLight, "L", true)], false)
                    .update.map(s => s.indigoDeviceId),
                [1],
            );
            assert.deepEqual(
                planReconcile(live([1, Role.onOffLight, false]), [spec(1, Role.onOffLight, "L", false)], false)
                    .update.map(s => s.indigoDeviceId),
                [1],
            );
        });

        it("a battery-gain recreate does not trip the mass-removal guard", () => {
            // Exactly like a role-change recreate: the device stays exported,
            // it just changes what it publishes.
            const plan = planReconcile(
                live([1, Role.onOffLight, false]), [spec(1, Role.onOffLight, "L", true)], false,
            );
            assert.deepEqual(plan.remove, []);
        });
    });

    describe("driving-device rekey (issue #246/ADR-0011)", () => {
        it("buckets a same-role, no-battery-gain device-id change as a rekey, not an update", () => {
            // Device 1 is exported as indigo-1; device 2 now wants to drive
            // the SAME identity, unchanged role. This is the migrate shape —
            // "device B now drives identity X" — and must not reach `update`,
            // which resolves `#live` by device 2's id and finds nothing there.
            const plan = planReconcile(live([1, Role.onOffLight]), [spec(2, Role.onOffLight, "L", false, "indigo-1")], false);
            assert.deepEqual(plan.rekey, [{ spec: spec(2, Role.onOffLight, "L", false, "indigo-1"), fromDeviceId: 1 }]);
            assert.deepEqual(plan.update, []);
            assert.deepEqual(plan.recreate, []);
            assert.deepEqual(plan.create, []);
        });

        it("keeps an unchanged device id on the update path, not rekey", () => {
            const plan = planReconcile(live([1, Role.onOffLight]), [spec(1, Role.onOffLight)], false);
            assert.deepEqual(plan.update.map(s => s.indigoDeviceId), [1]);
            assert.deepEqual(plan.rekey, []);
        });

        it("falls back to recreate when a device-id change is combined with a role change", () => {
            // Cross-role migrate is refused at the plugin layer (owner ruling
            // 2/E3, #240); this combination only arises from a hand-edited
            // .indiPref, and recreate — wire activity, logged loud — is the
            // honest deterministic fallback rather than a silent rekey that
            // cannot actually retarget a live cluster set.
            const plan = planReconcile(
                live([1, Role.onOffLight]), [spec(2, Role.dimmableLight, "L", false, "indigo-1")], false,
            );
            assert.deepEqual(plan.recreate.map(s => s.indigoDeviceId), [2]);
            assert.deepEqual(plan.rekey, []);
        });

        it("falls back to recreate when a device-id change is combined with a battery gain", () => {
            const plan = planReconcile(
                live([1, Role.onOffLight, false]), [spec(2, Role.onOffLight, "L", true, "indigo-1")], false,
            );
            assert.deepEqual(plan.recreate.map(s => s.indigoDeviceId), [2]);
            assert.deepEqual(plan.rekey, []);
        });

        it("a rekey is not a departure for the mass-removal guard", () => {
            // The only live identity is retargeted to a different device, not
            // removed — refusing this as `mass_removal_refused` would make a
            // single-export bridge unable to migrate at all.
            const plan = planReconcile(live([1, Role.onOffLight]), [spec(2, Role.onOffLight, "L", false, "indigo-1")], false);
            assert.deepEqual(plan.remove, []);
        });
    });
});
