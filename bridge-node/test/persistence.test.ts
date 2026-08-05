/**
 * E5 / XAC5 against a real Matter stack: endpoint identity survives a restart,
 * its loss is loud, and the documented way back works over the wire.
 *
 * `endpoint-map.test.ts` proves the detector in isolation; this file proves the
 * numbers it is fed are the ones matter.js actually assigns, and that the
 * refuse-to-start state a corrupted map produces really does stop endpoints
 * being created. Neither is provable without the stack.
 *
 * Its own file because node's test runner forks per file and matter.js takes an
 * exclusive lock per storage path — the "restart" tests here rebuild a
 * `ServerNode` on the *same* path, which only works once the previous one has
 * fully closed, and which would fight `integration.test.ts` for
 * `Environment.default` if they shared a process.
 */

import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { Logger } from "@matter/main";

import { ENDPOINT_MAP_FILE, type EndpointMapFile } from "../src/endpoint-map.js";
import { uniqueIdFor } from "../src/endpoints.js";
import { BridgeNode, matterJsVersion } from "../src/node.js";
import { ErrorCode, PROTOCOL_VERSION, RefuseReason } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";

const BRIDGE_VERSION = "0.1.0-test";
const KITCHEN = 123456789;
const LOUNGE = 123456790;

Logger.level = "fatal";

const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
mkdirSync(SCRATCH_ROOT, { recursive: true });
const scratch: string[] = [];

after(() => {
    for (const dir of scratch) {
        rmSync(dir, { recursive: true, force: true });
    }
});

function storage(): string {
    const dir = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-persistence-"));
    scratch.push(dir);
    return dir;
}

const IDENTITY = { installId: "persistence00001", passcode: 20202021, discriminator: 3840 };

/** §3.10 takes the Matter stack down and back up; 2s is not enough for that. */
const RESET_TIMEOUT_MS = 20_000;

const ENDPOINTS = [
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
];

interface Session {
    bridge: BridgeNode;
    client: TestClient;
    close: () => Promise<void>;
}

/** Start a real node + protocol server on `storagePath` and connect to it. */
async function boot(storagePath: string, commissionedAt?: string): Promise<Session> {
    const bridge = new BridgeNode(
        // Ephemeral on both ports, so a parallel run cannot collide.
        { storagePath, matterPort: 0, wsPort: 0 },
        commissionedAt === undefined ? { ...IDENTITY } : { ...IDENTITY, commissionedAt },
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
    await client.next(); // handshake
    return {
        bridge,
        client,
        close: async () => {
            client.close();
            await server.close();
            await bridge.close();
        },
    };
}

/**
 * `attach`, tolerant of the frame ordering the drift path produces.
 *
 * `handleAttach` marks the socket attached *before* it reconciles, so a
 * `drift_detected` raised by that reconcile is written ahead of the attach
 * response. That is lawful — §1's ordering guarantee is about requests, and the
 * plugin's own handshake pump dispatches an interleaved event and keeps waiting
 * — but a test that assumed "the next frame is my answer" would read the event
 * as the response and fail with something unrelated to what it is checking.
 */
async function attach(
    client: TestClient,
    messageId: string,
): Promise<{ status: Record<string, unknown>; events: Record<string, unknown>[] }> {
    client.send({
        message_id: messageId,
        command: "attach",
        args: { protocolVersion: PROTOCOL_VERSION, pluginVersion: "2026.7.30", endpoints: ENDPOINTS },
    });
    const events: Record<string, unknown>[] = [];
    for (;;) {
        const frame = await client.next(5000);
        if (frame.message_id === messageId) {
            return { status: frame, events };
        }
        events.push(frame);
    }
}

function readMap(storagePath: string): EndpointMapFile {
    return JSON.parse(readFileSync(join(storagePath, ENDPOINT_MAP_FILE), "utf8")) as EndpointMapFile;
}

function driftOf(status: Record<string, unknown>): unknown[] {
    return (status.result as { drift: unknown[] }).drift;
}

function numbersOf(status: Record<string, unknown>): Record<number, number> {
    const endpoints = (status.result as { endpoints: { indigoDeviceId: number; endpointNumber: number }[] })
        .endpoints;
    return Object.fromEntries(endpoints.map(endpoint => [endpoint.indigoDeviceId, endpoint.endpointNumber]));
}

describe("XAC5: endpoint identity across a bridge-node restart", () => {
    it("records the numbers matter.js assigned, and reports the same ones after a restart", async () => {
        const storagePath = storage();

        const first = await boot(storagePath);
        const before = numbersOf((await attach(first.client, "p1")).status);
        // The map is written by the reconcile, not by shutdown: a node that is
        // killed (or whose Mac loses power) between the two must still have a
        // baseline for the next start.
        const persisted = readMap(storagePath);
        await first.close();

        assert.deepEqual(persisted.endpoints, {
            [uniqueIdFor(KITCHEN)]: before[KITCHEN],
            [uniqueIdFor(LOUNGE)]: before[LOUNGE],
        });

        const second = await boot(storagePath);
        const restarted = await attach(second.client, "p2");
        await second.close();

        assert.deepEqual(numbersOf(restarted.status), before, "XAC5: numbers must survive a restart");
        assert.deepEqual(driftOf(restarted.status), []);
        assert.deepEqual(restarted.events, [], "a clean restart must raise no drift event");
        assert.equal((restarted.status.result as { driftChecked: boolean }).driftChecked, true);
    });

    it("reports drift when the recorded number no longer matches, and never repairs it", async () => {
        const storagePath = storage();
        const first = await boot(storagePath);
        const before = numbersOf((await attach(first.client, "p3")).status);
        await first.close();

        // Stand in for the one failure this whole milestone exists for: the
        // persisted allocation and the live one disagreeing. Poisoning the map
        // is the only way to produce it deterministically — the real cause is
        // matter.js's storage being lost, which is exactly what we cannot ask a
        // test to arrange without also losing the fabric.
        const poisoned: EndpointMapFile = {
            version: 1,
            endpoints: { ...readMap(storagePath).endpoints, [uniqueIdFor(KITCHEN)]: 99 },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(poisoned));

        const second = await boot(storagePath);
        // §5: the drift event reaches the attached client, not just the report.
        const { status, events } = await attach(second.client, "p4");
        const afterMap = readMap(storagePath);
        await second.close();

        assert.deepEqual(driftOf(status), [
            { uniqueId: uniqueIdFor(KITCHEN), expected: 99, actual: before[KITCHEN] },
        ]);
        assert.equal(events.length, 1, JSON.stringify(events));
        assert.equal(events[0]?.event, "drift_detected");
        assert.deepEqual((events[0]?.data as { drift: unknown[] }).drift, driftOf(status));
        // §4.3: never auto-repaired. If the baseline moved to match, the next
        // start would call the same fault clean.
        assert.equal(afterMap.endpoints[uniqueIdFor(KITCHEN)], 99);
    });

    it("stops reporting drift once the user rebuilds the map (§3.11)", async () => {
        const storagePath = storage();
        const first = await boot(storagePath);
        const before = numbersOf((await attach(first.client, "p5")).status);
        await first.close();

        writeFileSync(
            join(storagePath, ENDPOINT_MAP_FILE),
            JSON.stringify({ version: 1, endpoints: { [uniqueIdFor(KITCHEN)]: 99 } }),
        );

        const second = await boot(storagePath);
        const drifted = await attach(second.client, "p6");
        assert.equal(drifted.events.length, 1, "the poisoned map must have been reported");
        const rebuilt = await second.client.request({
            message_id: "p7",
            command: "rebuild_endpoint_map",
            args: {},
        });
        const status = await second.client.request({ message_id: "p8", command: "get_status", args: {} });
        await second.close();

        assert.deepEqual(driftOf(rebuilt), []);
        assert.equal((status.result as { driftChecked: boolean }).driftChecked, true);
        assert.deepEqual(driftOf(status), []);
        assert.deepEqual(readMap(storagePath).endpoints, {
            [uniqueIdFor(KITCHEN)]: before[KITCHEN],
            [uniqueIdFor(LOUNGE)]: before[LOUNGE],
        });
    });
});

describe("factory_reset (§3.10) and the endpoint map", () => {
    it("keeps endpoint-map.json by default — that is why it lives outside matter.js storage", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        await attach(session.client, "f1");
        const before = readMap(storagePath);

        const reset = await session.client.request({
            message_id: "f2",
            command: "factory_reset",
            args: { preserveEndpointNumbers: true },
        }, RESET_TIMEOUT_MS);
        const after = readMap(storagePath);
        await session.close();

        assert.deepEqual(reset.result, {});
        // `ServerNode.erase()` wipes matter.js's own storage context. The map
        // surviving it is the entire reason the file is a sibling of it rather
        // than a member.
        assert.deepEqual(after.endpoints, before.endpoints);
    });

    it("deletes it on preserveEndpointNumbers: false — PRD §7's explicit rebuild", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        await attach(session.client, "f3");
        assert.ok(existsSync(join(storagePath, ENDPOINT_MAP_FILE)));

        await session.client.request({
            message_id: "f4",
            command: "factory_reset",
            args: { preserveEndpointNumbers: false },
        }, RESET_TIMEOUT_MS);
        const gone = !existsSync(join(storagePath, ENDPOINT_MAP_FILE));
        await session.close();

        assert.ok(gone, "the map must go when the map is what was corrupt");
    });

    it("clears the commissioning witness, so the reset does not refuse to itself", async () => {
        // Without this the very next start would see witness-says-paired and
        // stack-says-no-fabrics — the fabric-storage-lost signature — and refuse
        // to serve anything, caused by the reset the user had just asked for.
        const storagePath = storage();
        const session = await boot(storagePath, "2026-08-01T00:00:00.000Z");
        // It refuses at first, exactly as it should with that witness and no fabrics.
        assert.equal(session.bridge.endpointMapRefusal(), RefuseReason.fabricStorageLost);
        await session.client.request({ message_id: "f5", command: "rebuild_endpoint_map", args: {} });

        await session.client.request({
            message_id: "f6",
            command: "factory_reset",
            args: { preserveEndpointNumbers: true },
        }, RESET_TIMEOUT_MS);
        await session.close();

        const identity = JSON.parse(readFileSync(join(storagePath, "identity.json"), "utf8"));
        assert.equal(identity.commissionedAt, undefined);
        const restarted = await boot(storagePath);
        const refusal = restarted.bridge.endpointMapRefusal();
        await restarted.close();
        assert.equal(refusal, undefined);
    });

    it("leaves a fresh commissionable pairing state behind (§3.10's whole point)", async () => {
        // The user-visible outcome, and nothing asserted it: the reset exists so
        // the bridge can be paired again, and a node that wiped its credentials
        // but stopped advertising would have "succeeded" identically.
        const storagePath = storage();
        const session = await boot(storagePath);
        await attach(session.client, "f7");

        await session.client.request({
            message_id: "f8",
            command: "factory_reset",
            args: { preserveEndpointNumbers: true },
        }, RESET_TIMEOUT_MS);
        const pairing = await session.client.request({ message_id: "f9", command: "get_pairing", args: {} });
        await session.close();

        const report = pairing.result as {
            commissioned: boolean;
            windowOpen: boolean;
            manualPairingCode: string | null;
            qrPairingCode: string | null;
        };
        assert.equal(report.commissioned, false);
        assert.equal(report.windowOpen, true, "a reset bridge must be advertising again");
        assert.ok(report.manualPairingCode, "and it must hand back a usable code");
        assert.match(String(report.qrPairingCode), /^MT:/);
    });

    it("is REFUSED while the node is refusing — §3.11 is the exit, not this", async () => {
        // The design call behind RECOVERY_COMMANDS, pinned. `factory_reset` is
        // arguably an exit from a corrupt map, but it is the destructive one:
        // it drops every ecosystem pairing, which §3.11 does not, and §3.11
        // already exits every refusal state. A user staring at a scary error
        // must not be offered the bigger hammer as an alternative to the
        // smaller one that works.
        const storagePath = storage();
        const session = await boot(storagePath, "2026-08-01T00:00:00.000Z");
        assert.equal(session.bridge.endpointMapRefusal(), RefuseReason.fabricStorageLost);

        const refused = await session.client.request({
            message_id: "f10",
            command: "factory_reset",
            args: { preserveEndpointNumbers: true },
        }, RESET_TIMEOUT_MS);

        assert.equal(refused.error_code, ErrorCode.endpointMapInvalid);
        assert.equal(session.bridge.endpointMapRefusal(), RefuseReason.fabricStorageLost);

        // And the documented sequence — rebuild, attach, then reset — works.
        await session.client.request({ message_id: "f10a", command: "rebuild_endpoint_map", args: {} });
        await attach(session.client, "f10c");
        const reset = await session.client.request({
            message_id: "f10b",
            command: "factory_reset",
            args: { preserveEndpointNumbers: true },
        }, RESET_TIMEOUT_MS);
        await session.close();
        assert.deepEqual(reset.result, {});
    });

    it("verifies the witness is really gone before reporting completion", async () => {
        const storagePath = storage();
        const session = await boot(storagePath, "2026-08-01T00:00:00.000Z");
        await session.client.request({ message_id: "f11", command: "rebuild_endpoint_map", args: {} });
        await attach(session.client, "f11a");

        await session.client.request({
            message_id: "f12",
            command: "factory_reset",
            args: { preserveEndpointNumbers: true },
        }, RESET_TIMEOUT_MS);
        const status = await session.client.request({ message_id: "f13", command: "get_status", args: {} });
        await session.close();

        // §4.3 `warnings`: a witness that survived the erase is the exact
        // fabricStorageLost signature, so it must be reported, not assumed.
        assert.deepEqual((status.result as { warnings: string[] }).warnings, []);
        assert.equal(
            JSON.parse(readFileSync(join(storagePath, "identity.json"), "utf8")).commissionedAt,
            undefined,
        );
    });
});

describe("the drift check runs on every path that can move a number", () => {
    it("runs after a reconcile that failed part-way through", async () => {
        // The `finally` is load-bearing and nothing held it there: moving the
        // check onto the success path broke no test at all. A part-applied
        // reconcile (registry.ts is explicit that there is no transaction
        // across several adds) has still CREATED endpoints, and those are
        // exactly the numbers most worth checking — skipping the check on the
        // failure path leaves the likeliest drift un-looked-at.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await assert.rejects(
                session.bridge.reconcile(
                    [
                        ENDPOINTS[0]!,
                        // A role outside the §4.2 enum: the registry builds the
                        // first endpoint, then throws on this one.
                        { ...ENDPOINTS[1]!, role: "notARole" },
                    ] as never,
                    false,
                ),
            );

            // `driftChecked` is the observable: on the success path only, the
            // throw skips the detector entirely and it stays false forever.
            assert.equal(
                session.bridge.getStatus().driftChecked,
                true,
                "the failed reconcile must still have run the detector",
            );
        } finally {
            await session.close();
        }
    });

    it("runs after a remove, not only after a create", async () => {
        // §3.3 was the one operation that reshapes the live set and never
        // looked, so a surviving endpoint whose number had moved stayed
        // unreported until something else happened to create one.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            assert.equal(session.bridge.getStatus().driftChecked, false, "nothing checked yet");

            await session.bridge.removeEndpoint(KITCHEN);

            assert.equal(
                session.bridge.getStatus().driftChecked,
                true,
                "remove_endpoint must run the detector",
            );
        } finally {
            await session.close();
        }
    });

    it("raises no drift when a role change re-creates an endpoint", async () => {
        // §3.3 retains the allocation, so a remove-then-add restores the same
        // number. The retention is pinned by registry.test.ts; the *absence* of
        // a drift event was not, and a regression there would tell every user
        // their accessories had swapped identities on an ordinary role edit.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "d2");
            await session.bridge.removeEndpoint(KITCHEN);
            await session.bridge.upsertEndpoint({ ...ENDPOINTS[0]!, role: "dimmableLight" } as never);

            assert.deepEqual(session.bridge.getStatus().drift, []);
        } finally {
            await session.close();
        }
    });
});

describe("refuse-to-start: previously commissioned, fabric storage gone (PRD §7)", () => {
    it("refuses to create a single endpoint, and says which remedy applies", async () => {
        const storagePath = storage();
        const session = await boot(storagePath, "2026-08-01T00:00:00.000Z");
        try {
            assert.equal(session.bridge.endpointMapRefusal(), RefuseReason.fabricStorageLost);

            const { status: refused } = await attach(session.client, "r1");

            assert.equal(refused.error_code, ErrorCode.endpointMapInvalid);
            assert.match(String(refused.details), /Matter fabric storage is gone/);
            // The claim that matters: nothing was created. A node that served
            // endpoints here would re-create every accessory in every paired
            // ecosystem under fresh numbers.
            const status = await session.client.request({ message_id: "r2", command: "get_status", args: {} });
            assert.equal((status.result as { endpointCount: number }).endpointCount, 0);
            assert.ok(!existsSync(join(storagePath, ENDPOINT_MAP_FILE)));
        } finally {
            await session.close();
        }
    });

    it("serves normally after the user confirms the rebuild, and stays served after a restart", async () => {
        const storagePath = storage();
        const first = await boot(storagePath, "2026-08-01T00:00:00.000Z");
        await first.client.request({ message_id: "r3", command: "rebuild_endpoint_map", args: {} });
        const { status: served } = await attach(first.client, "r4");
        await first.close();

        assert.equal((served.result as { endpointCount: number }).endpointCount, 2);

        // The witness is cleared by the rebuild, so the acknowledged loss is not
        // re-litigated on every subsequent start.
        const second = await boot(storagePath);
        const refusal = second.bridge.endpointMapRefusal();
        const again = await attach(second.client, "r5");
        await second.close();

        assert.equal(refusal, undefined);
        assert.deepEqual(driftOf(again.status), []);
    });
});
