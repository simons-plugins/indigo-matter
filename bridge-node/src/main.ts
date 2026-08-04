#!/usr/bin/env node
/**
 * indigo-matter bridge node entry point.
 *
 * Wires the three pieces together — persisted identity, the Matter ServerNode,
 * and the loopback protocol server — and shuts them down cleanly on SIGTERM so
 * launchd restarts are not mistaken for crashes.
 */

import { createRequire } from "node:module";

import { parseArgs, USAGE } from "./config.js";
import { BridgeNode, matterJsVersion } from "./node.js";
import { loadOrCreateIdentity } from "./storage.js";
import { BridgeWsServer } from "./ws-server.js";

const bridgeVersion: string = createRequire(import.meta.url)("../package.json").version;

function log(message: string): void {
    // stdout; launchd captures it into the plugin's log file.
    console.log(`[bridge] ${new Date().toISOString()} ${message}`);
}

async function main(): Promise<void> {
    const parsed = parseArgs(process.argv.slice(2));
    if (parsed === "help") {
        process.stdout.write(USAGE);
        return;
    }

    log(`indigo-matter-bridge ${bridgeVersion} (matter.js ${matterJsVersion}, node ${process.version})`);

    const identity = loadOrCreateIdentity(parsed.storagePath);
    const bridge = new BridgeNode(parsed, identity, bridgeVersion, log);
    await bridge.start();

    const ws = new BridgeWsServer({
        port: parsed.wsPort,
        bridge,
        bridgeVersion,
        matterJsVersion,
        log,
    });
    await ws.listen();

    let shuttingDown = false;
    const shutdown = (signal: string): void => {
        if (shuttingDown) {
            return;
        }
        shuttingDown = true;
        log(`Received ${signal}, shutting down`);
        void (async () => {
            try {
                await ws.close();
                await bridge.close();
            } catch (error) {
                log(`Error during shutdown: ${error instanceof Error ? error.message : String(error)}`);
            }
            // No process.exit here: matter.js's ProcessManager also handles the
            // signal and needs to release its storage lock. Exiting eagerly
            // orphans that lock. With both servers closed the loop drains on its
            // own; the unref'd timer only fires if something else holds it open.
            const escapeHatch = setTimeout(() => {
                log("Shutdown did not drain the event loop; exiting");
                process.exit(0);
            }, 10_000);
            escapeHatch.unref();
        })();
    };
    process.on("SIGTERM", () => shutdown("SIGTERM"));
    process.on("SIGINT", () => shutdown("SIGINT"));
}

main().catch((error: unknown) => {
    console.error(`[bridge] fatal: ${error instanceof Error ? (error.stack ?? error.message) : String(error)}`);
    process.exit(1);
});
