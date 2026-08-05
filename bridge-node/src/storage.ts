/**
 * Persisted bridge identity.
 *
 * The install id, commissioning passcode and discriminator are generated once
 * and kept in `identity.json` **outside** matter.js's own storage context, so a
 * future factory reset (which wipes that context) cannot silently change the
 * serial number a paired ecosystem remembers us by.
 *
 * Stdlib only — no matter.js import — so this is cheap to test.
 */

import { randomInt, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describeError } from "./protocol.js";

/** Inclusive passcode bounds. Matter core spec §5.1.1.6. */
export const PASSCODE_MIN = 1;
export const PASSCODE_MAX = 99_999_998;

/**
 * Trivial passcodes that must never be used. Matter core spec §5.1.7.1.
 * `0` is listed there too and is outside {@link PASSCODE_MIN}, but a persisted
 * file can still carry it, so it is named here rather than left to the bounds.
 */
export const INVALID_PASSCODES: readonly number[] = [
    0, 11111111, 22222222, 33333333, 44444444, 55555555, 66666666, 77777777, 88888888, 99999999, 12345678, 87654321,
];

/** The discriminator is 12 bits. */
export const DISCRIMINATOR_MAX = 0xfff;

const IDENTITY_FILE = "identity.json";

export interface BridgeIdentity {
    /** Stable install id; seeds `serialNumber` and `uniqueId`. */
    installId: string;
    /** Matter setup passcode for the initial (basic) commissioning window. */
    passcode: number;
    /** 12-bit commissioning discriminator. */
    discriminator: number;
    /**
     * ISO 8601 UTC instant this bridge first observed a fabric, or absent if it
     * never has (E5).
     *
     * The witness for PRD §7's "storage missing but previously commissioned".
     * It has to live *here* rather than in matter.js's storage, because the
     * whole failure it describes is matter.js's storage having vanished: a node
     * whose fabrics are gone cannot tell "never paired" from "paired, then the
     * fabric directory was lost" from anything inside the directory that is
     * missing. Absent on identities written before E5, which reads — correctly
     * — as "no evidence of commissioning", so an upgrade never refuses to start.
     */
    commissionedAt?: string;
}

/** True for a passcode inside the legal range and not on the trivial list. */
export function isValidPasscode(passcode: number): boolean {
    if (!Number.isInteger(passcode) || passcode < PASSCODE_MIN || passcode > PASSCODE_MAX) {
        return false;
    }
    return !INVALID_PASSCODES.includes(passcode);
}

/** A cryptographically random passcode that satisfies {@link isValidPasscode}. */
export function generatePasscode(): number {
    for (;;) {
        const candidate = randomInt(PASSCODE_MIN, PASSCODE_MAX + 1);
        if (isValidPasscode(candidate)) {
            return candidate;
        }
    }
}

/** A cryptographically random 12-bit discriminator. */
export function generateDiscriminator(): number {
    return randomInt(0, DISCRIMINATOR_MAX + 1);
}

function isUsableIdentity(value: unknown): value is BridgeIdentity {
    if (typeof value !== "object" || value === null) {
        return false;
    }
    const candidate = value as Partial<BridgeIdentity>;
    return (
        typeof candidate.installId === "string" &&
        candidate.installId.length > 0 &&
        typeof candidate.passcode === "number" &&
        isValidPasscode(candidate.passcode) &&
        typeof candidate.discriminator === "number" &&
        Number.isInteger(candidate.discriminator) &&
        candidate.discriminator >= 0 &&
        candidate.discriminator <= DISCRIMINATOR_MAX
    );
}

function isNotFound(error: unknown): boolean {
    return (error as NodeJS.ErrnoException | null)?.code === "ENOENT";
}

/**
 * Write JSON atomically: a temp file in the *same* directory (so the rename
 * stays within one filesystem and is therefore atomic), then `rename`.
 *
 * A crash mid-write must never leave a half-written file behind, because a
 * half-written file reads as corrupt — and for both files that go through here
 * (`identity.json` and E5's `endpoint-map.json`) "corrupt" is the state that
 * costs a user every accessory they have paired. Exported so the endpoint map
 * is written the same way rather than by a second copy of this.
 */
export function writeJsonAtomic(file: string, value: unknown): void {
    const temp = `${file}.${process.pid}.tmp`;
    try {
        writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
        renameSync(temp, file);
    } catch (error) {
        try {
            unlinkSync(temp);
        } catch {
            // Best effort: the temp file may never have been created.
        }
        throw error;
    }
}

function writeIdentity(file: string, identity: BridgeIdentity): void {
    writeJsonAtomic(file, identity);
}

/**
 * Why a *present* `identity.json` cannot be used, or `undefined` when the file
 * is absent (first run) or fine.
 *
 * This is the E5 half of {@link loadOrCreateIdentity}'s replace-it behaviour,
 * split out rather than folded in so the mint path stays exactly what it was.
 * The two answers must drive opposite decisions: a *missing* identity is a
 * first run and minting is the only thing to do, while a *present but corrupt*
 * one belongs to an install that may well be paired, and minting over it
 * changes the `SerialNumber` and `UniqueID` every ecosystem remembers us by —
 * silently un-pairing the lot. `main.ts` calls this first and refuses to serve
 * endpoints when it answers, which is the same loud-refusal posture PRD §7
 * requires of a lost endpoint map.
 */
export function identityProblem(storagePath: string): string | undefined {
    const file = join(storagePath, IDENTITY_FILE);
    try {
        const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
        return isUsableIdentity(parsed)
            ? undefined
            : `${file} is present but not a usable identity (missing or out-of-range fields)`;
    } catch (error) {
        return isNotFound(error) ? undefined : `${file} is unreadable (${describeError(error)})`;
    }
}

/**
 * Record that this bridge has been commissioned, if it is not recorded already.
 *
 * Called the first time a fabric is observed. Returns the identity to keep
 * using — the same object when nothing needed writing, so the caller can hold
 * one reference. A write failure is reported and swallowed: losing the witness
 * degrades a future refuse-to-start into a silent re-init, which is bad, but
 * failing the commissioning that just succeeded is worse and immediate.
 */
export function markCommissioned(
    storagePath: string,
    identity: BridgeIdentity,
    log: (message: string) => void = () => {},
    now: () => Date = () => new Date(),
): BridgeIdentity {
    if (identity.commissionedAt !== undefined) {
        return identity;
    }
    const updated: BridgeIdentity = { ...identity, commissionedAt: now().toISOString() };
    try {
        writeIdentity(join(storagePath, IDENTITY_FILE), updated);
    } catch (error) {
        log(`Could not record the commissioning marker in identity.json: ${describeError(error)}`);
    }
    return updated;
}

/**
 * Drop the commissioning witness — the §3.10 factory-reset counterpart of
 * {@link markCommissioned}. Without this a reset node would start once, see no
 * fabrics against a set `commissionedAt`, and refuse to serve anything on the
 * grounds that its own reset had lost the fabric storage.
 */
export function clearCommissioned(
    storagePath: string,
    identity: BridgeIdentity,
    log: (message: string) => void = () => {},
): BridgeIdentity {
    if (identity.commissionedAt === undefined) {
        return identity;
    }
    const { commissionedAt: _dropped, ...rest } = identity;
    try {
        writeIdentity(join(storagePath, IDENTITY_FILE), rest);
    } catch (error) {
        log(`Could not clear the commissioning marker in identity.json: ${describeError(error)}`);
    }
    return rest;
}

/**
 * Read `identity.json` from {@link storagePath}, creating it (and the directory)
 * with freshly randomised values on first run.
 *
 * An unreadable or invalid file is replaced here. That is only safe because
 * `main.ts` calls {@link identityProblem} *first* and refuses to serve endpoints
 * when a present file is unusable (E5) — reaching this function's replace branch
 * therefore means the caller has already decided the loss is acceptable.
 */
export function loadOrCreateIdentity(
    storagePath: string,
    log: (message: string) => void = () => {},
): BridgeIdentity {
    mkdirSync(storagePath, { recursive: true });
    const file = join(storagePath, IDENTITY_FILE);

    // `undefined` means "no file at all"; anything else is why we are replacing
    // a file that does exist — the distinction E5 turns into refuse-to-start.
    let problem: string | undefined;
    try {
        const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
        if (isUsableIdentity(parsed)) {
            log(`Loaded bridge identity from ${file}`);
            return parsed;
        }
        problem = "present but not a usable identity (missing or out-of-range fields)";
    } catch (error) {
        problem = isNotFound(error) ? undefined : `unreadable (${describeError(error)})`;
    }

    if (problem === undefined) {
        log(`No identity at ${file}; minting new bridge identity`);
    } else {
        log(`Replacing bridge identity at ${file}: ${problem}`);
    }

    const identity: BridgeIdentity = {
        installId: randomUUID(),
        passcode: generatePasscode(),
        discriminator: generateDiscriminator(),
    };
    writeIdentity(file, identity);
    return identity;
}

/**
 * Matter `SerialNumber` is capped at 32 characters, so the UUID is used with its
 * dashes stripped. Matter requires `UniqueID` and `SerialNumber` to differ, so
 * the node's UniqueID takes the full value and this takes the first half.
 */
export function serialNumberFor(identity: BridgeIdentity): string {
    return identity.installId.replace(/-/g, "").slice(0, 16);
}

/** The root node's Matter `UniqueID` — distinct from {@link serialNumberFor}. */
export function nodeUniqueIdFor(identity: BridgeIdentity): string {
    return identity.installId.replace(/-/g, "").slice(0, 32);
}
