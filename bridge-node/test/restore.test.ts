/**
 * Issue #141 against a real Matter stack: **the bridge is never online with an
 * empty accessory list.**
 *
 * The defect was measured on jarvis after a reboot, not inferred. The node
 * called `server.start()` with a childless aggregator and stayed that way for
 * 23 seconds, until the plugin connected and attached. Apple reconnects inside
 * that window, reads an empty `PartsList`, concludes every accessory has gone,
 * and when they reappear treats them as NEW accessories — dumped in the
 * bridge's own room, with metadata the user can no longer edit. Every restart
 * of the bridge therefore destroyed the user's room assignments for every
 * exported device.
 *
 * The headline test below is the one that encodes the whole bug: it snapshots
 * the aggregator's children **at the instant `ServerNode.start()` is invoked**
 * and again when the node announces it is online, and both must already hold
 * the accessories. Asserting the endpoint set only after `BridgeNode.start()`
 * returns would pass just as happily with the restore in the wrong place.
 *
 * Its own file for the same reason `persistence.test.ts` is: node's test runner
 * forks per file, matter.js takes an exclusive lock per storage path, and these
 * tests restart a `ServerNode` on the *same* path.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { type Endpoint, Logger, ServerNode } from "@matter/main";

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
const KITCHEN = 223456789;
const LOUNGE = 223456790;
const HALL = 223456791;

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
    const dir = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-restore-"));
    scratch.push(dir);
    return dir;
}

const IDENTITY = { installId: "restore000000001", passcode: 20202021, discriminator: 3841 };

const KITCHEN_SPEC = {
    indigoDeviceId: KITCHEN,
    role: "onOffLight",
    label: "Kitchen Lamp",
    reachable: true,
    states: { onOff: true },
    options: {},
};
const LOUNGE_SPEC = {
    indigoDeviceId: LOUNGE,
    role: "dimmableLight",
    label: "Lounge Lamp",
    reachable: true,
    states: { onOff: true, level: 60 },
    options: {},
};
const HALL_SPEC = {
    indigoDeviceId: HALL,
    role: "onOffPlugInUnit",
    label: "Hall Plug",
    reachable: true,
    states: { onOff: false },
    options: {},
};
const BOTH = [KITCHEN_SPEC, LOUNGE_SPEC];

interface Session {
    bridge: BridgeNode;
    client: TestClient;
    logged: string[];
    close: () => Promise<void>;
}

/** Start a real node + protocol server on `storagePath` and connect to it. */
async function boot(storagePath: string, options: { commissionedAt?: string; refusal?: string } = {}):
    Promise<Session> {
    const logged: string[] = [];
    const bridge = new BridgeNode(
        // Ephemeral on both ports, so a parallel run cannot collide.
        { storagePath, matterPort: 0, wsPort: 0 },
        options.commissionedAt === undefined
            ? { ...IDENTITY }
            : { ...IDENTITY, commissionedAt: options.commissionedAt },
        BRIDGE_VERSION,
        message => logged.push(message),
        options.refusal,
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
        logged,
        close: async () => {
            client.close();
            await server.close();
            await bridge.close();
        },
    };
}

/** `attach`, tolerant of an interleaved event frame (see persistence.test.ts). */
async function attach(
    client: TestClient,
    messageId: string,
    endpoints: unknown[],
    intent?: string,
): Promise<{ status: Record<string, unknown>; events: Record<string, unknown>[] }> {
    client.send({
        message_id: messageId,
        command: "attach",
        args: {
            protocolVersion: PROTOCOL_VERSION,
            pluginVersion: "2026.8.4",
            endpoints,
            ...(intent === undefined ? {} : { intent }),
        },
    });
    const events: Record<string, unknown>[] = [];
    for (;;) {
        const frame = await client.next(10_000);
        if (frame.message_id === messageId) {
            return { status: frame, events };
        }
        events.push(frame);
    }
}

function readMap(storagePath: string): EndpointMapFile {
    return JSON.parse(readFileSync(join(storagePath, ENDPOINT_MAP_FILE), "utf8")) as EndpointMapFile;
}

function numbersOf(bridge: BridgeNode): Record<number, number> {
    return Object.fromEntries(
        bridge.getStatus().endpoints.map(endpoint => [endpoint.indigoDeviceId, endpoint.endpointNumber]),
    );
}

/** The bridged children matter.js is actually carrying, in aggregator order. */
function children(server: ServerNode): Endpoint[] {
    for (const part of server.parts) {
        if (part.id === "aggregator") {
            return [...part.parts];
        }
    }
    return [];
}

function reachableOf(server: ServerNode, indigoDeviceId: number): boolean | undefined {
    const child = children(server).find(part => part.id === uniqueIdFor(indigoDeviceId));
    return (child?.stateOf("bridgedDeviceBasicInformation") as { reachable?: boolean } | undefined)?.reachable;
}

/**
 * Establish a storage dir that already holds two exported accessories, and
 * report the endpoint numbers matter.js gave them.
 *
 * A full round trip through a real node rather than a hand-written map, because
 * the numbers are matter.js's to choose and the whole point of the persisted
 * map is that it witnesses the ones it actually chose.
 */
async function seedTwoAccessories(storagePath: string): Promise<Record<number, number>> {
    const first = await boot(storagePath);
    await attach(first.client, "seed", BOTH);
    const numbers = numbersOf(first.bridge);
    await first.close();
    return numbers;
}

interface OnlineSnapshot {
    /** Child `Endpoint.id`s present when `ServerNode.start()` was invoked. */
    atStart: string[];
    /** …and when matter.js announced the node was online. */
    atOnline: string[];
    sawOnline: boolean;
}

/**
 * Snapshot the aggregator's children at the two moments issue #141 is about.
 *
 * `ServerNode.prototype.start` is patched rather than a seam being added to
 * `BridgeNode`, because the production ordering is exactly what is under test:
 * a seam the node called at the right moment would be a second way of asking
 * the same object the same question, and it would go on passing if somebody
 * moved the restore below `server.start()`.
 */
function watchGoingOnline(): { snapshots: OnlineSnapshot[]; restore: () => void } {
    const snapshots: OnlineSnapshot[] = [];
    const original = ServerNode.prototype.start;
    ServerNode.prototype.start = async function patched(this: ServerNode): Promise<void> {
        const snapshot: OnlineSnapshot = {
            atStart: children(this).map(child => child.id),
            atOnline: [],
            sawOnline: false,
        };
        snapshots.push(snapshot);
        this.lifecycle.online.on(() => {
            snapshot.atOnline = children(this).map(child => child.id);
            snapshot.sawOnline = true;
        });
        return original.call(this);
    };
    return {
        snapshots,
        // `delete` rather than reassignment: `start` is inherited from `Node`,
        // so the patch is an own property and removing it puts the prototype
        // chain back exactly as it was.
        restore: () => {
            delete (ServerNode.prototype as { start?: unknown }).start;
        },
    };
}

describe("issue #141: the bridge is never online with an empty accessory list", () => {
    it("has its accessories in the aggregator before the Matter stack is told to go online", async () => {
        // ⊗ THE headline. Before the fix, `start()` was called with a childless
        // aggregator and stayed empty until the plugin attached — 23 seconds on
        // jarvis after a reboot — so `atStart` and `atOnline` were both `[]` and
        // Apple re-created every accessory in the bridge's own room. Move the
        // restore below `server.start()` and this fails on `atStart` alone.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);

        const watch = watchGoingOnline();
        let session: Session | undefined;
        try {
            // Deliberately NO attach: the plugin plays no part in this.
            session = await boot(storagePath);
            const snapshot = watch.snapshots.at(-1);

            assert.ok(snapshot !== undefined, "the node must have started a ServerNode");
            assert.deepEqual(
                [...snapshot.atStart].sort(),
                [uniqueIdFor(KITCHEN), uniqueIdFor(LOUNGE)].sort(),
                "the aggregator must already carry both accessories when start() is called",
            );
            assert.equal(snapshot.sawOnline, true, "the node must actually have come online");
            assert.deepEqual(
                [...snapshot.atOnline].sort(),
                [uniqueIdFor(KITCHEN), uniqueIdFor(LOUNGE)].sort(),
                "and still carry them at the moment it announces it is online",
            );
            // The numbers are the identity: a restore that re-created them at
            // fresh numbers would be the same accessory swap by another route.
            assert.deepEqual(numbersOf(session.bridge), numbers, "restored at their PERSISTED numbers");
        } finally {
            watch.restore();
            await session?.close();
        }
    });

    it("restores them unreachable, because nothing has confirmed their state yet", async () => {
        // §3.5 / PRD XAC8: "present but not currently driven" is exactly what
        // Reachable: false says, and it is the honest answer until the plugin
        // attaches. Inventing `reachable: true` would have every ecosystem show
        // a live-looking accessory whose state nobody has read.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            assert.equal(reachableOf(session.bridge.server, KITCHEN), false);
            assert.equal(reachableOf(session.bridge.server, LOUNGE), false);

            // …and the attach is what makes them reachable again.
            await attach(session.client, "r1", BOTH);
            assert.equal(reachableOf(session.bridge.server, KITCHEN), true);
            assert.equal(reachableOf(session.bridge.server, LOUNGE), true);
        } finally {
            await session.close();
        }
    });

    it("raises no drift: these are the map's own numbers being re-used", async () => {
        // ⊗ The restore hands matter.js the same `Endpoint.id`s the map is keyed
        // on, so it hands back the same numbers. A restore that counted as drift
        // would tell the user their accessories had swapped identities on every
        // ordinary restart — and #140 means they could not clear the report.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            assert.deepEqual(session.bridge.getStatus().drift, [], "restoring is not a comparison");
            assert.equal(
                session.bridge.getStatus().driftChecked,
                false,
                "and it has not checked anything either",
            );

            const { status, events } = await attach(session.client, "r2", BOTH);

            assert.deepEqual((status.result as { drift: unknown[] }).drift, []);
            assert.equal((status.result as { driftChecked: boolean }).driftChecked, true);
            assert.deepEqual(events, [], "no drift_detected for an ordinary restart");
        } finally {
            await session.close();
        }
    });
});

describe("issue #141: attach stays authoritative over the restored set", () => {
    it("updates rather than creates when the plugin asks for the same devices", async () => {
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            await attach(session.client, "a1", BOTH);

            const reconciled = session.logged.filter(line => line.startsWith("Reconciled endpoints:"));
            assert.deepEqual(reconciled, [
                "Reconciled endpoints: 0 created, 2 updated, 0 recreated, 0 removed (2 live)",
            ]);
            assert.deepEqual(numbersOf(session.bridge), numbers, "and nothing was renumbered");
        } finally {
            await session.close();
        }
    });

    it("removes one that was un-exported while the plugin was away", async () => {
        // Restored-then-not-in-attach IS a removal, and that is correct: the
        // allow-list is the source of truth for the export set (§6.2), and the
        // restore is only a guess at what it still says.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            assert.equal(session.bridge.getStatus().endpointCount, 2, "both restored");

            await attach(session.client, "a2", [KITCHEN_SPEC]);

            assert.deepEqual(
                session.bridge.getStatus().endpoints.map(endpoint => endpoint.indigoDeviceId),
                [KITCHEN],
            );
            assert.deepEqual(children(session.bridge.server).map(child => child.id), [uniqueIdFor(KITCHEN)]);
        } finally {
            await session.close();
        }
    });

    it("creates one that was exported while the plugin was away", async () => {
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            await attach(session.client, "a3", [...BOTH, HALL_SPEC]);

            const reconciled = session.logged.filter(line => line.startsWith("Reconciled endpoints:"));
            assert.deepEqual(reconciled, [
                "Reconciled endpoints: 1 created, 2 updated, 0 recreated, 0 removed (3 live)",
            ]);
            assert.equal(session.bridge.getStatus().endpointCount, 3);
        } finally {
            await session.close();
        }
    });
});

describe("issue #141 meets the §3.1 mass-removal guard", () => {
    it("still refuses an emptying attach — the live set is no longer zero at first attach", async () => {
        // ⊗ The guard reads the LIVE set, and before the restore a fresh node's
        // was empty at first attach, so `live.size > 0` was false and an
        // emptying attach sailed through as a no-op. With a restored set the
        // guard is armed from the very first attach — which is what it is for:
        // a stale or buggy client must not silently un-export everything.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            const refused = await attach(session.client, "g1", []);

            assert.equal(refused.status.error_code, ErrorCode.massRemovalRefused);
            assert.equal(
                session.bridge.getStatus().endpointCount,
                2,
                "a refusal leaves the endpoint set untouched",
            );
        } finally {
            await session.close();
        }
    });

    it("still lets the plugin's pending un-export through with intent: replace_all", async () => {
        // The XAC7 debt path: the allow-list was emptied while the node was
        // down, so the reconnect carries the intent and the restored set is
        // exactly what it has to remove.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            const discharged = await attach(session.client, "g2", [], "replace_all");

            assert.equal(discharged.status.error_code, undefined, JSON.stringify(discharged.status));
            assert.equal((discharged.status.result as { endpointCount: number }).endpointCount, 0);
            assert.deepEqual(children(session.bridge.server), []);
        } finally {
            await session.close();
        }
    });
});

describe("issue #141: the version 1 endpoint map", () => {
    it("migrates in place — the numbers survive, and one attach makes it restorable", async () => {
        // ⊗ A v1 file is numbers-only, which is still the identity every paired
        // ecosystem is keyed on. Treating it as corrupt (or as a v2 file with
        // missing fields) would refuse to serve, or discard the one record that
        // cannot be re-derived. It restores nothing on this start — there is no
        // role or label to build from — and that is the honest answer.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);

        // Wind the file back to exactly what E5 wrote.
        const legacy: EndpointMapFileV1 = {
            version: 1,
            endpoints: {
                [uniqueIdFor(KITCHEN)]: numbers[KITCHEN]!,
                [uniqueIdFor(LOUNGE)]: numbers[LOUNGE]!,
            },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(legacy));

        const second = await boot(storagePath);
        try {
            assert.equal(second.bridge.endpointMapRefusal(), undefined, "a v1 map is not a fault");
            assert.equal(second.bridge.getStatus().endpointCount, 0, "nothing to rebuild from yet");
            assert.deepEqual(second.bridge.getStatus().warnings, []);

            const { status } = await attach(second.client, "v1", BOTH);

            // The v1 numbers were kept as the baseline, so the attach agrees
            // with them: a migration that dropped them would report drift here.
            assert.deepEqual((status.result as { drift: unknown[] }).drift, []);
            assert.deepEqual(numbersOf(second.bridge), numbers);
        } finally {
            await second.close();
        }

        const migrated = readMap(storagePath);
        assert.equal(migrated.version, ENDPOINT_MAP_VERSION);
        assert.deepEqual(migrated.endpoints, {
            [uniqueIdFor(KITCHEN)]: { number: numbers[KITCHEN], role: "onOffLight", label: "Kitchen Lamp" },
            [uniqueIdFor(LOUNGE)]: { number: numbers[LOUNGE], role: "dimmableLight", label: "Lounge Lamp" },
        });

        // …and the start after that one restores, which is the whole point of
        // migrating rather than discarding.
        const third = await boot(storagePath);
        try {
            assert.equal(third.bridge.getStatus().endpointCount, 2);
            assert.deepEqual(numbersOf(third.bridge), numbers);
        } finally {
            await third.close();
        }
    });
});

describe("issue #141: a refusing node restores nothing", () => {
    it("skips the restore entirely when the identity file is unusable", async () => {
        // ⊗ The refusal exists because a node that cannot trust its own records
        // must not create accessories: doing so under an identity nobody has
        // seen duplicates every one of them in every paired ecosystem. The
        // restore is a creation path like any other and has to obey it — and
        // this one is decided before the stack starts, so nothing is ever built.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const watch = watchGoingOnline();
        let session: Session | undefined;
        try {
            session = await boot(storagePath, { refusal: RefuseReason.identityUnreadable });

            assert.equal(session.bridge.endpointMapRefusal(), RefuseReason.identityUnreadable);
            assert.deepEqual(watch.snapshots.at(-1)?.atStart, [], "nothing was ever built");
            assert.equal(session.bridge.getStatus().endpointCount, 0);
            assert.ok(
                session.logged.some(line => line.startsWith("Not restoring any endpoints from the map")),
                "and it says why",
            );
        } finally {
            watch.restore();
            await session?.close();
        }
    });

    it("withdraws the restored set when the fabric-storage-lost refusal lands after start", async () => {
        // That refusal needs the fabric table, which cannot be read before the
        // stack is up — and the restore has to happen before it or there is no
        // restore at all. So it is taken back: by definition this node has NO
        // fabrics, so nothing was ever attached to see it, and the invariant
        // ("nothing is created while refusing") holds where it matters.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath, { commissionedAt: "2026-08-01T00:00:00.000Z" });
        try {
            assert.equal(session.bridge.endpointMapRefusal(), RefuseReason.fabricStorageLost);
            assert.equal(session.bridge.getStatus().endpointCount, 0);
            assert.deepEqual(children(session.bridge.server), []);
            assert.ok(
                session.logged.some(line => line.startsWith("Withdrawing the 2 endpoint(s) restored")),
                "and it says so rather than leaving the user to wonder",
            );
        } finally {
            await session.close();
        }
    });
});
