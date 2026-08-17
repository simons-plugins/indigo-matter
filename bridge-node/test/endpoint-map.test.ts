/**
 * E5: the persisted endpoint-number map, its drift detector, and the PRD §7
 * refuse-to-start decision.
 *
 * All stdlib — the module deliberately has no matter.js import, so the thing
 * that decides whether a user's accessories keep their identity is testable
 * without a Matter stack, a fabric, or a network.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import {
    ENDPOINT_MAP_FILE,
    ENDPOINT_MAP_VERSION,
    type EndpointRecord,
    EndpointMapStore,
    deleteEndpointMap,
    loadEndpointMap,
    refuseReasonFor,
} from "../src/endpoint-map.js";
import { RefuseReason } from "../src/protocol.js";

const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
const scratch: string[] = [];

after(() => {
    for (const dir of scratch) {
        // Chmod back first: the write-failure test makes a directory read-only.
        try {
            chmodSync(dir, 0o700);
        } catch {
            // Already gone or never restricted.
        }
        rmSync(dir, { recursive: true, force: true });
    }
});

function storage(): string {
    const dir = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-map-"));
    scratch.push(dir);
    return dir;
}

function mapFileIn(dir: string): { version: number; endpoints: Record<string, EndpointRecord> } {
    return JSON.parse(readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8"));
}

describe("loadEndpointMap", () => {
    it("reports an absent file as absent, not as a fault", () => {
        // The distinction is the whole refuse-to-start rule: a first run has no
        // map and must start, a lost map on a paired bridge must not.
        const loaded = loadEndpointMap(storage());
        assert.equal(loaded.present, false);
        assert.equal(loaded.problem, undefined);
        assert.equal(loaded.endpoints.size, 0);
    });

    it("reads a version 1 file into a UniqueID → number map", () => {
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({ version: 1, endpoints: { "indigo-123456789": 2, "indigo-123456790": 3 } }),
        );
        const loaded = loadEndpointMap(dir);
        assert.equal(loaded.present, true);
        assert.equal(loaded.problem, undefined);
        assert.deepEqual([...loaded.endpoints.entries()], [
            ["indigo-123456789", { number: 2 }],
            ["indigo-123456790", { number: 3 }],
        ]);
    });

    for (const [name, body] of [
        ["unparseable JSON", "{ not json"],
        ["a wrong schema version", JSON.stringify({ version: 99, endpoints: {} })],
        ["a non-object endpoints", JSON.stringify({ version: 1, endpoints: [] })],
        ["a non-numeric number", JSON.stringify({ version: 1, endpoints: { "indigo-1": "2" } })],
        ["a fractional number", JSON.stringify({ version: 1, endpoints: { "indigo-1": 2.5 } })],
    ] as const) {
        it(`reports ${name} as present-but-unusable`, () => {
            const dir = storage();
            writeFileSync(join(dir, ENDPOINT_MAP_FILE), body);
            const loaded = loadEndpointMap(dir);
            assert.equal(loaded.present, true, "the file exists, whatever is in it");
            assert.ok(loaded.problem !== undefined, "and it must say why it cannot be used");
            assert.equal(loaded.endpoints.size, 0, "a broken map must never be half-adopted");
        });
    }

    it("reports a read error that is NOT a missing file as present-but-unusable", () => {
        // ⊗ Returning `present: false` for every read error would read a
        // genuine fault — a permissions problem, an unreadable disk, the path
        // taken over by a directory — as a first run, and a first run never
        // refuses. The one signal that stops a commissioned bridge duplicating
        // every accessory would be off precisely when the disk is misbehaving.
        const dir = storage();
        // A directory where the file should be: `readFileSync` fails with
        // EISDIR, which is emphatically not ENOENT.
        mkdirSync(join(dir, ENDPOINT_MAP_FILE));

        const loaded = loadEndpointMap(dir);

        assert.equal(loaded.present, true, "something IS there — it just cannot be read");
        assert.ok(loaded.problem !== undefined);
        assert.equal(
            refuseReasonFor({ commissioned: true, mapProblem: loaded.problem }),
            RefuseReason.mapUnreadable,
            "and it must be the kind of answer a commissioned bridge refuses on",
        );
    });
});

describe("the restoration half of an entry (issue #141)", () => {
    it("reads a version 2 file's role and label back", () => {
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: 2,
                endpoints: { "indigo-1": { number: 2, role: "onOffLight", label: "Kitchen Lamp" } },
            }),
        );
        const store = new EndpointMapStore(dir);
        store.load();

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Kitchen Lamp" },
        ]);
    });

    it("keeps the number when only the role or label is malformed", () => {
        // ⊗ Rejecting the whole file over a bad label would turn a cosmetic
        // corruption into a refuse-to-start on a paired bridge, and would
        // discard the numbers — the one part that cannot be re-derived. The
        // entry loses only what was wrong with it and the next attach fills it
        // back in.
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: 2,
                endpoints: { "indigo-1": { number: 2, role: 7, label: "" } },
            }),
        );
        const store = new EndpointMapStore(dir);
        const loaded = store.load();

        assert.equal(loaded.problem, undefined);
        assert.equal(store.numberFor("indigo-1"), 2);
        assert.deepEqual(store.restorable(), [], "but it cannot be rebuilt from");
    });

    it("leaves a v1 entry restorable-in-waiting rather than treating it as a fault", () => {
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({ version: 1, endpoints: { "indigo-1": 2 } }),
        );
        const store = new EndpointMapStore(dir);

        assert.equal(store.load().problem, undefined);
        assert.equal(store.numberFor("indigo-1"), 2, "the number is the irreplaceable part");
        assert.deepEqual(store.restorable(), []);

        // One check with a live set is all it takes to make it restorable.
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" }]);

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
        ]);
        assert.deepEqual(mapFileIn(dir), {
            version: ENDPOINT_MAP_VERSION,
            endpoints: { "indigo-1": { number: 2, role: "onOffLight", label: "Lamp" } },
        });
    });

    it("follows a rename, and persists it", () => {
        // ⊗ A cached label that lags behind the cluster restores yesterday's
        // name after a restart, which in an ecosystem reads as the accessory
        // having been renamed by the bridge.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Old" }]);

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "New" }]);

        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "onOffLight", label: "New" },
        });
    });

    it("needs BOTH halves — a good role with no label rebuilds nothing", () => {
        // ⊗ T4. The existing malformed-entry test used a bad role AND a bad
        // label, so dropping the label half of the predicate passed it. An entry
        // with a role and no label would then be handed to `createEndpoint` as
        // `label: undefined`, which is either a crash or an unnamed accessory —
        // and an unnamed accessory in the Home app is the metadata loss #141 is
        // about.
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: 2,
                endpoints: {
                    "indigo-1": { number: 2, role: "onOffLight", label: "" },
                    "indigo-2": { number: 3, role: "", label: "Named" },
                },
            }),
        );
        const store = new EndpointMapStore(dir);
        store.load();

        assert.equal(store.numberFor("indigo-1"), 2, "the number is kept either way");
        assert.equal(store.numberFor("indigo-2"), 3);
        assert.deepEqual(store.restorable(), [], "but neither half alone can rebuild an endpoint");
    });

    it("refreshes the role and label of an entry whose NUMBER has drifted", () => {
        // ⊗ T5. The comment above this branch argues for it explicitly and
        // nothing tested it: making the drift case `continue` passed everything.
        // The number and the name are independent facts. An accessory that was
        // renamed while its number also moved must still come back under its
        // current name — and the number must still be left alone and reported,
        // because repairing it is what would bless the fault as the truth.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Old" }]);

        const drift = store.check([
            { uniqueId: "indigo-1", endpointNumber: 9, role: "dimmableLight", label: "New" },
        ]);

        assert.deepEqual(drift, [{ uniqueId: "indigo-1", expected: 2, actual: 9 }], "still reported");
        assert.deepEqual(
            store.restorable(),
            [{ uniqueId: "indigo-1", endpointNumber: 2, role: "dimmableLight", label: "New" }],
            "the baseline NUMBER is never repaired, but the name is not held hostage to it",
        );
        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "dimmableLight", label: "New" },
        });
    });

    it("never lets a caller that knows nothing erase what a caller that did recorded", () => {
        // The seed path has no live set at all, and reading its silence as "no
        // role any more" would strip a good entry of the fields that make it
        // restorable — turning every migration into another empty restart.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" }]);

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
        ]);
    });
});

describe("EndpointMapStore.check — the drift detector (PRD §4.3)", () => {
    it("records endpoints it has never seen, and says nothing about them", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        assert.equal(store.checked, false, "§4.3: nothing has been checked yet");

        const drift = store.check([
            { uniqueId: "indigo-1", endpointNumber: 2 },
            { uniqueId: "indigo-2", endpointNumber: 3 },
        ]);

        assert.deepEqual(drift, []);
        assert.equal(store.checked, true);
        assert.deepEqual(mapFileIn(dir), {
            version: ENDPOINT_MAP_VERSION,
            endpoints: { "indigo-1": { number: 2 }, "indigo-2": { number: 3 } },
        });
    });

    it("reports an endpoint whose number moved, and does NOT adopt the new one", () => {
        // §4.3 is explicit that drift is never auto-repaired. The baseline has
        // to survive the report, or the second pass would find the map already
        // agreeing with the fault and call it clean.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 5 }]);

        assert.deepEqual(drift, [{ uniqueId: "indigo-1", expected: 2, actual: 5 }]);
        assert.equal(store.numberFor("indigo-1"), 2, "the baseline must not move");
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 2 } });
        assert.deepEqual(
            store.check([{ uniqueId: "indigo-1", endpointNumber: 5 }]),
            [{ uniqueId: "indigo-1", expected: 2, actual: 5 }],
            "and it must keep reporting until a human resolves it",
        );
    });

    it("keeps the allocation of a device that is no longer live (§3.3)", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([
            { uniqueId: "indigo-1", endpointNumber: 2 },
            { uniqueId: "indigo-2", endpointNumber: 3 },
        ]);

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        assert.equal(store.numberFor("indigo-2"), 3, "§3.3 retains the allocation across a removal");
    });

    it("survives a restart: a reloaded map still catches the same drift", () => {
        const dir = storage();
        const first = new EndpointMapStore(dir);
        first.load();
        first.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        // The whole point of persisting: a process that has never seen this
        // endpoint before still knows what number it is supposed to have.
        const second = new EndpointMapStore(dir);
        const loaded = second.load();
        assert.equal(loaded.present, true);
        assert.deepEqual(second.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]), [
            { uniqueId: "indigo-1", expected: 2, actual: 9 },
        ]);
    });

    it("does not rewrite the file when nothing was added", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        const logged: string[] = [];
        const quiet = new EndpointMapStore(dir, message => logged.push(message));
        quiet.load();

        quiet.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        // `get_status`/attach run this on every reconnect; a steady state that
        // wrote to disk each time would be a needless write per watchdog cycle.
        assert.deepEqual(logged.filter(line => line.startsWith("Endpoint map recorded")), []);
    });

    it("does NOT call an empty comparison a clean bill of health", () => {
        // ⊗ `#checked` was set unconditionally at the end of `check`, so a
        // comparison against nothing at all — a reconcile that removed the last
        // endpoint, a `remove_endpoint` on a node that never attached — flipped
        // §4.3's `driftChecked` to true. The two readings of `drift: []` are
        // opposites, and this is the one that means "there was nothing to look
        // at"; `seed()` has always had the rule right.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();

        assert.deepEqual(store.check([]), []);
        assert.equal(store.checked, false, "nothing was compared, so nothing was verified");

        // And it only ever raises the flag: a real comparison that has already
        // happened is not un-done by a later empty one, or a bridge whose last
        // export was removed would forget it had ever checked.
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        assert.equal(store.checked, true);
        store.check([]);
        assert.equal(store.checked, true);
    });

    it("stays usable and says so loudly when the map cannot be written", () => {
        const dir = storage();
        const logged: string[] = [];
        const store = new EndpointMapStore(dir, message => logged.push(message));
        store.load();
        chmodSync(dir, 0o500); // read + execute only: no new files

        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        chmodSync(dir, 0o700);
        // Refusing to serve endpoints because a *witness* could not be written
        // would take a working bridge down over a disk problem — but the loss of
        // future drift detection cannot be silent either.
        assert.deepEqual(drift, []);
        assert.ok(
            logged.some(line => line.includes("Could not write the endpoint map")),
            `expected a write-failure warning, got ${JSON.stringify(logged)}`,
        );
    });
});

describe("EndpointMapStore.voidNumbers — reset renumbering adoption (issue #140)", () => {
    it("marks every entry and persists", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([
            { uniqueId: "indigo-1", endpointNumber: 2 },
            { uniqueId: "indigo-2", endpointNumber: 3 },
        ]);

        assert.equal(store.voidNumbers("test"), true);

        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, numberVoid: true },
            "indigo-2": { number: 3, numberVoid: true },
        });
    });

    it("is a no-op — no write — on an empty map", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();

        assert.equal(store.voidNumbers("test"), false);

        assert.equal(loadEndpointMap(dir).present, false, "an empty map must not manufacture a baseline");
    });

    it("check() adopts a renumbered void entry: numbers update, no drift, markers clear, persists", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        store.voidNumbers("factory reset");

        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]);

        assert.deepEqual(drift, [], "a voided renumbering must never be reported as drift");
        assert.equal(store.numberFor("indigo-1"), 9, "the new number is adopted as the baseline");
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 9 } }, "and the marker is gone");
    });

    it("a second check with the same (already-adopted) set is a quiet steady state", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        store.voidNumbers("factory reset");
        store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]);
        const settled = readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8");

        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]);

        assert.deepEqual(drift, []);
        assert.equal(
            readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8"),
            settled,
            "an already-adopted, unchanged set must cost no further write",
        );
    });

    it("clears the marker even when the live number happens to already match", () => {
        // The marker itself is what must stop being true, not just a mismatch —
        // otherwise a coincidentally-unchanged number would stay VOID forever,
        // silently swallowing the very next real drift on that entry.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        store.voidNumbers("factory reset");

        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        assert.deepEqual(drift, []);
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 2 } }, "marker cleared, not left set");
    });

    it("does NOT adopt a NON-void entry alongside void ones — the never-auto-repaired rule survives", () => {
        // The critical regression test: without the `numberVoid` guard in
        // `check`, EVERY entry would adopt its live number regardless of
        // marker, and genuine drift (a real storage loss, unrelated to a
        // reset) would stop being reported for ever.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([
            { uniqueId: "indigo-void", endpointNumber: 2 },
            { uniqueId: "indigo-drifted", endpointNumber: 3 },
        ]);
        store.voidNumbers("factory reset");
        // Un-void the second entry by hand — standing in for an entry that was
        // never touched by a reset at all (voidNumbers marks the whole map, so
        // this is how a genuinely mixed map is produced for the test).
        const file = mapFileIn(dir);
        delete file.endpoints["indigo-drifted"]!.numberVoid;
        writeFileSync(join(dir, ENDPOINT_MAP_FILE), JSON.stringify(file));
        const reloaded = new EndpointMapStore(dir);
        reloaded.load();

        const drift = reloaded.check([
            { uniqueId: "indigo-void", endpointNumber: 9 },
            { uniqueId: "indigo-drifted", endpointNumber: 99 },
        ]);

        assert.deepEqual(drift, [{ uniqueId: "indigo-drifted", expected: 3, actual: 99 }]);
        assert.equal(reloaded.numberFor("indigo-void"), 9, "the void entry still adopted");
        assert.equal(reloaded.numberFor("indigo-drifted"), 3, "the baseline of a real drift must not move");
    });

    it("the marker survives a persist/load round-trip", () => {
        const dir = storage();
        const first = new EndpointMapStore(dir);
        first.load();
        first.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        first.voidNumbers("factory reset");

        const second = new EndpointMapStore(dir);
        second.load();

        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 2, numberVoid: true } });
        // And the reloaded store still adopts rather than reports drift.
        assert.deepEqual(second.check([{ uniqueId: "indigo-1", endpointNumber: 7 }]), []);
        assert.equal(second.numberFor("indigo-1"), 7);
    });

    it("a legacy (pre-#140) file with no markers loads and drifts exactly as before", () => {
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({ version: ENDPOINT_MAP_VERSION, endpoints: { "indigo-1": { number: 2 } } }),
        );
        const store = new EndpointMapStore(dir);
        store.load();

        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]);

        assert.deepEqual(drift, [{ uniqueId: "indigo-1", expected: 2, actual: 9 }], "unvoided — still real drift");
        assert.equal(store.numberFor("indigo-1"), 2, "and the baseline must not move");
    });

    it("heals the reset's duplicate-number fingerprint: two void entries adopt distinct live numbers", () => {
        // Live example (#140): the witness recorded the SAME number on two
        // different entries after a reset. Voiding both and letting each adopt
        // its own live number is what makes the map consistent again.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([
            { uniqueId: "indigo-1", endpointNumber: 5 },
            { uniqueId: "indigo-2", endpointNumber: 5 },
        ]);
        store.voidNumbers("factory reset");

        const drift = store.check([
            { uniqueId: "indigo-1", endpointNumber: 5 },
            { uniqueId: "indigo-2", endpointNumber: 2 },
        ]);

        assert.deepEqual(drift, []);
        assert.equal(store.numberFor("indigo-1"), 5);
        assert.equal(store.numberFor("indigo-2"), 2);
        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 5 },
            "indigo-2": { number: 2 },
        });
    });

    it("stays restorable while voided — role/label are untouched by voiding", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" }]);

        store.voidNumbers("factory reset");

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
        ]);
    });

    it("logs the adoption once when it happens, naming the count", () => {
        const dir = storage();
        const logged: string[] = [];
        const store = new EndpointMapStore(dir, message => logged.push(message));
        store.load();
        store.check([
            { uniqueId: "indigo-1", endpointNumber: 2 },
            { uniqueId: "indigo-2", endpointNumber: 3 },
        ]);
        store.voidNumbers("factory reset");
        logged.length = 0;

        store.check([
            { uniqueId: "indigo-1", endpointNumber: 9 },
            { uniqueId: "indigo-2", endpointNumber: 8 },
        ]);

        assert.ok(
            logged.some(line => line.includes("Adopted 2 endpoint number")),
            `expected an adoption log line, got ${JSON.stringify(logged)}`,
        );
    });

    it("clears `checked` — a voided baseline has not been verified against anything", () => {
        // ⊗ `voidNumbers` used to leave `#checked` untouched, so between a
        // reset and the first post-reset reconcile, `driftChecked: true` /
        // `drift: []` claimed "checked, nothing moved" over numbers the node
        // itself had just declared untrustworthy.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        assert.equal(store.checked, true, "sanity: the first check earned the flag");

        store.voidNumbers("factory reset");

        assert.equal(store.checked, false, "a VOID baseline is unverified, whatever it was before");

        store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]);

        assert.equal(store.checked, true, "the first post-reset check against live entries earns it back");
    });

    it("reports the write failing, not a no-op, when the void cannot reach disk", () => {
        // ⊗ The boolean `voidNumbers` returns was discarded at both call sites
        // in `node.ts`, so a failed write left the VOID markers RAM-only while
        // the log claimed the opposite — this is the store-level half of that:
        // the caller has to be able to tell "wrote fine" from "in memory only".
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        chmodSync(dir, 0o500);
        try {
            assert.equal(store.voidNumbers("factory reset"), false, "the write failed");
            assert.equal(store.warnings.length, 1);
            assert.match(store.warnings[0]!, /Could not write the endpoint map/);
        } finally {
            chmodSync(dir, 0o700);
        }
        assert.equal(store.checked, false, "an unwritten baseline still verified nothing durable");

        // The marker survives in memory despite the failed write, and `#dirty`
        // makes the next successful write retry it — proven by the very next
        // check() adopting rather than reporting drift.
        const drift = store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]);

        assert.deepEqual(drift, [], "the in-memory VOID marker survived the failed write and still adopts");
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 9 } });
    });
});

describe("EndpointMapStore.forget — un-export without losing the number (issue #219)", () => {
    it("marks the entry orphaned, keeping BOTH the restoration half and the identity half", () => {
        // ⊗ The other half of `restorable`. Without it a device the user
        // un-exported stayed restorable for ever and was rebuilt-then-removed on
        // every boot. Deleting role/label (the pre-#219 behaviour) worked too but
        // threw away the only evidence a future re-adopt UI could match a
        // recreated device against — so #219 marks `orphaned` instead of erasing
        // anything. Deleting the ENTRY instead would also be wrong: §3.3 retains
        // the allocation so a re-export comes back as the same accessory rather
        // than a new one in every paired ecosystem.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
            { uniqueId: "indigo-2", endpointNumber: 3, role: "dimmableLight", label: "Other" },
        ]);

        assert.equal(store.forget(["indigo-1"]), 1);

        assert.equal(store.numberFor("indigo-1"), 2, "§3.3 retains the allocation");
        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-2", endpointNumber: 3, role: "dimmableLight", label: "Other" },
        ]);
        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "onOffLight", label: "Lamp", orphaned: true },
            "indigo-2": { number: 3, role: "dimmableLight", label: "Other" },
        });
    });

    it("re-exporting refills it, at the number it kept, and clears the orphan marker", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" }]);
        store.forget(["indigo-1"]);

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" }]);

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
        ]);
        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "onOffLight", label: "Lamp" },
        });
    });

    it("costs no disk write when there was nothing to forget", () => {
        // Unknown ids and already-bare entries (no role/label to protect) are
        // both no-ops: `forget` is driven by a diff, and a diff that found
        // nothing worth orphaning must not rewrite the file (nor make a failed
        // write look like a fresh one).
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        const before = readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8");

        assert.equal(store.forget(["indigo-1", "indigo-nope"]), 0);

        assert.equal(readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8"), before);
    });

    it("is idempotent — forgetting an already-orphaned entry is a no-op", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" }]);
        assert.equal(store.forget(["indigo-1"]), 1);
        const before = readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8");

        assert.equal(store.forget(["indigo-1"]), 0);

        assert.equal(readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8"), before);
    });

    it("old-format (bare {number}) records still load, and forgetting one is a no-op", () => {
        // A v1-migrated or never-refreshed entry has no role/label to protect,
        // and is already excluded from `restorable` on that basis alone — #219's
        // `orphaned` marker adds nothing for it, so `forget` must not write one.
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({ version: 1, endpoints: { "indigo-1": 2 } }),
        );
        const store = new EndpointMapStore(dir);
        const loaded = store.load();

        assert.equal(loaded.problem, undefined);
        assert.equal(store.numberFor("indigo-1"), 2);
        assert.equal(store.forget(["indigo-1"]), 0);
        assert.deepEqual(store.restorable(), []);
    });
});

describe("EndpointMapStore.seed replaces the whole map", () => {
    it("refuses to seed fewer numbers than the baseline already holds", () => {
        // ⊗ `seed` is a full replace, and nothing said so. Its one caller
        // bootstraps against a map that is by definition absent, so it drops
        // nothing — but a later caller with a map in hand would silently discard
        // the endpoint numbers of everything absent from its list, which is the
        // one thing in this file that cannot be re-derived. Fail loudly instead.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([
            { uniqueId: "indigo-1", endpointNumber: 2 },
            { uniqueId: "indigo-2", endpointNumber: 3 },
        ]);

        assert.throws(
            () => store.seed([{ uniqueId: "indigo-1", endpointNumber: 2 }], "a partial list"),
            /replaces the whole map/,
        );
        assert.equal(store.numberFor("indigo-2"), 3, "and nothing was dropped");
    });

    it("still bootstraps an empty baseline, which is what its caller does", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();

        assert.equal(store.seed([], "the migration path"), true);
        assert.equal(store.size, 0);
    });
});

describe("EndpointMapStore.rebuild (§3.11)", () => {
    it("adopts the live numbers, clears the problem, and persists", () => {
        const dir = storage();
        writeFileSync(join(dir, ENDPOINT_MAP_FILE), "{ corrupt");
        const store = new EndpointMapStore(dir);
        store.load();
        assert.ok(store.problem !== undefined);

        store.rebuild([
            { uniqueId: "indigo-1", endpointNumber: 4 },
            { uniqueId: "indigo-2", endpointNumber: 5 },
        ]);

        assert.equal(store.problem, undefined);
        assert.equal(store.checked, true, "§4.3: a persisted map is a checked one");
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 4 }, "indigo-2": { number: 5 } });
        assert.deepEqual(store.check([{ uniqueId: "indigo-1", endpointNumber: 4 }]), []);
    });

    it("forgets a mapping the live set no longer contains", () => {
        // Unlike `check`, which retains allocations, a rebuild is the user
        // saying "whatever is live now IS the truth" — retaining a stale entry
        // would leave the very baseline they asked to discard.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-gone", endpointNumber: 2 }]);

        store.rebuild([{ uniqueId: "indigo-1", endpointNumber: 4 }]);

        assert.equal(store.numberFor("indigo-gone"), undefined);
    });
});

describe("EndpointMapStore.discard (§3.10 preserveEndpointNumbers: false)", () => {
    it("deletes the file and forgets every number", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        store.discard();

        assert.equal(store.present, false);
        assert.equal(store.checked, false, "there is no baseline left to have checked");
        assert.equal(loadEndpointMap(dir).present, false);
    });

    it("is a no-op when there is nothing to discard", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.discard();
        assert.equal(deleteEndpointMap(dir), false);
    });

    it("reports a deletion it could not do, and warns instead of claiming success", () => {
        // ⊗ Three ways this went wrong at once: the `false` was returned but
        // never propagated, the §4.3 warning was the only trace, and
        // `factory_reset` logged "complete" over the top of both. What survives
        // a failed discard is a stale file the NEXT start loads as a baseline
        // for numbers that no longer mean anything.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
        // A directory in its place: `unlinkSync` fails with EPERM/EISDIR, which
        // `deleteEndpointMap` rethrows because it is not "already gone".
        rmSync(join(dir, ENDPOINT_MAP_FILE));
        mkdirSync(join(dir, ENDPOINT_MAP_FILE));

        assert.equal(store.discard(), false, "a discard that did not happen must not answer true");
        assert.equal(store.warnings.length, 1);
        assert.match(store.warnings[0]!, /Could not delete the endpoint map/);
    });

    it("clears the persistence warnings a successful discard has settled", () => {
        // §4.3: warnings are current, not historical. A map that could not be
        // written and is now deleted has no outstanding problem to report, and
        // a stale warning is one a user cannot make go away by doing anything.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        chmodSync(dir, 0o500);
        try {
            store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
            assert.equal(store.warnings.length, 1, "the failed write must warn first");
        } finally {
            chmodSync(dir, 0o700);
        }

        assert.equal(store.discard(), true);
        assert.deepEqual(store.warnings, []);
    });
});

describe("EndpointMapStore.load resets the warnings it is about to re-derive", () => {
    it("drops a stale warning rather than carrying it across a re-read", () => {
        // ⊗ `load()` is "the last read wins" for everything else it touches, and
        // a warning it did not clear outlives the state that produced it — so a
        // `get_status` after a successful reload would still be telling the user
        // to free disk space over a write that has since landed.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        chmodSync(dir, 0o500);
        try {
            store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);
            assert.equal(store.warnings.length, 1);
        } finally {
            chmodSync(dir, 0o700);
        }

        store.load();

        assert.deepEqual(store.warnings, [], "the reload re-derives everything, warnings included");
    });
});

describe("a write that did not land (E5 B1/B2)", () => {
    it("never claims driftChecked over a RAM-only baseline", () => {
        // ⊗ `#checked` used to be set unconditionally. A `driftChecked: true`
        // over a map that never reached disk asserts an all-clear about a
        // baseline the next restart cannot find — so that device's reallocated
        // number gets recorded as the truth and the real renumbering is never
        // reported by anybody, ever.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        chmodSync(dir, 0o500);
        try {
            assert.deepEqual(store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]), []);
            assert.equal(store.checked, false, "the write failed, so nothing durable was checked");
        } finally {
            chmodSync(dir, 0o700);
        }

        // And the retry: the next check re-attempts the write even though it
        // adds nothing, because the map still owes the disk what it holds.
        assert.deepEqual(store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]), []);
        assert.equal(store.checked, true);
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 2 } });
    });

    it("tells the caller a rebuild did not persist, and warns", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        chmodSync(dir, 0o500);
        try {
            assert.equal(store.rebuild([{ uniqueId: "indigo-1", endpointNumber: 2 }]), false);
            // §4.3 `warnings`: the node's log is a stdout nobody is watching in
            // this milestone, so a failed write has to reach get_status.
            assert.equal(store.warnings.length, 1);
            assert.match(store.warnings[0]!, /Could not write the endpoint map/);
        } finally {
            chmodSync(dir, 0o700);
        }
        assert.equal(store.rebuild([{ uniqueId: "indigo-1", endpointNumber: 2 }]), true);
        assert.deepEqual(store.warnings, [], "a warning is current, not historical");
    });
});

describe("rebuild preserves the evidence (E5 C2)", () => {
    /** The `.corrupt-<stamp>` copies sitting in a storage dir, oldest first. */
    function quarantinedMaps(dir: string): string[] {
        return readdirSync(dir)
            .filter(name => name.startsWith(`${ENDPOINT_MAP_FILE}.corrupt-`))
            .sort();
    }

    it("copies an unusable map aside before overwriting it", () => {
        const dir = storage();
        writeFileSync(join(dir, ENDPOINT_MAP_FILE), "{not json", "utf8");
        const store = new EndpointMapStore(dir);
        assert.notEqual(store.load().problem, undefined);

        assert.equal(store.rebuild([{ uniqueId: "indigo-1", endpointNumber: 7 }]), true);

        // The unusable bytes are the only surviving record of the old numbers,
        // and "unusable" is often a truncation a human can read.
        const copies = quarantinedMaps(dir);
        assert.equal(copies.length, 1, `expected one quarantined map, got ${copies.join(", ")}`);
        assert.equal(readFileSync(join(dir, copies[0]!), "utf8"), "{not json");
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 7 } });
        assert.equal(store.problem, undefined, "a successful write settles the question");
    });

    it("quarantines on the plain persist path too, not only on a rebuild", () => {
        // ⊗ `refuseReasonFor` deliberately does not refuse a corrupt map on a
        // bridge that has never been commissioned — there is nothing paired to
        // protect — so the first `check` walked straight into `persist` and
        // wrote over the file with no copy taken. That is the one route where
        // the old numbers were destroyed silently, and it is the route every
        // never-paired install with a bad map takes.
        const dir = storage();
        writeFileSync(join(dir, ENDPOINT_MAP_FILE), "{truncated", "utf8");
        const store = new EndpointMapStore(dir);
        assert.notEqual(store.load().problem, undefined);

        assert.deepEqual(store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]), []);

        const copies = quarantinedMaps(dir);
        assert.equal(copies.length, 1, `expected one quarantined map, got ${copies.join(", ")}`);
        assert.equal(readFileSync(join(dir, copies[0]!), "utf8"), "{truncated");
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": { number: 2 } });
        // Once per loaded file: `persist` runs on every check, and a copy per
        // reconcile would bury the one that matters in a directory of clones.
        store.check([{ uniqueId: "indigo-2", endpointNumber: 3 }]);
        assert.deepEqual(quarantinedMaps(dir), copies);
    });

    it("timestamps each copy, so a second failure cannot erase the first", () => {
        // ⊗ The name used to be a fixed `<file>.corrupt`, so the second thing to
        // go wrong overwrote the evidence of the first — and these bytes are the
        // only surviving record of what the endpoint numbers used to be.
        // `identity.json.unreadable-<stamp>` has always got this right.
        const dir = storage();
        let clock = new Date("2026-08-05T10:00:00Z");
        const store = new EndpointMapStore(dir, () => {}, () => clock);

        writeFileSync(join(dir, ENDPOINT_MAP_FILE), "{first", "utf8");
        store.load();
        assert.equal(store.rebuild([{ uniqueId: "indigo-1", endpointNumber: 2 }]), true);

        clock = new Date("2026-08-06T11:30:00Z");
        writeFileSync(join(dir, ENDPOINT_MAP_FILE), "{second", "utf8");
        store.load();
        assert.equal(store.rebuild([{ uniqueId: "indigo-1", endpointNumber: 3 }]), true);

        assert.deepEqual(quarantinedMaps(dir), [
            `${ENDPOINT_MAP_FILE}.corrupt-2026-08-05T10-00-00-000Z`,
            `${ENDPOINT_MAP_FILE}.corrupt-2026-08-06T11-30-00-000Z`,
        ]);
        assert.equal(readFileSync(join(dir, quarantinedMaps(dir)[0]!), "utf8"), "{first");
        assert.equal(readFileSync(join(dir, quarantinedMaps(dir)[1]!), "utf8"), "{second");
    });

    it("says so when it rebuilds from zero endpoints", () => {
        // §1.1 excludes `attach` from the recovery commands, so in the refusal
        // state there are never any live endpoints to rebuild FROM. That is
        // correct behaviour with a log line that used to describe the opposite.
        const logged: string[] = [];
        const store = new EndpointMapStore(storage(), message => logged.push(message));
        store.load();
        assert.equal(store.rebuild([]), true);
        assert.ok(logged.some(line => line.includes("ZERO live endpoints")));
    });
});

describe("refuseReasonFor — the PRD §7 decision", () => {
    it("serves a fresh install with no map at all", () => {
        // XAC1: a fresh install is inert but WORKING. Refusing here would break
        // every user who has never paired anything.
        assert.equal(refuseReasonFor({ commissioned: false }), undefined);
    });

    it("serves an uncommissioned bridge even with an unreadable map", () => {
        // Nothing is paired, so there is no accessory identity to protect and
        // the first reconcile will simply write a new map.
        assert.equal(refuseReasonFor({ commissioned: false, mapProblem: "corrupt" }), undefined);
    });

    it("refuses a commissioned bridge whose map is unreadable", () => {
        assert.equal(
            refuseReasonFor({ commissioned: true, mapProblem: "corrupt" }),
            RefuseReason.mapUnreadable,
        );
    });

    it("SERVES a commissioned bridge with no map at all — the upgrade path", () => {
        // ⊗ The deploy blocker. Every bridge commissioned before E5 shipped is
        // in exactly this state: fabrics, endpoints, and no endpoint-map.json,
        // because the file did not exist yet. Refusing here took every working
        // export offline the moment the node was updated — and bought nothing,
        // because matter.js owns the numbers and this map is only the witness:
        // a missing witness renumbers precisely nothing.
        assert.equal(
            refuseReasonFor({ commissioned: true, commissionedAt: "2026-08-01T00:00:00.000Z" }),
            undefined,
        );
    });

    it("refuses when the witness says paired but the fabric storage is gone", () => {
        // PRD §7's "storage missing but previously commissioned". This is the
        // #1 real-world accessory-duplication cause, and the only reason it is
        // detectable at all is that the witness lives outside matter.js storage.
        assert.equal(
            refuseReasonFor({ commissioned: false, commissionedAt: "2026-08-01T00:00:00.000Z" }),
            RefuseReason.fabricStorageLost,
        );
    });

    it("serves a commissioned bridge with a usable map", () => {
        assert.equal(
            refuseReasonFor({ commissioned: true, commissionedAt: "2026-08-01T00:00:00.000Z" }),
            undefined,
        );
    });
});

describe("bootstrapping a baseline instead of refusing (E5 upgrade path)", () => {
    it("seeds from numbers that already exist and serves", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        assert.equal(store.present, false, "the pre-E5 state: no map on disk");

        const persisted = store.seed(
            [
                { uniqueId: "indigo-1", endpointNumber: 2 },
                { uniqueId: "indigo-2", endpointNumber: 3 },
            ],
            "adopted from matter.js",
        );

        assert.equal(persisted, true);
        assert.equal(store.present, true);
        assert.equal(store.numberFor("indigo-1"), 2);
        // Seeded numbers are a baseline like any other: the very next check
        // must report a device that has since moved, not bless it.
        assert.deepEqual(store.check([{ uniqueId: "indigo-1", endpointNumber: 9 }]), [
            { uniqueId: "indigo-1", expected: 2, actual: 9 },
        ]);
    });

    it("falls back to an empty baseline the first reconcile then fills", () => {
        // The other half of the migration: matter.js's own store could not be
        // read, so there is nothing to adopt — but a bridge that serves and
        // records the live set is strictly better than one that refuses.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();

        assert.equal(store.seed([], "unreadable"), true);
        assert.equal(store.checked, false, "an empty seed has checked nothing");

        assert.deepEqual(store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]), []);
        assert.equal(store.checked, true);
        assert.equal(new EndpointMapStore(dir).load().endpoints.get("indigo-1")?.number, 2);
    });
});

describe("battery (issue #220)", () => {
    it("records battery: true add-only, and it is never written false", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: true }]);

        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "onOffLight", label: "Lamp", battery: true },
        });
        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: true },
        ]);
    });

    it("a battery: false live entry never writes the key at all", () => {
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: false }]);

        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "onOffLight", label: "Lamp" },
        });
    });

    it("keeps battery: true once seen, even after a live pass reports battery: false", () => {
        // §4.1: a battery LOSS is "no evidence right now", never a removal —
        // the map's own witness has to agree, or a restart would rebuild the
        // accessory without PowerSource even though it had one.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: true }]);

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: false }]);

        assert.deepEqual(mapFileIn(dir).endpoints, {
            "indigo-1": { number: 2, role: "onOffLight", label: "Lamp", battery: true },
        });
    });

    it("an entry with no battery opinion at all (omitted) leaves a recorded true untouched", () => {
        // The seed/migration path knows nothing about battery — an absent
        // value must never erase a recorded one, same rule as role/label.
        const dir = storage();
        const store = new EndpointMapStore(dir);
        store.load();
        store.check([{ uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: true }]);

        store.check([{ uniqueId: "indigo-1", endpointNumber: 2 }]);

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp", battery: true },
        ]);
    });

    it("a v2 file lacking the key at all loads exactly as before #220", () => {
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: 2,
                endpoints: { "indigo-1": { number: 2, role: "onOffLight", label: "Lamp" } },
            }),
        );
        const store = new EndpointMapStore(dir);
        const loaded = store.load();

        assert.equal(loaded.problem, undefined);
        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
        ]);
    });

    it("tolerates anything other than a literal true, same convention as numberVoid/orphaned", () => {
        const dir = storage();
        writeFileSync(
            join(dir, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: 2,
                endpoints: { "indigo-1": { number: 2, role: "onOffLight", label: "Lamp", battery: "yes" } },
            }),
        );
        const store = new EndpointMapStore(dir);
        store.load();

        assert.deepEqual(store.restorable(), [
            { uniqueId: "indigo-1", endpointNumber: 2, role: "onOffLight", label: "Lamp" },
        ]);
    });
});
