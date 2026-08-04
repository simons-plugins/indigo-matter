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
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

/** Inclusive passcode bounds. Matter core spec §5.1.1.6. */
export const PASSCODE_MIN = 1;
export const PASSCODE_MAX = 99_999_998;

/** Trivial passcodes that must never be used. Matter core spec §5.1.7.1. */
export const INVALID_PASSCODES: readonly number[] = [
    11111111, 22222222, 33333333, 44444444, 55555555, 66666666, 77777777, 88888888, 99999999, 12345678, 87654321,
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

/**
 * Read `identity.json` from {@link storagePath}, creating it (and the directory)
 * with freshly randomised values on first run. An unreadable or invalid file is
 * replaced — a bridge that cannot advertise is worse than one that needs
 * re-pairing, and E0 has nothing paired to protect yet.
 */
export function loadOrCreateIdentity(storagePath: string): BridgeIdentity {
    mkdirSync(storagePath, { recursive: true });
    const file = join(storagePath, IDENTITY_FILE);

    try {
        const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
        if (isUsableIdentity(parsed)) {
            return parsed;
        }
    } catch {
        // Missing or corrupt — fall through and mint a new identity.
    }

    const identity: BridgeIdentity = {
        installId: randomUUID(),
        passcode: generatePasscode(),
        discriminator: generateDiscriminator(),
    };
    writeFileSync(file, `${JSON.stringify(identity, null, 2)}\n`, { mode: 0o600 });
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
