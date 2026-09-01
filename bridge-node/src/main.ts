#!/usr/bin/env node
/**
 * indigo-matter bridge node entry point.
 *
 * Wires the three pieces together — persisted identity, the Matter ServerNode,
 * and the loopback protocol server — and shuts them down cleanly on SIGTERM so
 * launchd restarts are not mistaken for crashes.
 */

import { createRequire } from "node:module";
import { networkInterfaces } from "node:os";

import { assertMdnsInterface, parseArgs, USAGE } from "./config.js";
import { BridgeNode, matterJsVersion } from "./node.js";
import { describeError, describeErrorWithStack, RefuseReason } from "./protocol.js";
import {
    type BridgeIdentity,
    identityProblem,
    loadOrCreateIdentity,
    mintIdentity,
    quarantineIdentity,
} from "./storage.js";
import { BridgeWsServer } from "./ws-server.js";

const bridgeVersion: string = createRequire(import.meta.url)("../package.json").version;

/** Give a stalled shutdown this long before we stop being polite about it. */
const SHUTDOWN_ESCAPE_MS = 10_000;

function log(message: string): void {
    // stdout; launchd captures it into the plugin's log file.
    console.log(`[bridge] ${new Date().toISOString()} ${message}`);
}

/**
 * Label each startup step so a launchd log shows *which* one failed. Without
 * this, "Error: listen EADDRINUSE" could equally be the Matter port or the
 * protocol port.
 */
async function phase<T>(name: string, run: () => Promise<T> | T): Promise<T> {
    try {
        return await run();
    } catch (error) {
        log(`Startup failed — ${name}: ${describeErrorWithStack(error)}`);
        throw error;
    }
}

/**
 * What is up and must come down. `shutdown` walks this in REVERSE, so each
 * entry is registered at the point the thing it names becomes real.
 *
 * A registry rather than two `let`s because the registration *point* is the
 * load-bearing part — see the two call sites in {@link main}, which register
 * for opposite-looking reasons — and two variables encoded that only in
 * prose, plus a second time, silently, in the escape hatch's `pending` label.
 */
const closers: Array<{ what: string; close: () => Promise<void> }> = [];
let shuttingDown = false;

const shutdown = (signal: string): void => {
    if (shuttingDown) {
        return;
    }
    shuttingDown = true;
    log(`Received ${signal}, shutting down`);

    // Armed before the awaits, so a close that never resolves is still caught.
    // Unref'd: it must not itself hold the loop open. A forced exit is not a
    // clean shutdown, hence exit code 1 — launchd should see the difference.
    let pending = "shutdown start";
    const escapeHatch = setTimeout(() => {
        log(`Shutdown stalled at: ${pending}; forcing exit`);
        process.exit(1);
    }, SHUTDOWN_ESCAPE_MS);
    escapeHatch.unref();

    void (async () => {
        const closed: string[] = [];
        const failed: string[] = [];
        // Drained, not snapshotted. `main()` keeps running while this awaits —
        // a stop during the Matter start lets startup finish, and the protocol
        // WS registers *after* this loop began. A `[...closers].reverse()` copy
        // taken up front misses it and leaves a bound socket unclosed; popping
        // picks up whatever startup added in the meantime and still closes
        // innermost-first.
        //
        // One try per entry: a failing close must not skip the next, which is
        // what releases the storage lock.
        while (closers.length > 0) {
            const entry = closers.pop() as { what: string; close: () => Promise<void> };
            pending = `${entry.what} close`;
            try {
                await entry.close();
                closed.push(entry.what);
            } catch (error) {
                failed.push(entry.what);
                log(`Error closing ${entry.what}: ${describeErrorWithStack(error)}`);
            }
        }
        clearTimeout(escapeHatch);
        // Say what actually came down. A bare "Shutdown complete" certified
        // three different outcomes — everything closed, NOTHING closed because
        // the stop beat startup, and every close throwing — and nothing outside
        // the process could tell them apart, which is exactly the shape the
        // workspace's degradation-path rule warns about.
        const detail = closed.length > 0 ? `closed: ${closed.join(", ")}` : "closed: nothing was up yet";
        log(`Shutdown complete (${detail}${failed.length > 0 ? `; FAILED: ${failed.join(", ")}` : ""})`);
        // Everything that is ours is down, and what is left on the loop is not
        // ours to wait for. matter.js 0.17.8's `ServerNode.erase()` (§3.10)
        // leaves a ref'd timer behind that `close()` does not clear, measured
        // at 0.17.8: without this, a perfectly clean shutdown *after a factory
        // reset* would sit until the escape hatch fired and then exit 1,
        // telling launchd a successful stop was a crash. Exit 0 here; the
        // escape hatch still owns every path where a close does NOT return.
        process.exit(0);
    })();
};

// Installed at module scope, so they are in place before `main()` below runs a
// single line of startup. #328: these used to be the LAST statement of
// `main()`, which left the identity read, `bridge.start()` and `ws.listen()`
// running on node's default SIGTERM disposition — death by signal, exit code
// `null`, no `close()` of anything. The narrow end of that window was an
// intermittent CI failure (readiness is logged inside `ws.listen()`, a few
// ticks before the old `process.on` was reached); the wide end was every
// millisecond of startup, ~90ms of it measured on an idle Mac.
//
// Registering this early is safe, but NOT because matter.js has no handlers of
// its own: its `ProcessManager` installs SIGINT/SIGTERM/SIGABRT handlers when
// its runtime starts, gated on `runtime.signals`, which DEFAULTS TO TRUE. It
// installs none here only because `BridgeNode.start()` sets that var false
// (node.ts). Delete that line and this comment stops being true.
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

async function main(): Promise<void> {
    let parsed;
    try {
        parsed = parseArgs(process.argv.slice(2));
    } catch (error) {
        log(`Invalid arguments: ${describeError(error)}`);
        process.stderr.write(USAGE);
        process.exit(1);
    }
    if (parsed === "help") {
        process.stdout.write(USAGE);
        return;
    }
    const config = parsed;

    log(`indigo-matter-bridge ${bridgeVersion} (matter.js ${matterJsVersion}, node ${process.version})`);

    if (config.mdnsInterface !== undefined) {
        await phase("mDNS interface check", () =>
            assertMdnsInterface(config.mdnsInterface!, Object.keys(networkInterfaces())),
        );
    }

    // Asked BEFORE the identity is loaded, because loading it is what would
    // replace an unusable one — and replacing it mints a new `SerialNumber` and
    // `UniqueID`, which every paired ecosystem reads as a different accessory
    // (E5 / PRD §4.3). A missing file is a first run and answers `undefined`.
    const identityFault = await phase("identity check failed", () => identityProblem(config.storagePath));

    // The two answers take opposite paths, and running the mint unconditionally
    // — as the first cut of E5 did — destroyed the file the refusal below exists
    // to protect, one line before refusing to protect it. `loadOrCreateIdentity`
    // writes through `rename`, so the unusable original was gone by the time
    // anybody was told there had been a problem.
    let identity: BridgeIdentity;
    if (identityFault === undefined) {
        identity = await phase("identity load failed", () => loadOrCreateIdentity(config.storagePath, log));
    } else {
        log(`Bridge identity unusable: ${identityFault}`);
        const movedTo = quarantineIdentity(config.storagePath, log);
        if (movedTo !== undefined) {
            log(`Moved the unusable identity to ${movedTo} — repair or restore it, then restart`);
        }
        // In memory only. The node needs *an* identity to build a ServerNode at
        // all (that is what keeps `get_pairing` answering, §1.1), but nothing
        // durable may be written over an identity we could not read: no
        // endpoints are served while refusing, so this one is never anybody's
        // accessory and must not outlive the process.
        identity = mintIdentity();
        log("Using a temporary in-memory bridge identity; NOTHING has been written to identity.json");
    }

    const node = new BridgeNode(
        config,
        identity,
        bridgeVersion,
        log,
        identityFault === undefined ? undefined : RefuseReason.identityUnreadable,
    );
    // Registered BEFORE `start()` resolves, because `start()` returns long
    // after the node is live: `server.start()` brings it online — mDNS
    // advertising, accepting sessions, storage lock held — and only then does
    // `start()` wire churn detection, session hygiene and the endpoint-identity
    // assertion (node.ts). Registering on the RETURN would walk a stop straight
    // past a running node, and the log would be indistinguishable from a stop
    // that arrived before anything existed.
    //
    // The closer sequences itself behind the in-flight start rather than racing
    // it: `#server` is assigned before `server.start()` (node.ts), so closing
    // mid-start would race matter.js's own bring-up. The escape hatch bounds
    // how long that wait can take — a start wedged past it is a forced exit 1,
    // which for a wedged bridge is the right answer.
    const starting = phase(
        `Matter node start failed (matter port ${config.matterPort}, storage ${config.storagePath})`,
        () => node.start(),
    );
    closers.push({
        what: "Matter node",
        close: async () => {
            // A start that FAILED left nothing of ours up; its error is
            // `main()`'s to report, not this closer's to rethrow.
            await starting.catch(() => undefined);
            await node.close();
        },
    });
    await starting;

    const server = new BridgeWsServer({
        port: config.wsPort,
        bridge: node,
        bridgeVersion,
        matterJsVersion,
        log,
    });
    // Registered before `listen()`, and unlike the node above no sequencing is
    // needed: `close()` reads its own `#wss` handle (ws-server.ts), which
    // `listen()` assigns synchronously, so a close is either a guarded no-op or
    // an ordinary close of a live server. The CI flake's own window is the
    // latter — bound and accepting, readiness already logged, `listen()` not
    // yet returned to us.
    closers.push({ what: "protocol WS", close: () => server.close() });
    await phase(`protocol WS listen failed (port ${config.wsPort})`, () => server.listen());
}

// A crash must reach the launchd-captured stdout log with its stack, then exit
// non-zero so launchd restarts us. Silent survival on a broken invariant is the
// worse outcome: the plugin would see a live socket and a dead bridge.
process.on("uncaughtException", (error: unknown) => {
    log(`Uncaught exception: ${describeErrorWithStack(error)}`);
    process.exit(1);
});
process.on("unhandledRejection", (reason: unknown) => {
    log(`Unhandled rejection: ${describeErrorWithStack(reason)}`);
    process.exit(1);
});

main().catch((error: unknown) => {
    // A stop that arrives mid-startup abandons `main()` where it stands, and a
    // step interrupted that way can reject on its way out. That rejection is a
    // consequence of the shutdown, not a startup failure, and letting it exit 1
    // here would hand launchd the crash code for what was a clean stop.
    //
    // It must still be RECORDED. The window is reachable with a real fault in
    // it — a SIGTERM landing while `listen()` rejects EADDRINUSE — and the
    // `console.error` below is this program's ONLY writer to launchd's
    // StandardErrorPath, which the plugin greps for exactly that marker
    // (`_err_log_mentions_port_conflict`, launch_agent.py). Returning silently
    // would hide the one startup fault the plugin knows how to diagnose.
    if (shuttingDown) {
        log(`Startup abandoned by shutdown: ${describeErrorWithStack(error)}`);
        console.error(`[bridge] startup abandoned by shutdown: ${describeErrorWithStack(error)}`);
        return;
    }
    log(`Fatal: ${describeErrorWithStack(error)}`);
    // Secondary copy on stderr, for the case where stdout is the thing that broke.
    console.error(`[bridge] fatal: ${describeErrorWithStack(error)}`);
    process.exit(1);
});
