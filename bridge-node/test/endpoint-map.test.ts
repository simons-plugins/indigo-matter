/**
 * E5: the persisted endpoint-number map, its drift detector, and the PRD §7
 * refuse-to-start decision.
 *
 * All stdlib — the module deliberately has no matter.js import, so the thing
 * that decides whether a user's accessories keep their identity is testable
 * without a Matter stack, a fabric, or a network.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import {
    ENDPOINT_MAP_FILE,
    ENDPOINT_MAP_VERSION,
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

function mapFileIn(dir: string): { version: number; endpoints: Record<string, number> } {
    return JSON.parse(readFileSync(join(dir, ENDPOINT_MAP_FILE), "utf8"));
}

describe("loadEndpointMap", () => {
    it("reports an absent file as absent, not as a fault", () => {
        // The distinction is the whole refuse-to-start rule: a first run has no
        // map and must start, a lost map on a paired bridge must not.
        const loaded = loadEndpointMap(storage());
        assert.equal(loaded.present, false);
        assert.equal(loaded.problem, undefined);
        assert.equal(loaded.numbers.size, 0);
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
        assert.deepEqual([...loaded.numbers.entries()], [
            ["indigo-123456789", 2],
            ["indigo-123456790", 3],
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
            assert.equal(loaded.numbers.size, 0, "a broken map must never be half-adopted");
        });
    }
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
            endpoints: { "indigo-1": 2, "indigo-2": 3 },
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
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": 2 });
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
        assert.deepEqual(mapFileIn(dir).endpoints, { "indigo-1": 4, "indigo-2": 5 });
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
});

describe("refuseReasonFor — the PRD §7 decision", () => {
    it("serves a fresh install with no map at all", () => {
        // XAC1: a fresh install is inert but WORKING. Refusing here would break
        // every user who has never paired anything.
        assert.equal(refuseReasonFor({ commissioned: false, mapPresent: false }), undefined);
    });

    it("serves an uncommissioned bridge even with an unreadable map", () => {
        // Nothing is paired, so there is no accessory identity to protect and
        // the first reconcile will simply write a new map.
        assert.equal(
            refuseReasonFor({ commissioned: false, mapPresent: false, mapProblem: "corrupt" }),
            undefined,
        );
    });

    it("refuses a commissioned bridge whose map is unreadable", () => {
        assert.equal(
            refuseReasonFor({ commissioned: true, mapPresent: true, mapProblem: "corrupt" }),
            RefuseReason.mapUnreadable,
        );
    });

    it("refuses a commissioned bridge with no map at all", () => {
        assert.equal(
            refuseReasonFor({ commissioned: true, mapPresent: false }),
            RefuseReason.mapMissingWhileCommissioned,
        );
    });

    it("refuses when the witness says paired but the fabric storage is gone", () => {
        // PRD §7's "storage missing but previously commissioned". This is the
        // #1 real-world accessory-duplication cause, and the only reason it is
        // detectable at all is that the witness lives outside matter.js storage.
        assert.equal(
            refuseReasonFor({ commissioned: false, mapPresent: true, commissionedAt: "2026-08-01T00:00:00.000Z" }),
            RefuseReason.fabricStorageLost,
        );
    });

    it("serves a commissioned bridge with a usable map", () => {
        assert.equal(
            refuseReasonFor({ commissioned: true, mapPresent: true, commissionedAt: "2026-08-01T00:00:00.000Z" }),
            undefined,
        );
    });
});
