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
import { describeError, describeErrorWithStack } from "./protocol.js";
import { loadOrCreateIdentity } from "./storage.js";
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

    const identity = await phase("identity load failed", () => loadOrCreateIdentity(config.storagePath, log));

    const bridge = new BridgeNode(config, identity, bridgeVersion, log);
    await phase(`Matter node start failed (matter port ${config.matterPort}, storage ${config.storagePath})`, () =>
        bridge.start(),
    );

    const ws = new BridgeWsServer({
        port: config.wsPort,
        bridge,
        bridgeVersion,
        matterJsVersion,
        log,
    });
    await phase(`protocol WS listen failed (port ${config.wsPort})`, () => ws.listen());

    let shuttingDown = false;
    const shutdown = (signal: string): void => {
        if (shuttingDown) {
            return;
        }
        shuttingDown = true;
        log(`Received ${signal}, shutting down`);

        // Armed before the awaits, so a close that never resolves is still
        // caught. Unref'd: it must not itself hold the loop open. A forced exit
        // is not a clean shutdown, hence exit code 1 — launchd should see the
        // difference. BridgeNode.start() sets `runtime.signals: false`, so
        // matter.js's ProcessManager is not racing us for these signals.
        let pending = "protocol WS close";
        const escapeHatch = setTimeout(() => {
            log(`Shutdown stalled at: ${pending}; forcing exit`);
            process.exit(1);
        }, SHUTDOWN_ESCAPE_MS);
        escapeHatch.unref();

        void (async () => {
            // Separate try blocks: a failing WS close must not skip the Matter
            // close, which is what releases the storage lock.
            try {
                await ws.close();
            } catch (error) {
                log(`Error closing protocol WS: ${describeErrorWithStack(error)}`);
            }
            pending = "Matter node close";
            try {
                await bridge.close();
            } catch (error) {
                log(`Error closing Matter node: ${describeErrorWithStack(error)}`);
            }
            pending = "event loop drain";
            log("Shutdown complete");
        })();
    };
    process.on("SIGTERM", () => shutdown("SIGTERM"));
    process.on("SIGINT", () => shutdown("SIGINT"));
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
    log(`Fatal: ${describeErrorWithStack(error)}`);
    // Secondary copy on stderr, for the case where stdout is the thing that broke.
    console.error(`[bridge] fatal: ${describeErrorWithStack(error)}`);
    process.exit(1);
});
