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
import { mkdirSync, readdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
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

/**
 * What {@link quarantineIdentity} names the files it moves aside.
 *
 * Shared with {@link identityProblem} on purpose: the refusal is only sticky
 * because it can recognise its own leavings, and two independently written
 * copies of this string would drift apart in exactly the release where the
 * refusal stopped firing and nobody noticed.
 */
const QUARANTINE_PREFIX = `${IDENTITY_FILE}.unreadable-`;

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

/** Absent, fine, or there-and-unusable — the three answers that differ. */
type IdentityFileState = { kind: "absent" } | { kind: "usable" } | { kind: "unusable"; problem: string };

function inspectIdentityFile(storagePath: string): IdentityFileState {
    const file = join(storagePath, IDENTITY_FILE);
    try {
        const parsed: unknown = JSON.parse(readFileSync(file, "utf8"));
        return isUsableIdentity(parsed)
            ? { kind: "usable" }
            : {
                  kind: "unusable",
                  problem: `${file} is present but not a usable identity (missing or out-of-range fields)`,
              };
    } catch (error) {
        return isNotFound(error)
            ? { kind: "absent" }
            : { kind: "unusable", problem: `${file} is unreadable (${describeError(error)})` };
    }
}

/**
 * Why the identity file that is *there* cannot be used, ignoring any
 * quarantined siblings — `undefined` when it is fine **or absent**.
 *
 * The narrow half of {@link identityProblem}, for the one caller that must not
 * confuse "there is no file" with "there is a file I cannot read": §3.10's
 * factory reset reads this back to confirm the commissioning witness is gone,
 * and an absent file means it certainly is.
 */
export function identityFileProblem(storagePath: string): string | undefined {
    const state = inspectIdentityFile(storagePath);
    return state.kind === "unusable" ? state.problem : undefined;
}

/**
 * Why this storage directory cannot hand back a usable identity, or
 * `undefined` when it can (or when this is a genuine first run).
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
 *
 * **A quarantined sibling counts, which is what makes the refusal survive a
 * restart.** The first start over an unusable file moves it to
 * `identity.json.unreadable-<stamp>` and mints a replacement in memory only —
 * so the *second* start finds no `identity.json` at all, and reading that as a
 * first run would mint AND WRITE one, serving under a `SerialNumber` no paired
 * ecosystem has ever seen, with a routine first-run log line to show for it.
 * The refusal has to last until a human resolves it, and the evidence that one
 * has not is the marker still lying there.
 */
export function identityProblem(storagePath: string): string | undefined {
    const state = inspectIdentityFile(storagePath);
    if (state.kind !== "absent") {
        return state.kind === "unusable" ? state.problem : undefined;
    }
    return quarantinedIdentityProblem(storagePath);
}

/**
 * The refusal a leftover quarantine marker carries, naming the way out.
 *
 * A listing we cannot take is deliberately *not* a refusal: this function is
 * part of a never-throwing guard, and inventing one out of a failed `readdir`
 * would block a first run over a transient permissions problem — the opposite
 * of the trade the refusal is making everywhere else.
 */
function quarantinedIdentityProblem(storagePath: string): string | undefined {
    let markers: string[];
    try {
        markers = readdirSync(storagePath)
            .filter(name => name.startsWith(QUARANTINE_PREFIX))
            .sort();
    } catch {
        return undefined;
    }
    if (markers.length === 0) {
        return undefined;
    }
    return (
        `${join(storagePath, IDENTITY_FILE)} is missing, but ${markers.length === 1 ? "a" : markers.length} ` +
        `quarantined identit${markers.length === 1 ? "y" : "ies"} (${markers.join(", ")}) still ` +
        `sit${markers.length === 1 ? "s" : ""} beside it — an earlier start could not read the identity ` +
        `and moved it aside. Restore or repair one of them as ${IDENTITY_FILE} in ${storagePath} and ` +
        "restart. Deleting them all is the deliberate \"start fresh as a brand-new bridge\" choice, and " +
        "it loses every pairing this bridge already has."
    );
}

/**
 * The outcome of moving the commissioning witness.
 *
 * The identity is still returned — the caller has to keep using one whatever
 * happened — but `persisted` is now an answer rather than a silence. A caller
 * that reports success over a witness that never reached disk is describing a
 * different bridge from the one that will boot next: `factory_reset` leaving
 * `commissionedAt` behind makes the very next start refuse with
 * `fabricStorageLost`, blaming lost storage for the reset the user asked for.
 */
export interface WitnessWrite {
    identity: BridgeIdentity;
    /** False when the change was made in memory only. */
    persisted: boolean;
    /** Why it did not reach disk, when it did not. */
    problem?: string;
}

/**
 * Record that this bridge has been commissioned, if it is not recorded already.
 *
 * Called the first time a fabric is observed. Returns the identity to keep
 * using — the same object when nothing needed writing, so the caller can hold
 * one reference. A write failure is reported and **not** thrown: losing the
 * witness degrades a future refuse-to-start into a silent re-init, which is
 * bad, but failing the commissioning that just succeeded is worse and
 * immediate. It is reported to the caller as well as to the log, because the
 * log in this milestone is a terminal somebody closed (§4.3 `warnings`).
 */
export function markCommissioned(
    storagePath: string,
    identity: BridgeIdentity,
    log: (message: string) => void = () => {},
    now: () => Date = () => new Date(),
): WitnessWrite {
    if (identity.commissionedAt !== undefined) {
        return { identity, persisted: true };
    }
    const updated: BridgeIdentity = { ...identity, commissionedAt: now().toISOString() };
    try {
        writeIdentity(join(storagePath, IDENTITY_FILE), updated);
    } catch (error) {
        const problem = `Could not record the commissioning marker in identity.json: ${describeError(error)}`;
        log(problem);
        return { identity: updated, persisted: false, problem };
    }
    return { identity: updated, persisted: true };
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
): WitnessWrite {
    if (identity.commissionedAt === undefined) {
        return { identity, persisted: true };
    }
    const { commissionedAt: _dropped, ...rest } = identity;
    try {
        writeIdentity(join(storagePath, IDENTITY_FILE), rest);
    } catch (error) {
        const problem = `Could not clear the commissioning marker in identity.json: ${describeError(error)}`;
        log(problem);
        return { identity: rest, persisted: false, problem };
    }
    return { identity: rest, persisted: true };
}

/**
 * `identity.json` exactly as it is on disk right now, or `undefined` when it is
 * absent or unusable.
 *
 * The verification half of {@link clearCommissioned}: §3.10 promises the
 * witness is gone, and the only way to say that truthfully is to go and look.
 * A write can report success and still not be what the next start reads.
 */
export function readIdentity(storagePath: string): BridgeIdentity | undefined {
    try {
        const parsed: unknown = JSON.parse(readFileSync(join(storagePath, IDENTITY_FILE), "utf8"));
        return isUsableIdentity(parsed) ? parsed : undefined;
    } catch {
        return undefined;
    }
}

/** Fresh random identity values. Pure — nothing is written. */
export function mintIdentity(): BridgeIdentity {
    return {
        installId: randomUUID(),
        passcode: generatePasscode(),
        discriminator: generateDiscriminator(),
    };
}

/**
 * Move an unusable `identity.json` aside instead of writing over it (E5).
 *
 * This file holds the `SerialNumber` and `UniqueID` every paired ecosystem
 * remembers us by. When it cannot be parsed, the *last* thing to do is replace
 * it: the bytes on disk may be a truncation, a stray edit or a half-finished
 * copy that a human can repair, and they are the only surviving record of an
 * identity that cannot be regenerated. Renaming rather than deleting means the
 * refusal that follows is recoverable; minting over it was not.
 *
 * Returns where it went, or `undefined` if there was nothing to move (or the
 * move itself failed — which is reported, not thrown, because the node is about
 * to refuse to serve endpoints either way).
 */
export function quarantineIdentity(
    storagePath: string,
    log: (message: string) => void = () => {},
    now: () => Date = () => new Date(),
): string | undefined {
    const file = join(storagePath, IDENTITY_FILE);
    const stamp = now().toISOString().replace(/[:.]/g, "-");
    const target = join(storagePath, `${QUARANTINE_PREFIX}${stamp}`);
    try {
        renameSync(file, target);
        return target;
    } catch (error) {
        if (!isNotFound(error)) {
            log(`Could not move the unusable identity aside: ${describeError(error)}`);
        }
        return undefined;
    }
}

/**
 * Read `identity.json` from {@link storagePath}, creating it (and the directory)
 * with freshly randomised values on first run.
 *
 * **Only ever called when there is no usable file to lose.** `main.ts` calls
 * {@link identityProblem} first, and a present-but-unusable file goes down the
 * {@link quarantineIdentity} + {@link mintIdentity} path instead, which writes
 * nothing. The `problem !== undefined` branch below therefore survives only for
 * the race where the file becomes unreadable between those two reads — a case
 * where there is no original left to preserve anyway.
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

    const identity = mintIdentity();
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
