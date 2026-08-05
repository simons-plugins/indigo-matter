/**
 * The three layers wired together: a real {@link BridgeNode} on a real Matter
 * stack, behind a real {@link BridgeWsServer}, talked to over a real socket.
 *
 * Every other suite cuts one of those seams — `protocol.test.ts` stubs the
 * bridge, `registry.test.ts` drives the registry directly with no protocol at
 * all — which leaves the wiring *between* them untested: that `node.ts` routes
 * its `#command` sink into the WebSocket server's `command` event at all, that
 * `attach` composes reconcile with `getStatus`, that a `set_state` arriving as
 * a wire frame reaches a matter.js attribute. Each of those is one line of glue
 * that no unit test can fail on.
 *
 * This file gets its own process (node's test runner forks per file), which is
 * what lets it use `Environment.default` — `BridgeNode.start` sets `storage.path`
 * on it, and matter.js takes an exclusive lock per storage path.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { type Endpoint, Logger } from "@matter/main";

import { endpointIdFor } from "../src/endpoints.js";
import { BridgeNode, matterJsVersion } from "../src/node.js";
import { PROTOCOL_VERSION } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";

const BRIDGE_VERSION = "0.1.0-test";
const KITCHEN = 123456789;
const LOUNGE = 123456790;

Logger.level = "fatal";

const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
mkdirSync(SCRATCH_ROOT, { recursive: true });
const storagePath = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-integration-"));

after(() => rmSync(storagePath, { recursive: true, force: true }));

/** A matter.js `$Changed` observable, as much of it as this test drives. */
interface ChangeObservable {
    emit(value: unknown, oldValue: unknown, context: unknown): void;
}

/**
 * Stand in for a controller writing an attribute — the same lever
 * `registry.test.ts` uses, and for the same reason: a genuine remote write
 * needs a commissioned fabric and a session, which is a great deal of
 * machinery to prove that a non-offline context reaches our handler.
 */
function ecosystemWrite(endpoint: Endpoint, behavior: string, attribute: string, value: unknown, previous: unknown) {
    const events = endpoint.eventsOf(behavior) as unknown as Record<string, ChangeObservable | undefined>;
    const observable = events[`${attribute}$Changed`];
    assert.ok(observable !== undefined, `${behavior}.${attribute}$Changed does not exist`);
    observable.emit(value, previous, { offline: false });
}

describe("plugin ⇄ node, end to end", () => {
    it("attaches, applies a set_state, and reports an ecosystem write on the wire", async () => {
        const bridge = new BridgeNode(
            // Port 0 on both: matter.js and `ws` each bind an ephemeral port, so
            // a parallel run cannot collide with this one.
            { storagePath, matterPort: 0, wsPort: 0 },
            { installId: "integration0001", passcode: 20202021, discriminator: 3840 },
            BRIDGE_VERSION,
            () => {},
        );
        await bridge.start();

        const server = new BridgeWsServer({
            port: 0,
            bridge,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion,
            log: () => {},
        });
        await server.listen();

        const client = await TestClient.connect(server.port);
        try {
            // §2: the bare handshake, from the real node's real versions.
            const handshake = await client.next();
            assert.equal(handshake.protocolVersion, PROTOCOL_VERSION);
            assert.equal(handshake.bridgeVersion, BRIDGE_VERSION);
            assert.equal(handshake.matterJsVersion, matterJsVersion);

            // §3.1 — attach is a full reconcile whose result is a StatusReport.
            const attached = await client.request({
                message_id: "i1",
                command: "attach",
                args: {
                    protocolVersion: PROTOCOL_VERSION,
                    pluginVersion: "2026.8.1",
                    endpoints: [
                        {
                            indigoDeviceId: KITCHEN,
                            role: "onOffLight",
                            label: "Kitchen Lamp",
                            reachable: true,
                            states: { onOff: false },
                            options: {},
                        },
                        {
                            indigoDeviceId: LOUNGE,
                            role: "dimmableLight",
                            label: "Lounge Lamp",
                            reachable: true,
                            states: { onOff: true, level: 60 },
                            options: {},
                        },
                    ],
                },
            });
            const status = attached.result as Record<string, unknown>;
            assert.equal(status.endpointCount, 2);
            assert.equal(status.driftChecked, false, "§4.3: an empty drift is not an all-clear");
            assert.deepEqual(
                (status.endpoints as { indigoDeviceId: number }[]).map(endpoint => endpoint.indigoDeviceId),
                [KITCHEN, LOUNGE],
            );

            // §6.2: attach answers exactly what get_status would.
            const queried = await client.request({ message_id: "i2", command: "get_status", args: {} });
            assert.deepEqual(queried.result, status);

            // §3.4 — a wire frame reaching a matter.js attribute.
            assert.deepEqual(
                await client.request({
                    message_id: "i3",
                    command: "set_state",
                    args: { indigoDeviceId: LOUNGE, states: { level: 100 } },
                }),
                { message_id: "i3", result: {} },
            );

            const aggregator = [...bridge.server.parts].find(part => part.id === "aggregator");
            assert.ok(aggregator !== undefined);
            const lounge = [...aggregator.parts].find(part => part.id === endpointIdFor(LOUNGE));
            assert.ok(lounge !== undefined, "the attach did not build the lounge endpoint");
            assert.equal((lounge.stateOf("levelControl") as Record<string, unknown>).currentLevel, 254);

            // §6.4: our own write must not come back as a command.
            await assert.rejects(client.next(200), "a local set_state echoed onto the wire");

            // §5 — an ecosystem acting on the endpoint arrives as a `command`
            // frame. This is the whole point of the file: the path runs through
            // watchCommands → registry emit → BridgeNode#command → sendEvent.
            ecosystemWrite(lounge, "levelControl", "currentLevel", 152, 254);
            assert.deepEqual(await client.next(), {
                event: "command",
                data: { indigoDeviceId: LOUNGE, command: "setLevel", args: { level: 60 } },
            });

            ecosystemWrite(lounge, "onOff", "onOff", false, true);
            assert.deepEqual(await client.next(), {
                event: "command",
                data: { indigoDeviceId: LOUNGE, command: "onOff", args: { value: false } },
            });
        } finally {
            client.close();
            await server.close();
            await bridge.close();
        }
    });
});
