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
import {
    chmodSync,
    existsSync,
    mkdirSync,
    mkdtempSync,
    readFileSync,
    rmSync,
    writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { Logger, type ServerNode } from "@matter/main";

import {
    ENDPOINT_MAP_FILE,
    ENDPOINT_MAP_VERSION,
    type EndpointMapFile,
    type EndpointMapFileV1,
} from "../src/endpoint-map.js";
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
        publishedAs: uniqueIdFor(KITCHEN),
        role: "onOffLight",
        label: "Kitchen Lamp",
        reachable: true,
        states: { onOff: false },
        options: {},
    },
    {
        indigoDeviceId: LOUNGE,
        publishedAs: uniqueIdFor(LOUNGE),
        role: "dimmableLight",
        label: "Lounge Lamp",
        reachable: true,
        states: { onOff: true, level: 60 },
        options: {},
    },
];

/**
 * A {@link BridgeNode} that can be posed as commissioned.
 *
 * Three of the decisions this file is about only ever run once a fabric
 * exists — the E5 migration bootstrap, the commissioning witness, and §3.11
 * leaving that witness alone on a bridge that is still paired — and a fabric
 * cannot be manufactured in a test: commissioning needs a controller, real
 * certificates and a real network. That is why `refuseReasonFor` was extracted
 * as a pure function in the first place; this is the same problem one layer up,
 * where the code under test is `node.ts` and a real ServerNode has to be there.
 *
 * What is posed is only matter.js's *answer about its own commissioning state*:
 * the two properties `node.ts` reads, `lifecycle.isCommissioned` and
 * `state.commissioning.fabrics`. Everything underneath is the real stack doing
 * the real work, so the endpoint map, the identity file and the log are all
 * genuine — which is the point, because they are what these tests assert on.
 */
class PosedNode extends BridgeNode {
    /** The fabric table the node will see. Empty means never paired. */
    posedFabrics: { fabricIndex: number; label: string; rootVendorId: number }[] = [];

    override get server(): ServerNode {
        const real = super.server;
        if (this.posedFabrics.length === 0) {
            return real;
        }
        // `isCommissioned` is a getter on the lifecycle object's prototype, so
        // an own data property shadows it. The fabric table is NOT
        // configurable — matter.js seals it — hence the read-through Proxy for
        // that one and only that one.
        Object.defineProperty(real.lifecycle, "isCommissioned", { value: true, configurable: true });
        const fabrics = Object.fromEntries(this.posedFabrics.map(fabric => [fabric.fabricIndex, fabric]));
        return new Proxy(real, {
            get(target, prop) {
                if (prop !== "state") {
                    const value: unknown = Reflect.get(target, prop, target);
                    // Bound, or a method reached through the proxy would run
                    // with the proxy as `this` and fail on matter.js's own
                    // private fields.
                    return typeof value === "function" ? value.bind(target) : value;
                }
                const state = Reflect.get(target, prop, target) as Record<string, unknown>;
                return new Proxy(state, {
                    get(stateTarget, stateProp) {
                        if (stateProp !== "commissioning") {
                            return Reflect.get(stateTarget, stateProp, stateTarget);
                        }
                        const commissioning = Reflect.get(stateTarget, stateProp, stateTarget) as Record<
                            string,
                            unknown
                        >;
                        return new Proxy(commissioning, {
                            get: (target_, prop_) =>
                                prop_ === "fabrics" ? fabrics : Reflect.get(target_, prop_, target_),
                        });
                    },
                });
            },
        });
    }
}

/** One posed fabric, shaped as matter.js's own `state.commissioning.fabrics` entries. */
const POSED_FABRIC = { fabricIndex: 1, label: "Apple Home", rootVendorId: 4937 };

interface PosedSession {
    bridge: PosedNode;
    /** Everything the node logged, which is where several of these answers live. */
    logged: string[];
    close: () => Promise<void>;
}

/** Start a posed node — `commissioned: true` gives it one fabric from the off. */
async function bootPosed(
    storagePath: string,
    options: { commissioned: boolean; commissionedAt?: string },
): Promise<PosedSession> {
    const logged: string[] = [];
    const bridge = new PosedNode(
        { storagePath, matterPort: 0, wsPort: 0 },
        options.commissionedAt === undefined
            ? { ...IDENTITY }
            : { ...IDENTITY, commissionedAt: options.commissionedAt },
        BRIDGE_VERSION,
        message => logged.push(message),
    );
    // Before `start()`: the migration decision is taken during it.
    bridge.posedFabrics = options.commissioned ? [POSED_FABRIC] : [];
    await bridge.start();
    return { bridge, logged, close: () => bridge.close() };
}

function migrationLines(logged: readonly string[]): string[] {
    return logged.filter(line => line.startsWith("MIGRATION"));
}

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

        // Since #141 the entry carries the role and label too — that is what
        // lets the next start rebuild the accessory before it goes online.
        // Since #219 it also carries the driving device id.
        assert.deepEqual(persisted.endpoints, {
            [uniqueIdFor(KITCHEN)]: {
                number: before[KITCHEN],
                role: "onOffLight",
                label: "Kitchen Lamp",
                deviceId: KITCHEN,
            },
            [uniqueIdFor(LOUNGE)]: {
                number: before[LOUNGE],
                role: "dimmableLight",
                label: "Lounge Lamp",
                deviceId: LOUNGE,
            },
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
        const current = readMap(storagePath);
        const poisoned: EndpointMapFile = {
            ...current,
            endpoints: {
                ...current.endpoints,
                [uniqueIdFor(KITCHEN)]: { ...current.endpoints[uniqueIdFor(KITCHEN)]!, number: 99 },
            },
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
        assert.equal(afterMap.endpoints[uniqueIdFor(KITCHEN)]?.number, 99);
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
            [uniqueIdFor(KITCHEN)]: {
                number: before[KITCHEN],
                role: "onOffLight",
                label: "Kitchen Lamp",
                deviceId: KITCHEN,
            },
            [uniqueIdFor(LOUNGE)]: {
                number: before[LOUNGE],
                role: "dimmableLight",
                label: "Lounge Lamp",
                deviceId: LOUNGE,
            },
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
        // than a member. Since issue #140 the numbers themselves are also
        // marked VOID — matter.js's own allocation was just wiped along with
        // them — so the entries keep their number/role/label but every one
        // gains `numberVoid: true`, which the next `check()` clears silently.
        assert.deepEqual(
            after.endpoints,
            Object.fromEntries(
                Object.entries(before.endpoints).map(([uniqueId, record]) => [
                    uniqueId,
                    { ...record, numberVoid: true },
                ]),
            ),
        );
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

describe("factory_reset (preserve: true) voids the map instead of drift-checking it (issue #140)", () => {
    it("marks every entry VOID and logs the adoption message, not the old §3.11 remedy", async () => {
        const storagePath = storage();
        const logged: string[] = [];
        const bridge = new BridgeNode(
            { storagePath, matterPort: 0, wsPort: 0 },
            { ...IDENTITY },
            BRIDGE_VERSION,
            message => logged.push(message),
        );
        await bridge.start();
        try {
            await bridge.reconcile(ENDPOINTS as never, false);
            const before = readMap(storagePath);
            assert.ok(Object.keys(before.endpoints).length > 0, "sanity: something was recorded");

            await bridge.factoryReset(true);

            const after = readMap(storagePath);
            for (const [uniqueId, record] of Object.entries(before.endpoints)) {
                assert.deepEqual(
                    after.endpoints[uniqueId],
                    { ...record, numberVoid: true },
                    "the reset erased matter.js's own allocation, so the witness must be voided, not left standing",
                );
            }
            assert.ok(
                logged.some(line => line.includes("now VOID")),
                `expected the new voiding log line, got: ${logged.join("\n")}`,
            );
            // #140's whole complaint: this remedy sent the user to a §3.11
            // rebuild the plugin refuses on a healthy node.
            assert.ok(
                !logged.some(line => line.includes("rebuild the map (§3.11) to accept it")),
                "the old drift-and-rebuild-remedy line must be gone",
            );
        } finally {
            await bridge.close();
        }
    });

    it("tells the truth when the VOID markers cannot reach disk, instead of claiming success", async t => {
        // ⊗ `voidNumbers`'s boolean return used to be discarded here, so a
        // failed write still logged "its numbers are now VOID ... a §3.11
        // rebuild is not needed" — over markers that existed in memory only.
        // A crash before the next successful persist would have brought back
        // #140's forever-drift report after the user was told it was fixed.
        if (process.getuid?.() === 0) {
            t.skip("root ignores directory permissions");
            return;
        }
        const storagePath = storage();
        const logged: string[] = [];
        const bridge = new BridgeNode(
            { storagePath, matterPort: 0, wsPort: 0 },
            { ...IDENTITY },
            BRIDGE_VERSION,
            message => logged.push(message),
        );
        await bridge.start();
        try {
            await bridge.reconcile(ENDPOINTS as never, false);

            chmodSync(storagePath, 0o500);
            try {
                await bridge.factoryReset(true);
            } finally {
                chmodSync(storagePath, 0o700);
            }

            assert.ok(
                logged.some(line => line.includes("VOID markers could NOT be written")),
                `expected the write-failure branch, got: ${logged.join("\n")}`,
            );
            assert.ok(
                !logged.some(line => line.includes("its numbers are now VOID")),
                "the success message must not fire over an unwritten baseline",
            );
        } finally {
            await bridge.close();
        }
    });
});

describe("noteLastFabricGone voids the endpoint map too (issue #140)", () => {
    it("marks every entry VOID for the same reason as a preserving factory reset", async () => {
        // The last-fabric self-reset (§3.9's last unpair, or an ecosystem
        // removing us) drives matter.js to erase itself exactly as `erase()`
        // does, so `noteLastFabricGone` carries the same obligation. Reached
        // directly, the way `noteFabrics never swallows the read` below reaches
        // its private method: a real self-reset cannot be manufactured without
        // a real commissioner.
        const storagePath = storage();
        writeFileSync(
            join(storagePath, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: ENDPOINT_MAP_VERSION,
                endpoints: {
                    [uniqueIdFor(KITCHEN)]: { number: 2, role: "onOffLight", label: "Kitchen Lamp" },
                },
            }),
        );
        const witness = "2026-08-01T00:00:00.000Z";
        const bridge = new BridgeNode(
            { storagePath, matterPort: 0, wsPort: 0 },
            { ...IDENTITY, commissionedAt: witness },
            BRIDGE_VERSION,
            () => {},
        );
        await bridge.start();
        try {
            assert.equal(
                bridge.endpointMapRefusal(),
                RefuseReason.fabricStorageLost,
                "sanity: a witness with no real fabric must be refusing",
            );

            (bridge as unknown as { noteLastFabricGone(): void }).noteLastFabricGone();

            assert.deepEqual(readMap(storagePath).endpoints, {
                [uniqueIdFor(KITCHEN)]: {
                    number: 2,
                    role: "onOffLight",
                    label: "Kitchen Lamp",
                    numberVoid: true,
                },
            });
        } finally {
            await bridge.close();
        }
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
                        // A label past Matter's NodeLabel limit: the registry
                        // builds the first endpoint, then throws while building
                        // this one. A bad *role* would not do — `reconcileNow`
                        // validates every role up front, so nothing is created
                        // and there is no part-applied set to check.
                        { ...ENDPOINTS[1]!, label: "x".repeat(300) },
                    ] as never,
                    false,
                ),
            );

            // The first endpoint IS there, and `driftChecked` is the
            // observable: on the success path only, the throw skips the
            // detector entirely and it stays false forever.
            assert.equal(session.bridge.getStatus().endpointCount, 1, "the reconcile must have part-applied");
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
        //
        // The observable is the drift being RECOMPUTED rather than
        // `driftChecked` flipping: an empty comparison no longer claims to have
        // checked anything (§4.3), so a remove on a node that never attached —
        // which is what this test used to do — verifies nothing at all now.
        // Poisoning one number and watching the removal clear the report is the
        // same assertion against a live set that actually exists.
        const storagePath = storage();
        writeFileSync(
            join(storagePath, ENDPOINT_MAP_FILE),
            JSON.stringify({ version: 1, endpoints: { [uniqueIdFor(LOUNGE)]: 99 } }),
        );
        const session = await boot(storagePath);
        try {
            assert.equal(session.bridge.getStatus().driftChecked, false, "nothing checked yet");
            await attach(session.client, "d1");
            assert.equal(session.bridge.getStatus().drift.length, 1, "the poisoned number must be reported");

            await session.bridge.removeEndpoint(LOUNGE);

            assert.deepEqual(
                session.bridge.getStatus().drift,
                [],
                "remove_endpoint must re-run the detector, not leave the last answer standing",
            );
            assert.equal(session.bridge.getStatus().driftChecked, true);
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

describe("the E5 migration bootstrap, on a node (PRD §7, §4.3)", () => {
    it("adopts a baseline and serves when a commissioned bridge has no map at all", async () => {
        // ⊗ Skipping the bootstrap leaves a commissioned bridge serving with no
        // baseline whatsoever: `driftChecked` never becomes meaningful, and the
        // one fault this milestone exists to catch — matter.js's own allocation
        // being lost — has nothing to be compared against, forever.
        const storagePath = storage();
        const session = await bootPosed(storagePath, { commissioned: true });
        try {
            assert.equal(session.bridge.endpointMapRefusal(), undefined, "the upgrade path must SERVE");
            assert.equal(migrationLines(session.logged).length, 1, session.logged.join("\n"));
            // The baseline is empty and on disk: the first reconcile fills it
            // from the live set, which IS matter.js's persisted allocation.
            assert.deepEqual(readMap(storagePath), { version: ENDPOINT_MAP_VERSION, endpoints: {} });
        } finally {
            await session.close();
        }
    });

    it("leaves a map that IS there completely alone", async () => {
        // ⊗ Bootstrapping on `commissioned` alone re-seeds an empty baseline
        // over a good map on EVERY start — which does not renumber anything,
        // and is far worse than that: it silently throws away the only record
        // of what the numbers were, so the storage loss it exists to detect
        // becomes undetectable on the very next boot.
        const storagePath = storage();
        // A v1 (numbers-only) file on purpose: the migration path must leave a
        // legacy map exactly as it found it too.
        const existing: EndpointMapFileV1 = { version: 1, endpoints: { [uniqueIdFor(KITCHEN)]: 7 } };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(existing));

        const session = await bootPosed(storagePath, { commissioned: true });
        try {
            // The file itself, not just the log line: a re-seed writes a
            // perfectly valid map and logs a perfectly reassuring line.
            assert.deepEqual(readMap(storagePath), existing as unknown as EndpointMapFile);
            assert.deepEqual(migrationLines(session.logged), []);
        } finally {
            await session.close();
        }
    });

    it("says nothing about a migration on a bridge that has never been paired", async () => {
        // ⊗ A never-commissioned bridge has no map because it has never needed
        // one. Announcing a MIGRATION there tells every fresh install that
        // something was recovered, which is the log line a user reads when they
        // are trying to work out whether their accessories moved.
        const storagePath = storage();
        const session = await bootPosed(storagePath, { commissioned: false });
        try {
            assert.equal(session.bridge.endpointMapRefusal(), undefined);
            assert.deepEqual(migrationLines(session.logged), []);
            assert.ok(!existsSync(join(storagePath, ENDPOINT_MAP_FILE)), "and it writes no baseline");
        } finally {
            await session.close();
        }
    });

    it("does not claim the migration happened when the baseline could not be written", async t => {
        if (process.getuid?.() === 0) {
            t.skip("root ignores directory permissions");
            return;
        }
        // ⊗ The MIGRATION line is written before the answer is known, and
        // `seed()`'s return was dropped on the floor — so a bridge that could
        // not write the baseline reported the migration as done, and the next
        // start bootstrapped again over a log that said it never needed to.
        const storagePath = storage();
        // One clean run first, so matter.js's own storage subdirectory already
        // exists: the read-only directory below must block OUR new file, not
        // the Matter stack's startup.
        await (await bootPosed(storagePath, { commissioned: true })).close();
        rmSync(join(storagePath, ENDPOINT_MAP_FILE));

        chmodSync(storagePath, 0o500);
        try {
            const session = await bootPosed(storagePath, { commissioned: true });
            try {
                assert.equal(migrationLines(session.logged).length, 2, session.logged.join("\n"));
                assert.ok(
                    session.logged.some(line => line.startsWith("MIGRATION NOT RECORDED")),
                    session.logged.join("\n"),
                );
                assert.ok(!existsSync(join(storagePath, ENDPOINT_MAP_FILE)));
            } finally {
                await session.close();
            }
        } finally {
            chmodSync(storagePath, 0o700);
        }
    });

    it("surfaces a commissioning witness that could not be written as a §4.3 warning", async () => {
        // ⊗ Two mutations live here: `applyWitness` recording the failure as a
        // *cleared* warning, and `noteCommissioningWitness` taking the identity
        // back without going through it at all. Either way the bridge comes up
        // looking healthy while the witness that arms PRD §7's refuse-to-start
        // is quietly absent — and nobody finds out until the fabric storage is
        // lost and nothing refuses.
        const storagePath = storage();
        // A directory where identity.json goes: the atomic write's `rename`
        // cannot land on it, which is a write failure the node must survive.
        mkdirSync(join(storagePath, "identity.json"));

        const session = await bootPosed(storagePath, { commissioned: true });
        try {
            const warnings = session.bridge.getStatus().warnings;
            assert.ok(
                warnings.some(warning => warning.includes("Could not record the commissioning marker")),
                `expected the witness failure in warnings, got ${JSON.stringify(warnings)}`,
            );
        } finally {
            await session.close();
        }
    });
});

describe("§3.11 on a bridge that is still paired", () => {
    it("leaves the commissioning witness alone while fabrics remain", async () => {
        // ⊗ The witness is cleared by a rebuild only when the fabrics are
        // already gone — that is the user accepting a loss. Clearing it
        // unconditionally disarms PRD §7's refuse-to-start on a bridge that is
        // still paired: the NEXT time matter.js's storage vanishes there is no
        // evidence it was ever commissioned, so the node serves happily and
        // duplicates every accessory in every ecosystem.
        const storagePath = storage();
        const witness = "2026-08-01T00:00:00.000Z";
        writeFileSync(join(storagePath, "identity.json"), JSON.stringify({ ...IDENTITY, commissionedAt: witness }));

        const session = await bootPosed(storagePath, { commissioned: true, commissionedAt: witness });
        try {
            assert.equal(session.bridge.endpointMapRefusal(), undefined);

            await session.bridge.rebuildEndpointMap();

            const onDisk = JSON.parse(readFileSync(join(storagePath, "identity.json"), "utf8"));
            assert.equal(onDisk.commissionedAt, witness, "a rebuild must not un-witness a live pairing");
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

describe("refusing for an unusable identity is a different refusal (E5 R4)", () => {
    /** Reach the private refusal directly: it needs no Matter stack to speak. */
    function refusalLineFor(reason: string): string {
        const logged: string[] = [];
        const bridge = new BridgeNode(
            { storagePath: storage(), matterPort: 0, wsPort: 0 },
            { ...IDENTITY },
            BRIDGE_VERSION,
            message => logged.push(message),
        );
        (bridge as unknown as { refuse(reason: string): void }).refuse(reason);
        assert.equal(logged.length, 1, logged.join("\n"));
        return logged[0]!;
    }

    it("does not send the user to rebuild the map, which cannot fix it", () => {
        // ⊗ One remedy sentence was printed for every reason, and for this one
        // it names the single command guaranteed to refuse them: §3.11 THROWS
        // on `identityUnreadable` by design, because rebuilding the map would
        // leave the bridge serving under a SerialNumber no paired ecosystem has
        // ever seen. Following the log line got the user nowhere and left the
        // file that actually needs restoring unmentioned.
        const line = refusalLineFor(RefuseReason.identityUnreadable);

        assert.match(line, /REFUSING to serve endpoints/);
        assert.doesNotMatch(line, /§3\.11/);
        assert.doesNotMatch(line, /map is rebuilt/);
        assert.match(line, /identity\.json\.unreadable-<stamp>/);
        assert.match(line, /restart/);
    });

    it("still sends a map refusal to §3.11, which is where that one is fixed", () => {
        // #132: the remedy used to promise the rebuild WILL duplicate
        // accessories. It cannot — it renumbers nothing; any duplication
        // belongs to the storage loss that caused the refusal.
        const line = refusalLineFor(RefuseReason.mapUnreadable);

        assert.match(line, /endpoint map is unreadable/);
        assert.match(line, /BRIDGE_PROTOCOL §3\.11/);
        assert.match(line, /renumbers nothing/);
    });

    it("carries a refusal decided before the node existed all the way in", async () => {
        // ⊗ `main.ts` takes the identity decision before this object can be
        // built, because loading the identity is what would overwrite it. The
        // constructor argument is the only route that reason has into the node,
        // and dropping the hand-off leaves a bridge that refuses silently —
        // with nothing in the launchd log to say why anything stopped working.
        const storagePath = storage();
        const logged: string[] = [];
        const bridge = new BridgeNode(
            { storagePath, matterPort: 0, wsPort: 0 },
            { ...IDENTITY },
            BRIDGE_VERSION,
            message => logged.push(message),
            RefuseReason.identityUnreadable,
        );
        await bridge.start();
        try {
            assert.equal(bridge.endpointMapRefusal(), RefuseReason.identityUnreadable);
            assert.ok(
                logged.some(line => line.startsWith("REFUSING to serve endpoints")),
                `the refusal never reached the log: ${logged.join("\n")}`,
            );
        } finally {
            await bridge.close();
        }
    });
});

describe("the §4.3 warnings channel actually reaches the plugin", () => {
    it("puts an endpoint-map write failure on the wire, not just in the store", async t => {
        if (process.getuid?.() === 0) {
            t.skip("root ignores directory permissions");
            return;
        }
        // ⊗ Nothing read this channel end to end: every assertion on `warnings`
        // was that it is EMPTY, so dropping either half of the merge in
        // `getStatus` — the node's own identity warnings or the endpoint map's —
        // passed the whole suite. `warnings` is the only channel that reaches a
        // user in this milestone; the node's other one is a stdout nobody is
        // watching.
        const storagePath = storage();
        const session = await boot(storagePath);
        // After `start()`: matter.js creates its own storage subdirectory under
        // this path, and it must be allowed to. What the read-only directory
        // blocks is OUR new file — the endpoint map's temp-plus-rename write.
        chmodSync(storagePath, 0o500);
        try {
            await attach(session.client, "w1");

            const status = await session.client.request({ message_id: "w2", command: "get_status", args: {} });
            const warnings = (status.result as { warnings: string[] }).warnings;

            assert.ok(
                warnings.some(warning => warning.includes("Could not write the endpoint map")),
                `expected the write failure on the wire, got ${JSON.stringify(warnings)}`,
            );
            // §4.3: an unwritten baseline has verified nothing durable.
            assert.equal((status.result as { driftChecked: boolean }).driftChecked, false);
        } finally {
            chmodSync(storagePath, 0o700);
            await session.close();
        }
    });
});

describe("factory_reset tells the truth about what it could not do (§3.10)", () => {
    it("warns that the witness could not be VERIFIED when identity.json is unreadable", async () => {
        // ⊗ The verification read answers `undefined` both for "the file is
        // gone, so the witness certainly is" and for "the file is there and I
        // cannot parse it" — so a reset over a corrupt identity reported the
        // witness verified when nothing had been verified at all. If that file
        // does still carry `commissionedAt`, the next start refuses to serve
        // anything and blames lost fabric storage for the reset the user asked
        // for; being told is the difference between a two-minute fix and a
        // bridge that will not come back.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "v1");
            writeFileSync(join(storagePath, "identity.json"), "{ truncated", "utf8");

            await session.client.request(
                { message_id: "v2", command: "factory_reset", args: { preserveEndpointNumbers: true } },
                RESET_TIMEOUT_MS,
            );
            const status = await session.client.request({ message_id: "v3", command: "get_status", args: {} });

            const warnings = (status.result as { warnings: string[] }).warnings;
            assert.ok(
                warnings.some(warning => warning.includes("could NOT verify")),
                `expected an unverifiable-witness warning, got ${JSON.stringify(warnings)}`,
            );
            assert.ok(
                warnings.some(warning => warning.includes("identity.json")),
                "and it must name the file the user has to look at",
            );
        } finally {
            await session.close();
        }
    });

    it("says the endpoint map survived a discard it could not perform", async () => {
        // ⊗ `discard()` returns whether the file went, and `factoryReset`
        // ignored it before logging "Factory reset complete" — so a map that
        // outlived `preserveEndpointNumbers: false` was reported as discarded,
        // and the next start loaded it as a baseline for numbers that no longer
        // mean anything.
        const storagePath = storage();
        const logged: string[] = [];
        const bridge = new BridgeNode(
            { storagePath, matterPort: 0, wsPort: 0 },
            { ...IDENTITY },
            BRIDGE_VERSION,
            message => logged.push(message),
        );
        await bridge.start();
        try {
            await bridge.reconcile(ENDPOINTS as never, false);
            assert.ok(existsSync(join(storagePath, ENDPOINT_MAP_FILE)));
            // A directory in its place: `unlinkSync` cannot remove it, which is
            // the shape of every real deletion failure (a locked file, a
            // read-only volume) without needing either.
            rmSync(join(storagePath, ENDPOINT_MAP_FILE));
            mkdirSync(join(storagePath, ENDPOINT_MAP_FILE));

            await bridge.factoryReset(false);

            assert.ok(
                logged.some(line => line.includes("endpoint map file survived the reset")),
                `the ignored discard failure never surfaced: ${logged.join("\n")}`,
            );
            assert.ok(
                bridge.getStatus().warnings.some(warning => warning.includes("Could not delete the endpoint map")),
                "§4.3: and the plugin has to be told, not just the log",
            );
        } finally {
            await bridge.close();
        }
    });
});

describe("noteFabrics never swallows the read it depends on (E5 S2)", () => {
    it("logs what a failed fabric read costs, and does not rethrow", () => {
        // ⊗ A bare `catch { return }` hid three things at once: the failure
        // itself, every §5 fabrics_changed / commissioned / decommissioned
        // event, and — worst because it is silent and delayed — the clearing of
        // `commissionedAt` when the last fabric leaves. A deliberate unpair
        // whose read failed here strands the witness, and the NEXT start refuses
        // to serve anything, reporting lost fabric storage for something the
        // user did on purpose.
        //
        // A node that was never started reproduces exactly the read this catch
        // exists for: `fabrics()` throws on a stack that is not there, which is
        // what the teardown paths hand it.
        const logged: string[] = [];
        const bridge = new BridgeNode(
            { storagePath: storage(), matterPort: 0, wsPort: 0 },
            { ...IDENTITY },
            BRIDGE_VERSION,
            message => logged.push(message),
        );

        assert.doesNotThrow(() =>
            (bridge as unknown as { noteFabrics(change?: string): void }).noteFabrics("removed"),
        );

        assert.equal(logged.length, 1, logged.join("\n"));
        assert.match(logged[0]!, /Fabric list unavailable/);
        assert.match(logged[0]!, /commissioning witness has NOT been cleared/);
        assert.match(logged[0]!, /fabrics_changed/);
    });
});
