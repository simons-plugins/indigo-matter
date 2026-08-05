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
    clearCommissioned,
    DISCRIMINATOR_MAX,
    generateDiscriminator,
    generatePasscode,
    identityProblem,
    INVALID_PASSCODES,
    isValidPasscode,
    loadOrCreateIdentity,
    markCommissioned,
    mintIdentity,
    nodeUniqueIdFor,
    quarantineIdentity,
    readIdentity,
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

describe("identityProblem — the E5 refuse-to-start guard", () => {
    it("answers undefined when there is no identity yet (a first run)", () => {
        const dir = scratch();
        try {
            // Minting over an absent file is the only correct thing to do, and
            // the guard must not turn a fresh install into a refusal.
            assert.equal(identityProblem(dir), undefined);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("answers undefined for an identity that is fine", () => {
        const dir = scratch();
        try {
            loadOrCreateIdentity(dir);
            assert.equal(identityProblem(dir), undefined);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    for (const [name, body] of [
        ["unparseable", "{ not json"],
        ["missing installId", JSON.stringify({ passcode: 20202021, discriminator: 3840 })],
        ["a trivial passcode", JSON.stringify({ installId: "x", passcode: 11111111, discriminator: 3840 })],
        ["an out-of-range discriminator", JSON.stringify({ installId: "x", passcode: 20202021, discriminator: 99999 })],
    ] as const) {
        it(`reports a present-but-${name} identity`, () => {
            const dir = scratch();
            try {
                writeFileSync(join(dir, "identity.json"), body);
                // Not "replace it": regenerating changes the SerialNumber and
                // UniqueID that every paired ecosystem remembers us by, which
                // un-pairs the lot without a word. main.ts refuses instead.
                assert.ok(identityProblem(dir) !== undefined);
            } finally {
                rmSync(dir, { recursive: true, force: true });
            }
        });
    }

    it("does not itself modify or create the file", () => {
        const dir = scratch();
        try {
            identityProblem(dir);
            assert.deepEqual(readdirSync(dir), [], "the guard must be a pure read");
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });
});

describe("an unusable identity.json is moved aside, never minted over (E5 A3)", () => {
    it("preserves the original bytes and writes nothing in its place", () => {
        // ⊗ main.ts called identityProblem() and then loadOrCreateIdentity()
        // unconditionally — and the mint writes through `rename`, so the
        // unreadable file was destroyed one line BEFORE the refusal that exists
        // to protect it. Those bytes carry the SerialNumber and UniqueID every
        // paired ecosystem knows this bridge by, and they cannot be regenerated.
        const dir = scratch();
        try {
            writeFileSync(join(dir, "identity.json"), "{truncated", "utf8");
            assert.notEqual(identityProblem(dir), undefined);

            const movedTo = quarantineIdentity(dir, () => {}, () => new Date("2026-08-05T10:00:00Z"));

            assert.equal(movedTo, join(dir, "identity.json.unreadable-2026-08-05T10-00-00-000Z"));
            assert.equal(readFileSync(movedTo!, "utf8"), "{truncated");
            assert.deepEqual(
                readdirSync(dir),
                ["identity.json.unreadable-2026-08-05T10-00-00-000Z"],
                "no replacement may be written while we are refusing to serve",
            );
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("mints in memory only", () => {
        const dir = scratch();
        try {
            const identity = mintIdentity();
            assert.ok(isValidPasscode(identity.passcode));
            assert.ok(identity.discriminator <= DISCRIMINATOR_MAX);
            assert.deepEqual(readdirSync(dir), [], "mintIdentity must touch no disk at all");
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("is a silent no-op when there is nothing to move", () => {
        const dir = scratch();
        try {
            const logged: string[] = [];
            assert.equal(quarantineIdentity(dir, message => logged.push(message)), undefined);
            assert.deepEqual(logged, []);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });
});

describe("the commissioning witness (PRD §7)", () => {
    it("stamps commissionedAt once and keeps the first value", () => {
        const dir = scratch();
        try {
            const identity = loadOrCreateIdentity(dir);
            assert.equal(identity.commissionedAt, undefined, "a fresh identity has never been paired");

            const first = markCommissioned(dir, identity, () => {}, () => new Date("2026-08-01T10:00:00Z"));
            const again = markCommissioned(dir, first.identity, () => {}, () => new Date("2026-09-09T10:00:00Z"));

            assert.equal(first.persisted, true);
            assert.equal(first.identity.commissionedAt, "2026-08-01T10:00:00.000Z");
            assert.equal(again.identity, first.identity, "a second fabric must not restamp the witness");
            const onDisk = JSON.parse(readFileSync(join(dir, "identity.json"), "utf8"));
            assert.equal(onDisk.commissionedAt, "2026-08-01T10:00:00.000Z");
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("survives a reload — that is the entire point of it", () => {
        const dir = scratch();
        try {
            markCommissioned(dir, loadOrCreateIdentity(dir), () => {}, () => new Date("2026-08-01T10:00:00Z"));
            // The witness has to outlive the process, because the failure it
            // witnesses (matter.js's storage vanishing) is only visible at the
            // NEXT start.
            assert.equal(loadOrCreateIdentity(dir).commissionedAt, "2026-08-01T10:00:00.000Z");
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("clears on a factory reset, so the reset does not refuse to itself", () => {
        const dir = scratch();
        try {
            const paired = markCommissioned(dir, loadOrCreateIdentity(dir), () => {}).identity;

            const reset = clearCommissioned(dir, paired, () => {});

            assert.equal(reset.persisted, true);
            assert.equal(reset.identity.commissionedAt, undefined);
            assert.equal(loadOrCreateIdentity(dir).commissionedAt, undefined);
            // §3.10 promises the witness is GONE; verify it by reading it back,
            // which is what factoryReset now does before it reports completion.
            assert.equal(readIdentity(dir)?.commissionedAt, undefined);
            // Everything that IS the identity must survive the clear.
            assert.equal(reset.identity.installId, paired.installId);
            assert.equal(reset.identity.passcode, paired.passcode);
            assert.equal(reset.identity.discriminator, paired.discriminator);
        } finally {
            rmSync(dir, { recursive: true, force: true });
        }
    });

    it("keeps the bridge running when the witness cannot be written", () => {
        const dir = scratch();
        try {
            const identity = loadOrCreateIdentity(dir);
            chmodSync(dir, 0o500);
            const logged: string[] = [];

            const marked = markCommissioned(dir, identity, message => logged.push(message));

            chmodSync(dir, 0o700);
            // Failing the commissioning that just succeeded, over a marker for a
            // hypothetical future start, would be the worse trade — but it has
            // to be loud, because the refusal it enables is now unarmed.
            assert.ok(marked.identity.commissionedAt !== undefined);
            // ⊗ B1: the caller used to get only the identity back and could not
            // tell this from a write that landed.
            assert.equal(marked.persisted, false);
            assert.match(marked.problem ?? "", /Could not record the commissioning marker/);
            assert.ok(logged.some(line => line.includes("Could not record the commissioning marker")));
        } finally {
            chmodSync(dir, 0o700);
            rmSync(dir, { recursive: true, force: true });
        }
    });
});
