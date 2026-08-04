/**
 * Identity persistence: randomised once, stable thereafter, never a trivial
 * passcode.
 */

import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";

import {
    DISCRIMINATOR_MAX,
    generateDiscriminator,
    generatePasscode,
    INVALID_PASSCODES,
    isValidPasscode,
    loadOrCreateIdentity,
    nodeUniqueIdFor,
    PASSCODE_MAX,
    PASSCODE_MIN,
    serialNumberFor,
} from "../src/storage.js";

function scratch(): string {
    return mkdtempSync(join(tmpdir(), "indigo-bridge-test-"));
}

describe("passcode generation", () => {
    it("never produces a trivial passcode", () => {
        for (let i = 0; i < 2000; i++) {
            const passcode = generatePasscode();
            assert.ok(!INVALID_PASSCODES.includes(passcode), `generated trivial passcode ${passcode}`);
            assert.ok(passcode >= 1 && passcode <= PASSCODE_MAX);
        }
    });

    it("rejects the spec's invalid list and out-of-range values", () => {
        for (const invalid of INVALID_PASSCODES) {
            assert.equal(isValidPasscode(invalid), false, `${invalid} should be invalid`);
        }
        assert.equal(isValidPasscode(0), false);
        assert.equal(isValidPasscode(-1), false);
        assert.equal(isValidPasscode(PASSCODE_MAX + 1), false);
        assert.equal(isValidPasscode(1.5), false);
        assert.equal(isValidPasscode(20202021), true);
        // The inclusive bounds are legal values, not off-by-one rejects.
        assert.equal(isValidPasscode(PASSCODE_MIN), true);
        assert.equal(isValidPasscode(1), true);
        assert.equal(isValidPasscode(PASSCODE_MAX), true);
    });

    it("names 0 on the invalid list, not just outside the bounds", () => {
        // A persisted `"passcode": 0` must be caught by the spec's trivial list
        // as well as by the range check.
        assert.ok(INVALID_PASSCODES.includes(0));
    });

    it("never uses the matter.js example defaults verbatim by chance-free construction", () => {
        // 20202021/3840 are legal values; what matters is that we do not *default*
        // to them. A fresh identity is drawn from the full range each time.
        const seen = new Set<number>();
        for (let i = 0; i < 200; i++) {
            seen.add(generatePasscode());
        }
        assert.ok(seen.size > 190, "passcodes are not being randomised");
    });

    it("produces 12-bit discriminators", () => {
        for (let i = 0; i < 2000; i++) {
            const value = generateDiscriminator();
            assert.ok(Number.isInteger(value) && value >= 0 && value <= DISCRIMINATOR_MAX);
        }
    });
});

describe("loadOrCreateIdentity", () => {
    it("creates on first run and returns the same identity thereafter", () => {
        const dir = scratch();
        try {
            const first = loadOrCreateIdentity(dir);
            assert.ok(isValidPasscode(first.passcode));
            assert.ok(first.discriminator <= DISCRIMINATOR_MAX);
            assert.ok(first.installId.length > 0);

            const second = loadOrCreateIdentity(dir);
            assert.deepEqual(second, first);

            const onDisk = JSON.parse(readFileSync(join(dir, "identity.json"), "utf8"));
            assert.deepEqual(onDisk, first);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("replaces a corrupt or invalid identity file", () => {
        const dir = scratch();
        try {
            writeFileSync(join(dir, "identity.json"), "{ not json");
            const recovered = loadOrCreateIdentity(dir);
            assert.ok(isValidPasscode(recovered.passcode));

            writeFileSync(join(dir, "identity.json"), JSON.stringify({ installId: "x", passcode: 11111111, discriminator: 1 }));
            const replaced = loadOrCreateIdentity(dir);
            assert.ok(isValidPasscode(replaced.passcode));
            assert.notEqual(replaced.passcode, 11111111);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("regenerates an identity whose discriminator is out of 12-bit range, and says so", () => {
        const dir = scratch();
        try {
            writeFileSync(
                join(dir, "identity.json"),
                JSON.stringify({ installId: "x", passcode: 20202021, discriminator: DISCRIMINATOR_MAX + 1 }),
            );
            const logs: string[] = [];
            const replaced = loadOrCreateIdentity(dir, message => logs.push(message));
            assert.ok(replaced.discriminator <= DISCRIMINATOR_MAX);
            assert.notEqual(replaced.installId, "x");
            assert.equal(logs.length, 1);
            assert.match(logs[0]!, /Replacing bridge identity/);
            assert.match(logs[0]!, /not a usable identity/);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("distinguishes a missing file from an unreadable one in the log", () => {
        const dir = scratch();
        try {
            const minted: string[] = [];
            loadOrCreateIdentity(dir, message => minted.push(message));
            assert.equal(minted.length, 1);
            assert.match(minted[0]!, /minting new bridge identity/);

            const reloaded: string[] = [];
            loadOrCreateIdentity(dir, message => reloaded.push(message));
            assert.match(reloaded[0]!, /Loaded bridge identity/);

            writeFileSync(join(dir, "identity.json"), "{ not json");
            const corrupt: string[] = [];
            loadOrCreateIdentity(dir, message => corrupt.push(message));
            assert.match(corrupt[0]!, /Replacing bridge identity/);
            // The caught parse error is quoted, so the log says *why*.
            assert.match(corrupt[0]!, /unreadable \(.+\)/);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("writes atomically and leaves no temp file behind", () => {
        const dir = scratch();
        try {
            loadOrCreateIdentity(dir);
            assert.deepEqual(readdirSync(dir), ["identity.json"]);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("cleans up the temp file when the rename cannot happen", t => {
        if (process.getuid?.() === 0) {
            t.skip("root ignores directory permissions");
            return;
        }
        const dir = scratch();
        const nested = join(dir, "bridge");
        try {
            // A read-only directory makes writeFileSync fail after mkdirSync
            // succeeded, which is the path the temp-file cleanup guards.
            loadOrCreateIdentity(nested);
            chmodSync(nested, 0o500);
            writeFileSync(join(nested, "identity.json"), "{ not json");
            assert.throws(() => loadOrCreateIdentity(nested));
            chmodSync(nested, 0o700);
            assert.deepEqual(readdirSync(nested), ["identity.json"]);
        } finally {
            try {
                chmodSync(nested, 0o700);
            } catch {
                // Directory may not exist if the first load failed.
            }
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("derives a Matter-legal serial number and a distinct unique id", () => {
        const dir = scratch();
        try {
            const identity = loadOrCreateIdentity(dir);
            const serial = serialNumberFor(identity);
            const unique = nodeUniqueIdFor(identity);
            for (const value of [serial, unique]) {
                assert.ok(value.length > 0 && value.length <= 32);
                assert.ok(!value.includes("-"));
            }
            // Matter requires UniqueID and SerialNumber to differ.
            assert.notEqual(serial, unique);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });
});
