/** The #203 countdown marker file: its failure modes. All stdlib, no Matter stack. */

import assert from "node:assert/strict";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { COUNTDOWNS_FILE, CountdownStore } from "../src/countdowns.js";

const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
mkdirSync(SCRATCH_ROOT, { recursive: true });
const scratch: string[] = [];
after(() => {
    for (const dir of scratch) {
        try {
            chmodSync(dir, 0o700);
        } catch {
            // gone
        }
        rmSync(dir, { recursive: true, force: true });
    }
});
const storage = (): string => {
    const dir = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-countdowns-"));
    scratch.push(dir);
    return dir;
};
const fileOf = (dir: string): string => join(dir, COUNTDOWNS_FILE);

function loaded(dir: string): { store: CountdownStore; logs: string[] } {
    const logs: string[] = [];
    const store = new CountdownStore(dir, message => logs.push(message));
    store.load();
    return { store, logs };
}

describe("CountdownStore", () => {
    it("persists a deadline and a fresh load reads it back", () => {
        const dir = storage();
        loaded(dir).store.setCountdown("indigo-1", 1_700_000_000_000);
        assert.equal(loaded(dir).store.countdownDeadline("indigo-1"), 1_700_000_000_000);
    });

    it("clearing removes it from disk, and clearing nothing costs no write", () => {
        const dir = storage();
        const { store } = loaded(dir);
        store.setCountdown("indigo-1", 5);
        store.clearCountdown("indigo-1");
        assert.equal(loaded(dir).store.countdownDeadline("indigo-1"), undefined);
        writeFileSync(fileOf(dir), "sentinel");
        store.clearCountdown("indigo-1");
        store.clearCountdown("indigo-404");
        assert.equal(readFileSync(fileOf(dir), "utf8"), "sentinel");
    });

    it("a missing file means no markers and says nothing", () => {
        const { store, logs } = loaded(storage());
        assert.equal(store.countdownDeadline("indigo-1"), undefined);
        assert.deepEqual(logs, []);
    });

    for (const [name, content] of [
        ["not JSON", "{nope"],
        ["no countdowns object", JSON.stringify({ version: 1 })],
        ["an array", JSON.stringify({ version: 1, countdowns: [1] })],
    ] as const) {
        it(`${name}: no markers, one warning, and the next write repairs it`, () => {
            const dir = storage();
            writeFileSync(fileOf(dir), content);
            const { store, logs } = loaded(dir);
            assert.equal(store.countdownDeadline("indigo-1"), undefined);
            assert.equal(logs.filter(line => /not usable/.test(line)).length, 1);
            store.setCountdown("indigo-1", 9);
            assert.equal(logs.filter(line => /not usable/.test(line)).length, 1, "said once");
            assert.equal(loaded(dir).store.countdownDeadline("indigo-1"), 9);
        });
    }

    it("an unreadable file (permissions) is no markers with one warning", () => {
        const dir = storage();
        writeFileSync(fileOf(dir), JSON.stringify({ version: 1, countdowns: { "indigo-1": 5 } }));
        chmodSync(fileOf(dir), 0o000);
        try {
            const { store, logs } = loaded(dir);
            if (process.getuid?.() !== 0) {
                assert.equal(store.countdownDeadline("indigo-1"), undefined);
                assert.equal(logs.filter(line => /could not be read/.test(line)).length, 1);
            }
        } finally {
            chmodSync(fileOf(dir), 0o600);
        }
    });

    it("drops non-finite, negative and non-numeric entries, keeps the good ones", () => {
        const dir = storage();
        writeFileSync(
            fileOf(dir),
            '{"version":1,"countdowns":{"indigo-1":5,"indigo-2":-3,"indigo-3":"soon","indigo-4":0,"indigo-5":null}}',
        );
        const { store } = loaded(dir);
        assert.equal(store.countdownDeadline("indigo-1"), 5);
        for (const id of ["indigo-2", "indigo-3", "indigo-4", "indigo-5"]) {
            assert.equal(store.countdownDeadline(id), undefined);
        }
    });

    it("refuses to record a non-finite or negative deadline", () => {
        const { store } = loaded(storage());
        assert.equal(store.setCountdown("indigo-1", Number.NaN), false);
        assert.equal(store.setCountdown("indigo-1", -1), false);
        assert.equal(store.setCountdown("indigo-1", Number.POSITIVE_INFINITY), false);
        assert.equal(store.countdownDeadline("indigo-1"), undefined);
    });

    it("prune drops markers for identities the endpoint map does not know, and only those", () => {
        const dir = storage();
        const { store } = loaded(dir);
        store.setCountdown("indigo-1", 5);
        store.setCountdown("indigo-2", 6);
        store.prune(new Set(["indigo-2"]));
        const reread = loaded(dir).store;
        assert.equal(reread.countdownDeadline("indigo-1"), undefined);
        assert.equal(reread.countdownDeadline("indigo-2"), 6);
    });

    it("a write that fails is logged, never thrown", () => {
        const dir = storage();
        const { store, logs } = loaded(dir);
        chmodSync(dir, 0o500);
        try {
            if (process.getuid?.() !== 0) {
                assert.doesNotThrow(() => store.setCountdown("indigo-1", 5));
                assert.ok(logs.some(line => /Could not write/.test(line)));
            }
        } finally {
            chmodSync(dir, 0o700);
        }
    });
});
