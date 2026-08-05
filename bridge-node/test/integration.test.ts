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
import {
    BridgeNode,
    HARDWARE_VERSION,
    HARDWARE_VERSION_STRING,
    matterJsVersion,
    PRODUCT_NAME,
    VENDOR_ID,
    VENDOR_NAME,
} from "../src/node.js";
import { PROTOCOL_VERSION } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";
import { golden } from "./stub-bridge.js";

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
            // E5: the attach ran the detector, so the empty `drift` beside this
            // IS an all-clear — the two endpoints were recorded against a fresh
            // map on a storage dir this test made a moment ago (§4.3).
            assert.deepEqual(status.drift, []);
            assert.equal(status.driftChecked, true, "§4.3: attach runs the drift check");
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
            const kitchen = [...aggregator.parts].find(part => part.id === endpointIdFor(KITCHEN));
            assert.ok(kitchen !== undefined, "the attach did not build the kitchen endpoint");

            // PRD §5.3 — every bridged child publishes the ROOT's identity.
            //
            // ⊗ This is the only test that can fail on `node.ts`'s
            // `bridgedIdentity`. `registry.test.ts` asserts a hand-written copy
            // of the same object literal, so the two agreed with each other and
            // with nothing else: publishing Apple's vendor id (0x1349) on every
            // bridged accessory left the whole suite green. Here the child is
            // read off a REAL ServerNode and compared with that node's own
            // BasicInformation, which is the property the docstring claims —
            // "an ecosystem is never shown two answers".
            const root = bridge.server.state.basicInformation as Record<string, unknown>;
            assert.equal(root.vendorName, VENDOR_NAME);
            assert.equal(Number(root.vendorId), VENDOR_ID);
            assert.equal(root.productName, PRODUCT_NAME);
            assert.equal(root.hardwareVersion, HARDWARE_VERSION);
            assert.equal(root.hardwareVersionString, HARDWARE_VERSION_STRING);
            assert.equal(root.softwareVersionString, BRIDGE_VERSION);
            for (const child of [kitchen, lounge]) {
                const info = child.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;
                for (const field of [
                    "vendorName",
                    "productName",
                    "hardwareVersion",
                    "hardwareVersionString",
                    "softwareVersion",
                    "softwareVersionString",
                ] as const) {
                    assert.equal(info[field], root[field], `${child.id}.${field} must equal the root's`);
                }
                // Branded on both sides; compare the numbers they carry.
                assert.equal(Number(info.vendorId), Number(root.vendorId), `${child.id}.vendorId`);
            }

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

            // §3.9 over an index that holds nothing — the ONE removal outcome a
            // never-commissioned node can reach, and the one that mattered:
            // ⊗ this used to answer `{}`, indistinguishable from a real removal,
            // and the plugin reported "that ecosystem has been unpaired. Every
            // accessory has been removed" over a node-side no-op. It also used
            // to return before `noteFabrics`, so the caller's stale list — which
            // is what its picker is built from — was never corrected.
            client.send({ message_id: "i4", command: "remove_fabric", args: { fabricIndex: 7 } });
            assert.deepEqual(
                await client.next(),
                { event: "fabrics_changed", data: { fabrics: [], change: "unchanged" } },
                "an already-gone index must still re-publish the fabric set",
            );
            assert.deepEqual(await client.next(), {
                message_id: "i4",
                result: { removed: false, remaining: 0 },
            });
        } finally {
            client.close();
            await server.close();
            await bridge.close();
        }
    });
});

describe("the whole §4.2 role table, from the golden frames", () => {
    it("attaches all 15 roles and applies every role's set_state on a real stack", async () => {
        // `attach_all_roles` and the `set_state_*` family are the shared §7
        // fixtures — the same bytes the Python suite asserts. Driving them
        // through a real node is what makes them a contract rather than two
        // independent transcriptions: E4's whole risk is a role factory that
        // builds but writes the wrong attribute, and only a live matter.js can
        // fail on that.
        //
        // The golden *responses* are not compared verbatim: they describe a
        // commissioned node with a paired Apple Home fabric and fixed endpoint
        // numbers, and this node is fresh. What a live node can promise — the
        // count, the roles, the ordering, and `{}` for every set_state — is
        // asserted instead.
        const bridge = new BridgeNode(
            { storagePath: mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-roles-")), matterPort: 0, wsPort: 0 },
            { installId: "integration0002", passcode: 20202021, discriminator: 3840 },
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
            await client.next(); // §2 handshake

            const request = golden.attach_all_roles.request;
            const expected = golden.attach_all_roles.response.result as {
                endpointCount: number;
                endpoints: { indigoDeviceId: number; role: string }[];
            };
            const attached = await client.request(request);
            assert.equal(attached.error_code, undefined, `attach failed: ${JSON.stringify(attached)}`);
            const status = attached.result as { endpointCount: number; endpoints: { indigoDeviceId: number; role: string }[] };

            assert.equal(status.endpointCount, expected.endpointCount);
            assert.deepEqual(
                status.endpoints.map(endpoint => [endpoint.indigoDeviceId, endpoint.role]),
                expected.endpoints.map(endpoint => [endpoint.indigoDeviceId, endpoint.role]),
            );

            // Every per-role set_state frame, verbatim, against the endpoints
            // the attach just built.
            const frames = Object.entries(golden as Record<string, unknown>).filter(
                ([name]) => name.startsWith("set_state_") && name !== "set_state_unknown_device" &&
                    name !== "set_state_bad_keys",
            ) as [string, { request: Record<string, unknown>; response: Record<string, unknown> }][];
            assert.ok(frames.length >= 14, `only ${frames.length} set_state frames were swept`);

            for (const [name, exchange] of frames) {
                const args = exchange.request.args as { indigoDeviceId: number };
                if (!expected.endpoints.some(endpoint => endpoint.indigoDeviceId === args.indigoDeviceId)) {
                    continue; // the plain `set_state` frame targets another fixture's device
                }
                assert.deepEqual(await client.request(exchange.request), exchange.response, name);
            }

            // §6.4: fifteen roles' worth of local writes, and not one echo.
            await assert.rejects(client.next(200), "a set_state echoed onto the wire as a command");
        } finally {
            client.close();
            await server.close();
            await bridge.close();
        }
    });
});
