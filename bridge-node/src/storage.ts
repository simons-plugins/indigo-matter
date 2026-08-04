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
 * Write `identity.json` atomically: a temp file in the *same* directory (so the
 * rename stays within one filesystem and is therefore atomic), then `rename`.
 * A crash mid-write must never leave a half-written identity behind, because a
 * half-written identity reads as corrupt and gets regenerated.
 */
function writeIdentity(file: string, identity: BridgeIdentity): void {
    const temp = `${file}.${process.pid}.tmp`;
    try {
        writeFileSync(temp, `${JSON.stringify(identity, null, 2)}\n`, { mode: 0o600 });
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

/**
 * Read `identity.json` from {@link storagePath}, creating it (and the directory)
 * with freshly randomised values on first run. An unreadable or invalid file is
 * replaced — a bridge that cannot advertise is worse than one that needs
 * re-pairing, and E0 has nothing paired to protect yet.
 *
 * TODO(E1): a corrupt-but-present identity must refuse to start (PRD §4.3) —
 * regeneration un-pairs every ecosystem. Only the missing-file branch may mint.
 */
export function loadOrCreateIdentity(
    storagePath: string,
    log: (message: string) => void = () => {},
): BridgeIdentity {
    mkdirSync(storagePath, { recursive: true });
    const file = join(storagePath, IDENTITY_FILE);

    // `undefined` means "no file at all"; anything else is why we are replacing
    // a file that does exist — the distinction E1 turns into refuse-to-start.
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
 * {@link uniqueIdFor} takes the full value and this takes the first half.
 */
export function serialNumberFor(identity: BridgeIdentity): string {
    return identity.installId.replace(/-/g, "").slice(0, 16);
}

/** The root node's Matter `UniqueID` — distinct from {@link serialNumberFor}. */
export function nodeUniqueIdFor(identity: BridgeIdentity): string {
    return identity.installId.replace(/-/g, "").slice(0, 32);
}
