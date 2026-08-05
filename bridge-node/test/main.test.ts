/**
 * `main.ts` as a real process — the only file in the node with no other cover.
 *
 * Everything else here is unit-tested behind a seam, but the entry point IS the
 * seam: what it exits with is what launchd reads. The agent runs with
 * `KeepAlive { SuccessfulExit: false }`, so **exit 0 means "stay stopped" and
 * anything else means "respawn immediately"** — and E5 changed that code path,
 * because `ServerNode.erase()` leaves a ref'd timer `close()` never clears, so a
 * clean stop *after a factory reset* used to sit until the escape hatch fired
 * and then exit 1. Nothing anywhere asserted the number.
 *
 * These spawn the built entry point on ephemeral ports in a scratch storage dir.
 * They are slower than the rest of the suite and they are worth it: a wrong exit
 * code is a restart loop or a bridge that never comes back, and neither shows up
 * in any other test.
 */

import assert from "node:assert/strict";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { after, describe, it } from "node:test";

import { ErrorCode, PROTOCOL_VERSION } from "../src/protocol.js";
import { TestClient } from "./client.js";

/**
 * `.test-build/test/` at run time, so the compiled entry point is two levels up
 * in `dist/`. Built by `npm run build`, which `npm test` does not run — hence
 * the explicit skip below rather than a confusing spawn failure.
 */
const here = dirname(fileURLToPath(import.meta.url));
const ENTRY = join(here, "..", "..", "dist", "main.js");

const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
mkdirSync(SCRATCH_ROOT, { recursive: true });
const scratch: string[] = [];

after(() => {
    for (const dir of scratch) {
        rmSync(dir, { recursive: true, force: true });
    }
});

function storage(): string {
    const dir = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-main-"));
    scratch.push(dir);
    return dir;
}

interface Started {
    child: ChildProcessWithoutNullStreams;
    output: () => string;
}

/** Spawn the entry point and resolve once it says the protocol WS is listening. */
async function start(args: string[], timeoutMs = 30_000): Promise<Started> {
    const child = spawn(process.execPath, [ENTRY, ...args], { stdio: "pipe" });
    let output = "";
    const started = new Promise<void>((resolve, reject) => {
        const timer = setTimeout(
            () => reject(new Error(`Bridge did not start in ${timeoutMs}ms. Output:\n${output}`)),
            timeoutMs,
        );
        const onData = (chunk: Buffer): void => {
            output += chunk.toString();
            if (output.includes("Protocol WebSocket listening")) {
                clearTimeout(timer);
                resolve();
            }
        };
        child.stdout.on("data", onData);
        child.stderr.on("data", onData);
        child.once("exit", code => {
            clearTimeout(timer);
            reject(new Error(`Bridge exited early with code ${code}. Output:\n${output}`));
        });
    });
    await started;
    return { child, output: () => output };
}

/** SIGTERM it and return the exit code launchd would see. */
async function stopAndWait(child: ChildProcessWithoutNullStreams, timeoutMs = 20_000): Promise<number | null> {
    const exited = new Promise<number | null>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`Bridge did not exit in ${timeoutMs}ms`)), timeoutMs);
        child.once("exit", code => {
            clearTimeout(timer);
            resolve(code);
        });
    });
    child.kill("SIGTERM");
    return exited;
}

/** Two ephemeral-ish ports derived from the PID, so parallel files do not clash. */
function ports(offset: number): string[] {
    const base = 41_000 + ((process.pid + offset * 37) % 4_000);
    return ["--matter-port", String(base), "--ws-port", String(base + 1)];
}

/** The protocol port {@link ports} handed the child, so a test can dial it. */
function wsPort(offset: number): number {
    return Number(ports(offset)[3]);
}

describe("main.ts as a process", { skip: existsSync(ENTRY) ? false : "run `npm run build` first" }, () => {
    it("exits 0 on SIGTERM, so launchd does not read a clean stop as a crash", async () => {
        const dir = storage();
        const { child, output } = await start(["--storage-path", dir, ...ports(1)]);

        const code = await stopAndWait(child);

        assert.equal(code, 0, `expected a clean exit. Output:\n${output()}`);
        assert.match(output(), /Shutdown complete/);
        // Not the escape hatch: that path is a forced exit(1), and telling it
        // apart from a genuine clean stop is the entire point of the number.
        assert.doesNotMatch(output(), /Shutdown stalled/);
    });

    it("writes an identity on first run and reuses it on the second", async () => {
        const dir = storage();
        const first = await start(["--storage-path", dir, ...ports(2)]);
        await stopAndWait(first.child);

        const identity = JSON.parse(readFileSync(join(dir, "identity.json"), "utf8"));
        assert.ok(identity.installId);

        const second = await start(["--storage-path", dir, ...ports(2)]);
        const reused = second.output();
        await stopAndWait(second.child);

        assert.match(reused, /Loaded bridge identity/);
        assert.deepEqual(JSON.parse(readFileSync(join(dir, "identity.json"), "utf8")), identity);
    });

    it("moves an unusable identity aside and mints NOTHING over it", async () => {
        // ⊗ The A3 regression, end to end. `identityProblem()` was called and
        // then `loadOrCreateIdentity()` ran unconditionally — and it writes
        // through `rename`, so the unreadable file (which carries the
        // SerialNumber and UniqueID every paired ecosystem knows this bridge
        // by, and which cannot be regenerated) was destroyed one line before
        // the refusal that exists to protect it.
        const dir = storage();
        writeFileSync(join(dir, "identity.json"), '{"installId": "truncated', "utf8");

        const { child, output } = await start(["--storage-path", dir, ...ports(3)]);
        await stopAndWait(child);

        assert.match(output(), /Bridge identity unusable/);
        assert.match(output(), /NOTHING has been written to identity\.json/);

        const files = readdirSync(dir).filter(name => name.startsWith("identity.json"));
        assert.deepEqual(
            files.filter(name => name === "identity.json"),
            [],
            "a replacement identity must NOT be written while the node is refusing",
        );
        const quarantined = files.find(name => name.startsWith("identity.json.unreadable-"));
        assert.ok(quarantined, `nothing was moved aside: ${files.join(", ")}`);
        assert.equal(readFileSync(join(dir, quarantined), "utf8"), '{"installId": "truncated');
    });

    it("refuses to serve endpoints over an unusable identity, and stays refusing", async () => {
        // ⊗ The whole refuse-to-start path for an unreadable `identity.json`,
        // end to end and over the wire, because nothing exercised it: the
        // hand-off from `main.ts` to the node, the node's refusal, §1.1's
        // recovery trio, and §3.11 declining to pretend it can help. Dropping
        // any one of them leaves a bridge that serves accessories under a
        // SerialNumber and UniqueID no paired ecosystem has ever seen.
        const dir = storage();
        writeFileSync(join(dir, "identity.json"), '{"installId": "truncated', "utf8");

        const { child, output } = await start(["--storage-path", dir, ...ports(4)]);
        const client = await TestClient.connect(wsPort(4));
        try {
            await client.next(); // §2 handshake

            const refused = await client.request({
                message_id: "e1",
                command: "attach",
                args: { protocolVersion: PROTOCOL_VERSION, pluginVersion: "test", endpoints: [] },
            });
            assert.equal(refused.error_code, ErrorCode.endpointMapInvalid, JSON.stringify(refused));
            assert.match(String(refused.details), /identity file is present but unreadable/);

            // §1.1: the recovery trio still answers, or the plugin has no way
            // to see the state it is in.
            for (const command of ["get_status", "get_pairing"] as const) {
                const response = await client.request({ message_id: `e-${command}`, command, args: {} });
                assert.ok(response.result !== undefined, `${command} was refused: ${JSON.stringify(response)}`);
            }

            // ...and the third one refuses on purpose. Rebuilding the map here
            // would clear the refusal without touching the identity, which is
            // the exact harm the refusal exists to prevent.
            const rebuild = await client.request({ message_id: "e2", command: "rebuild_endpoint_map", args: {} });
            assert.equal(rebuild.error_code, ErrorCode.endpointMapInvalid, JSON.stringify(rebuild));
            assert.match(String(rebuild.details), /cannot fix/);
        } finally {
            client.close();
            await stopAndWait(child);
        }

        assert.match(output(), /NOTHING has been written to identity\.json/);
        // The original bytes are the only surviving record of an identity that
        // cannot be regenerated, and exactly one copy of them must exist.
        const files = readdirSync(dir).filter(name => name.startsWith("identity.json"));
        assert.deepEqual(
            files.filter(name => name === "identity.json"),
            [],
            "a replacement identity must NOT be written while the node is refusing",
        );
        const quarantined = files.filter(name => name.startsWith("identity.json.unreadable-"));
        assert.equal(quarantined.length, 1, `expected exactly one quarantined identity, got ${files.join(", ")}`);
        assert.equal(readFileSync(join(dir, quarantined[0]!), "utf8"), '{"installId": "truncated');
    });

    it("keeps refusing on the NEXT start, when only the quarantine marker is left", async () => {
        // ⊗ The refusal used to last exactly one restart. Start one moves the
        // file aside; start two finds no identity.json at all, reads that as a
        // first run, and mints AND WRITES a replacement — so the bridge comes
        // up serving under a brand-new SerialNumber with nothing but the
        // routine "minting new bridge identity" line to show for it.
        const dir = storage();
        writeFileSync(join(dir, "identity.json"), "{ not json", "utf8");

        const first = await start(["--storage-path", dir, ...ports(5)]);
        await stopAndWait(first.child);

        const second = await start(["--storage-path", dir, ...ports(5)]);
        const restarted = second.output();
        await stopAndWait(second.child);

        assert.match(restarted, /Bridge identity unusable/, `the second start did not refuse:\n${restarted}`);
        assert.match(restarted, /identity\.json\.unreadable-/, "and it must name the file to restore");
        assert.doesNotMatch(restarted, /minting new bridge identity/);
        assert.ok(!existsSync(join(dir, "identity.json")), "still nothing written over the quarantined identity");
    });

    it("exits 0 on a clean stop AFTER a factory reset", async () => {
        // ⊗ `ServerNode.erase()` leaves a ref'd timer that `close()` never
        // clears, so this path — and only this path — used to sit until the
        // escape hatch fired and then exit 1. launchd runs the agent with
        // `KeepAlive { SuccessfulExit: false }`, so that number is the
        // difference between "stay stopped" and an immediate respawn. Both the
        // explicit `exit(0)` and the `clearTimeout` that disarms the hatch are
        // load-bearing here and nowhere else.
        const dir = storage();
        const { child, output } = await start(["--storage-path", dir, ...ports(6)]);
        const client = await TestClient.connect(wsPort(6));
        try {
            await client.next(); // §2 handshake
            await client.request({
                message_id: "s1",
                command: "attach",
                args: { protocolVersion: PROTOCOL_VERSION, pluginVersion: "test", endpoints: [] },
            });
            const reset = await client.request(
                { message_id: "s2", command: "factory_reset", args: { preserveEndpointNumbers: true } },
                30_000,
            );
            assert.deepEqual(reset.result, {}, JSON.stringify(reset));
        } finally {
            client.close();
        }

        const code = await stopAndWait(child);

        assert.equal(code, 0, `a clean stop after a reset must exit 0. Output:\n${output()}`);
        assert.match(output(), /Shutdown complete/);
        // The escape hatch is a forced exit(1); leaving it armed turns a clean
        // stop into a 10-second stall and then a lie to launchd.
        assert.doesNotMatch(output(), /Shutdown stalled/);
    });

    it("prints usage and exits 0 for --help", async () => {
        const child = spawn(process.execPath, [ENTRY, "--help"], { stdio: "pipe" });
        let out = "";
        child.stdout.on("data", chunk => {
            out += chunk.toString();
        });
        const code = await new Promise<number | null>(resolve => child.once("exit", resolve));
        assert.equal(code, 0);
        assert.match(out, /Usage: indigo-matter-bridge/);
    });

    it("exits 1 on an unparseable argument, so launchd retries nothing silently", async () => {
        const child = spawn(process.execPath, [ENTRY, "--ws-port", "not-a-port"], { stdio: "pipe" });
        const code = await new Promise<number | null>(resolve => child.once("exit", resolve));
        assert.equal(code, 1);
    });
});
