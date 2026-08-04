/**
 * Identity persistence: randomised once, stable thereafter, never a trivial
 * passcode.
 */

import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
        assert.equal(isValidPasscode(PASSCODE_MAX + 1), false);
        assert.equal(isValidPasscode(1.5), false);
        assert.equal(isValidPasscode(20202021), true);
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
