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
import { OnOffServer } from "@matter/main/behaviors/on-off";

import {
    ENDPOINT_MAP_FILE,
    ENDPOINT_MAP_VERSION,
    type EndpointMapFile,
    type EndpointMapFileV1,
} from "../src/endpoint-map.js";
import { uniqueIdFor } from "../src/endpoints.js";
import { BridgeNode, ENDPOINT_COUNT_ADVISORY, ENDPOINT_COUNT_WARNING, matterJsVersion } from "../src/node.js";
import { ErrorCode, PROTOCOL_VERSION, RefuseReason } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";

/** The On/Off cluster as `endpoints.ts` builds it for a light. */
const OnOffLighting = OnOffServer.with("Lighting");

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

/**
 * Strip `forget()`'s non-deterministic `orphanedAt` stamp (issue #219) so the
 * rest of a persisted record can still be `deepEqual`ed exactly; its own
 * stamping is pinned in `endpoint-map.test.ts`, so these integration tests
 * only need to know it is THERE, not what it says.
 */
function withoutOrphanedAt<T extends { orphanedAt?: string }>(record: T): Omit<T, "orphanedAt"> {
    const { orphanedAt, ...rest } = record;
    return rest;
}

/** `upsert_endpoint` for one freshly-specced onOffLight accessory, awaited. */
async function upsertOne(client: TestClient, messageId: string, indigoDeviceId: number): Promise<void> {
    client.send({
        message_id: messageId,
        command: "upsert_endpoint",
        args: {
            endpoint: {
                indigoDeviceId,
                role: "onOffLight",
                label: `Advisory ${indigoDeviceId}`,
                reachable: true,
                states: { onOff: false },
                options: {},
            },
        },
    });
    for (;;) {
        const frame = await client.next(10_000);
        if (frame.message_id === messageId) {
            assert.equal(frame.error_code, undefined, JSON.stringify(frame));
            return;
        }
    }
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

function childOf(server: ServerNode, indigoDeviceId: number): Endpoint | undefined {
    return children(server).find(part => part.id === uniqueIdFor(indigoDeviceId));
}

function reachableOf(server: ServerNode, indigoDeviceId: number): boolean | undefined {
    const child = childOf(server, indigoDeviceId);
    return (child?.stateOf("bridgedDeviceBasicInformation") as { reachable?: boolean } | undefined)?.reachable;
}

/** The name the accessory actually publishes — what an ecosystem shows the user. */
function labelOf(server: ServerNode, indigoDeviceId: number): string | undefined {
    const child = childOf(server, indigoDeviceId);
    return (child?.stateOf("bridgedDeviceBasicInformation") as { nodeLabel?: string } | undefined)?.nodeLabel;
}

function onOffOf(server: ServerNode, indigoDeviceId: number): boolean | undefined {
    const child = childOf(server, indigoDeviceId);
    return (child?.stateOf("onOff") as { onOff?: boolean } | undefined)?.onOff;
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

describe("issue #141: an un-exported device stops being restored", () => {
    it("marks its entry orphaned on removal, keeps its number AND role/label, and never comes back", async () => {
        // ⊗ THE ghost. `check` only ever adds and refreshes, so once v2 recorded
        // a role and label an entry stayed restorable FOR EVER — including for a
        // device the user deliberately un-exported. Every boot rebuilt it as a
        // child endpoint before `server.start()` and the plugin's attach removed
        // it again seconds later: the exact appear-then-vanish churn #141 exists
        // to eliminate, aimed at the devices the user had already removed, and a
        // regression of XAC7's "un-exported accessories are gone". Delete the
        // `forgetRemoved` call in `node.reconcile` and the third boot below
        // restores 2 and the attach removes one of them again.
        //
        // Since issue #219, `forget` keeps role/label (re-adopt evidence for a
        // future recreated-device UI) rather than deleting them — `orphaned` is
        // what now keeps the entry out of `restorable`.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);

        // The user un-exports the lounge lamp.
        const first = await boot(storagePath);
        try {
            await attach(first.client, "u1", [KITCHEN_SPEC]);
        } finally {
            await first.close();
        }

        const afterRemoval = readMap(storagePath);
        assert.deepEqual(
            withoutOrphanedAt(afterRemoval.endpoints[uniqueIdFor(LOUNGE)]!),
            { number: numbers[LOUNGE], role: "dimmableLight", label: "Lounge Lamp", orphaned: true, deviceId: LOUNGE },
            "the number AND role/label survive (§3.3, #219) so a re-export returns the SAME " +
                "accessory and the entry remains re-adopt evidence, but `orphaned` keeps it " +
                "out of the pre-attach rebuild",
        );
        assert.deepEqual(
            afterRemoval.endpoints[uniqueIdFor(KITCHEN)],
            { number: numbers[KITCHEN], role: "onOffLight", label: "Kitchen Lamp", deviceId: KITCHEN },
            "and the device that is still exported is untouched",
        );

        const second = await boot(storagePath);
        try {
            assert.deepEqual(
                second.bridge.getStatus().endpoints.map(endpoint => endpoint.indigoDeviceId),
                [KITCHEN],
                "only the still-exported device is restored",
            );

            // The second-order cost, pinned: a ghost was in neither the desired
            // set nor the un-export debt, yet the node still spent
            // REMOVAL_PACING_MS on it at every single attach. No restore, no
            // removal, no pacing — the reconcile is a pure update.
            await attach(second.client, "u2", [KITCHEN_SPEC]);
            assert.deepEqual(
                second.logged.filter(line => line.startsWith("Reconciled endpoints:")),
                ["Reconciled endpoints: 0 created, 1 updated, 0 recreated, 0 removed (1 live)"],
                "no ghost to remove, so no removal pacing is spent on one",
            );
        } finally {
            await second.close();
        }

        // …and one re-export refills the entry, at the number it kept.
        const third = await boot(storagePath);
        try {
            await attach(third.client, "u3", BOTH);
            assert.deepEqual(numbersOf(third.bridge), numbers, "the re-export is the same accessory");
        } finally {
            await third.close();
        }
        assert.deepEqual(readMap(storagePath).endpoints[uniqueIdFor(LOUNGE)], {
            number: numbers[LOUNGE],
            role: "dimmableLight",
            label: "Lounge Lamp",
            deviceId: LOUNGE,
        });
    });

    it("says the map is all-orphaned, not 'no role/label yet', when everything in it is un-exported", async () => {
        // #222 review: restoreEndpoints()'s empty-restorable log said "no role/label
        // yet ... the next restart will restore" UNCONDITIONALLY — false once every
        // entry actually has a role/label but is `orphaned` (#219): those entries
        // will NOT restore on the next boot, correctly, and the old message claimed
        // the opposite.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const first = await boot(storagePath);
        try {
            // XAC7's mass-removal guard refuses emptying the live set without
            // explicit intent, so un-exporting BOTH needs it here.
            const discharged = await attach(first.client, "o1", [], "replace_all");
            assert.equal(discharged.status.error_code, undefined, JSON.stringify(discharged.status));
        } finally {
            await first.close();
        }

        const second = await boot(storagePath);
        try {
            assert.ok(
                second.logged.some(line => line.includes("all of them orphaned")),
                "must name the map as all-orphaned",
            );
            assert.ok(
                second.logged.some(line => line.includes("correct outcome")),
                "must say this is the correct outcome, not a fault",
            );
            assert.ok(
                !second.logged.some(line => line.includes("no role/label yet")),
                "must NOT claim the entries lack role/label — they have it; they are orphaned",
            );
        } finally {
            await second.close();
        }
    });

    it("forgets one removed through remove_endpoint too, not just through attach", async () => {
        // ⊗ §3.3's own removal path is the other way a device leaves the live
        // set, and a fix wired only into `attach` would leave it making ghosts.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            await attach(session.client, "r0", BOTH);
            session.client.send({
                message_id: "rm1",
                command: "remove_endpoint",
                args: { indigoDeviceId: LOUNGE },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "rm1") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }
        } finally {
            await session.close();
        }

        assert.deepEqual(withoutOrphanedAt(readMap(storagePath).endpoints[uniqueIdFor(LOUNGE)]!), {
            number: numbers[LOUNGE],
            role: "dimmableLight",
            label: "Lounge Lamp",
            orphaned: true,
            deviceId: LOUNGE,
        });
    });

    it("forgets only what it watched go, never merely what is absent", async () => {
        // ⊗ The guard rail on the fix, and the reason it diffs the live set
        // rather than reading `restorable()`. Absence is not evidence: an entry
        // can be missing from the live set because it was never restorable to
        // begin with. Here HALL carries a role written by a NEWER node, so the
        // restore skips it and the attach never mentions it — it is absent from
        // the live set throughout, and no user un-exported anything.
        //
        // Widen `forgetRemoved` to "every restorable entry that is not live" and
        // this older node silently strips the newer one's entry on the first
        // attach, which is the v1-migration mistake in the other direction.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);
        const fromTheFuture = { number: 903, role: "teleporter", label: "From The Future" };
        writeFileSync(
            join(storagePath, ENDPOINT_MAP_FILE),
            JSON.stringify({
                version: ENDPOINT_MAP_VERSION,
                endpoints: {
                    [uniqueIdFor(KITCHEN)]: {
                        number: numbers[KITCHEN]!,
                        role: "onOffLight",
                        label: "Kitchen Lamp",
                    },
                    [uniqueIdFor(LOUNGE)]: {
                        number: numbers[LOUNGE]!,
                        role: "dimmableLight",
                        label: "Lounge Lamp",
                    },
                    [uniqueIdFor(HALL)]: fromTheFuture,
                } satisfies Record<string, unknown>,
            }),
        );

        const session = await boot(storagePath);
        try {
            await attach(session.client, "n1", BOTH);
        } finally {
            await session.close();
        }

        assert.deepEqual(readMap(storagePath).endpoints, {
            [uniqueIdFor(KITCHEN)]: {
                number: numbers[KITCHEN],
                role: "onOffLight",
                label: "Kitchen Lamp",
                deviceId: KITCHEN,
            },
            [uniqueIdFor(LOUNGE)]: {
                number: numbers[LOUNGE],
                role: "dimmableLight",
                label: "Lounge Lamp",
                deviceId: LOUNGE,
            },
            // Untouched: HALL was never live (its role is from the future),
            // so it was never checked and gains nothing new.
            [uniqueIdFor(HALL)]: fromTheFuture,
        });
    });
});

describe("issue #141: what a restored endpoint actually publishes", () => {
    it("comes up under its own recorded name, before any attach", async () => {
        // ⊗ T1. `label: entry.label` was pinned by nothing: mutate it to a
        // constant and every test still passed while the accessory really did
        // come up in the Home app as "Restored Accessory". The name is the whole
        // point — restoring the SET but not the identity is the same lost
        // metadata #141 is about, by a slower route.
        const storagePath = storage();
        await seedTwoAccessories(storagePath);

        const session = await boot(storagePath);
        try {
            assert.equal(labelOf(session.bridge.server, KITCHEN), "Kitchen Lamp");
            assert.equal(labelOf(session.bridge.server, LOUNGE), "Lounge Lamp");
        } finally {
            await session.close();
        }
    });

    it("invents no state value — the role's own default stands until the plugin speaks", async () => {
        // ⊗ T6. `states: {}` is load-bearing: the restore has read nothing from
        // Indigo, so writing any value would have every ecosystem show a reading
        // nobody has taken. Mutate it to `{onOff: true}` and a lamp that was off
        // when the bridge restarted comes back reported as on.
        const storagePath = storage();
        const off = { ...KITCHEN_SPEC, states: { onOff: false } };
        const first = await boot(storagePath);
        try {
            await attach(first.client, "s0", [off]);
            assert.equal(onOffOf(first.bridge.server, KITCHEN), false);
        } finally {
            await first.close();
        }

        const second = await boot(storagePath);
        try {
            assert.equal(
                onOffOf(second.bridge.server, KITCHEN),
                false,
                "the restore wrote nothing, so nothing changed",
            );
        } finally {
            await second.close();
        }
    });
});

describe("issue #143: a created endpoint publishes the state its spec carried", () => {
    it("does not resurrect the value matter.js persisted for a previously-removed accessory", async () => {
        // Measured on jarvis, not inferred: three lights were un-exported, then
        // re-exported while Alexa was still paired, and all three came back
        // reported OFF while actually on. matter.js keeps
        // `…parts.<id>.onOff.onOff` in its own store after `endpoint.close()`,
        // and a persisted value OUTRANKS the `initialState` the constructor was
        // given — so `create()` handing the spec's states to `createEndpoint`
        // is not enough on its own for any endpoint id the store has seen
        // before. `update()` has always followed up with `applyStates`;
        // `create()` did not.
        const storagePath = storage();
        const off = { ...KITCHEN_SPEC, states: { onOff: false } };

        // LOUNGE stays exported throughout, so removing KITCHEN is an ordinary
        // un-export and not the §3.1 mass removal (which needs an intent).
        const session = await boot(storagePath);
        try {
            await attach(session.client, "a0", [off, LOUNGE_SPEC]);
            assert.equal(onOffOf(session.bridge.server, KITCHEN), false);

            // Un-export: the endpoint goes, its persisted attribute does not.
            await attach(session.client, "a1", [LOUNGE_SPEC]);
            assert.equal(childOf(session.bridge.server, KITCHEN), undefined);

            // Re-export the same device, now genuinely on.
            await attach(session.client, "a2", [KITCHEN_SPEC, LOUNGE_SPEC]);
            assert.equal(
                onOffOf(session.bridge.server, KITCHEN),
                true,
                "the re-created accessory must publish the state Indigo just reported, " +
                    "not the one it had when the user un-exported it",
            );
        } finally {
            await session.close();
        }
    });

    it("publishes the spec's state when a RESTARTED node re-creates an un-exported accessory", async () => {
        // The field sequence exactly: the accessory was off, the user
        // un-exported it, the node restarted (so matter.js loaded the persisted
        // `false` from disk rather than carrying a closed endpoint in memory),
        // and the user then re-exported it while it was on. This is the one the
        // in-process test above cannot reach.
        const storagePath = storage();
        const off = { ...KITCHEN_SPEC, states: { onOff: false } };

        const first = await boot(storagePath);
        try {
            await attach(first.client, "c0", [off, LOUNGE_SPEC]);
            assert.equal(onOffOf(first.bridge.server, KITCHEN), false);
            await attach(first.client, "c1", [LOUNGE_SPEC]);
            assert.equal(childOf(first.bridge.server, KITCHEN), undefined);
        } finally {
            await first.close();
        }

        const second = await boot(storagePath);
        try {
            await attach(second.client, "c2", [LOUNGE_SPEC]);
            second.client.send({
                message_id: "c3",
                command: "upsert_endpoint",
                args: { endpoint: KITCHEN_SPEC },
            });
            for (;;) {
                const frame = await second.client.next(10_000);
                if (frame.message_id === "c3") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }
            assert.equal(
                onOffOf(second.bridge.server, KITCHEN),
                true,
                "a re-exported accessory must publish the state Indigo just reported",
            );
        } finally {
            await second.close();
        }
    });

    it("publishes the spec's state for an accessory the store already knows, across a restart", async () => {
        // The same fault by the other route, and the one that hits every user
        // rather than only those who un-export: after a restart the restore
        // creates the endpoint with `states: {}` (correctly — nothing has been
        // read from Indigo yet), so the persisted value is what is live. The
        // plugin's attach then supplies the truth. That lands through
        // `update()` here, so this passes today and pins it.
        const storagePath = storage();
        const off = { ...KITCHEN_SPEC, states: { onOff: false } };
        const first = await boot(storagePath);
        try {
            await attach(first.client, "b0", [off]);
        } finally {
            await first.close();
        }

        const second = await boot(storagePath);
        try {
            await attach(second.client, "b1", [KITCHEN_SPEC]);
            assert.equal(onOffOf(second.bridge.server, KITCHEN), true);
        } finally {
            await second.close();
        }
    });
});

describe("issue #141: one unusable map entry costs only itself", () => {
    it("skips a key that is not ours and a role this build does not know", async () => {
        // ⊗ T3. The `indigoDeviceIdFrom`/`isRole`/`isSupportedRole` skip in
        // node.ts was tested only through its helpers: collapse the whole
        // condition to an undefined check and nothing failed. `indigo-1e3` is
        // the case the strict parse exists for — `Number()` would coerce it to
        // 1000 and invent a device id, which is a new accessory in every paired
        // ecosystem — and an unknown role is what a map written by a NEWER node
        // looks like.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);
        const handWritten: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: {
                [uniqueIdFor(KITCHEN)]: {
                    number: numbers[KITCHEN]!,
                    role: "onOffLight",
                    label: "Kitchen Lamp",
                },
                "indigo-1e3": { number: 900, role: "onOffLight", label: "Not Ours" },
                [uniqueIdFor(HALL)]: { number: 901, role: "teleporter", label: "From The Future" },
            },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(handWritten));

        const session = await boot(storagePath);
        try {
            assert.deepEqual(
                session.bridge.getStatus().endpoints.map(endpoint => endpoint.indigoDeviceId),
                [KITCHEN],
                "the good entry restores and the two bad ones cost it nothing",
            );
            for (const uniqueId of ["indigo-1e3", uniqueIdFor(HALL)]) {
                assert.ok(
                    session.logged.some(
                        line =>
                            line.startsWith(`Endpoint map entry ${uniqueId} `) &&
                            line.includes("cannot be rebuilt by this bridge version"),
                    ),
                    `${uniqueId} must be skipped AND named: ${session.logged.join(" | ")}`,
                );
            }
        } finally {
            await session.close();
        }
    });

    it("restores the rest when one spec throws on its way into the Matter tree", async () => {
        // ⊗ T2. Deleting `restore()`'s per-spec try/catch survived every test,
        // and without it one corrupt entry aborts the whole restore — so the
        // bridge boots empty, which is the bug.
        //
        // The ORIGINAL fixture here was `indigo-0${KITCHEN}` / `indigo-${KITCHEN}`
        // — two different map keys that parse to the SAME device id, so
        // `restoreEndpoints()` (commit 1's temporary re-derivation of
        // `publishedAs` from the parsed device id, not the map key itself) built
        // BOTH specs with the identical `Endpoint.id`, and the second collided
        // inside matter.js. Since commit 3, `publishedAs` is the map's OWN key —
        // always unique, because a JS object cannot hold two entries under one
        // key — so that collision can no longer happen BY CONSTRUCTION: every
        // restored spec gets a distinct `Endpoint.id` for free. What can still
        // fail on its way into the Matter tree is an entry whose KEY is not a
        // legal one: `deviceId` makes it resolvable (issue #219 — a re-adopted
        // or hand-edited entry need not have a key `parsePublishedId` accepts)
        // while the key itself violates PR5 design F9's "no `.` in an `Endpoint.id`" —
        // exactly the kind of corrupted-but-resolvable entry `restore()`'s
        // per-spec try/catch exists to survive.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);
        const COLLIDER_DEVICE_ID = 902_000_000;
        const handWritten: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: {
                [uniqueIdFor(KITCHEN)]: {
                    number: numbers[KITCHEN]!,
                    role: "onOffLight",
                    label: "Kitchen Lamp",
                },
                "indigo-not.a.legal.id": {
                    number: 902,
                    role: "onOffLight",
                    label: "Collides",
                    deviceId: COLLIDER_DEVICE_ID,
                },
                [uniqueIdFor(LOUNGE)]: {
                    number: numbers[LOUNGE]!,
                    role: "dimmableLight",
                    label: "Lounge Lamp",
                },
            },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(handWritten));

        const session = await boot(storagePath);
        try {
            assert.deepEqual(
                session.bridge.getStatus().endpoints.map(endpoint => endpoint.indigoDeviceId).sort(),
                [KITCHEN, LOUNGE].sort(),
                "serving two of three beats serving none",
            );
            assert.ok(
                session.logged.some(line => line.startsWith("Could not restore endpoint ")),
                `the failure must be named, not swallowed: ${session.logged.join(" | ")}`,
            );
            assert.ok(
                session.logged.some(line => line.startsWith("Restored 2 of 3 endpoint(s)")),
                "and counted honestly",
            );
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
            [uniqueIdFor(KITCHEN)]: {
                number: numbers[KITCHEN],
                role: "onOffLight",
                label: "Kitchen Lamp",
                deviceId: KITCHEN,
            },
            [uniqueIdFor(LOUNGE)]: {
                number: numbers[LOUNGE],
                role: "dimmableLight",
                label: "Lounge Lamp",
                deviceId: LOUNGE,
            },
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

describe("issue #222: the aggregator is pinned at endpoint 1", () => {
    it("takes endpoint number 1, because it is added to the ServerNode before any child", async () => {
        // ⊗ Alexa requires the bridge's aggregator at endpoint 1 (node.ts's
        // `start()` doc comment). Nothing pinned that ordering decision until
        // now — a future edit that adds another endpoint to the ServerNode
        // before the aggregator would silently break Alexa discovery with no
        // test catching it.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            const aggregator = session.bridge.server.parts.find(part => part.id === "aggregator");
            assert.ok(aggregator !== undefined, "the aggregator must be a direct child of the root node");
            assert.equal(aggregator.number, 1);
        } finally {
            await session.close();
        }
    });
});

describe("issue #222: the endpoint-count advisory, softer than the 100 warning", () => {
    /** `count` freshly-specced onOffLight accessories, distinct from every other suite's ids. */
    function manySpecs(count: number): unknown[] {
        return Array.from({ length: count }, (_, i) => ({
            indigoDeviceId: 250000000 + i,
            role: "onOffLight",
            label: `Advisory ${i}`,
            reachable: true,
            states: { onOff: false },
            options: {},
        }));
    }

    it("stays quiet at exactly the advisory threshold — only PAST it counts", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "adv0", manySpecs(ENDPOINT_COUNT_ADVISORY));
            assert.ok(
                !session.logged.some(line => line.includes(`exceeds ${ENDPOINT_COUNT_ADVISORY}`)),
                "50 itself must not trip the advisory",
            );
        } finally {
            await session.close();
        }
    });

    it("logs an advisory once past 50, without also tripping the 100 warning", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "adv1", manySpecs(ENDPOINT_COUNT_ADVISORY + 1));
            assert.ok(
                session.logged.some(
                    line => line.startsWith(`${ENDPOINT_COUNT_ADVISORY + 1} exported endpoints exceeds`) &&
                        line.includes("community-reported"),
                ),
                "the advisory must fire once past 50, phrased as community-reported not a verdict",
            );
            assert.ok(
                !session.logged.some(line => line.includes("bite before memory does")),
                "the harder 100 warning's own wording must not ALSO fire",
            );
        } finally {
            await session.close();
        }
    });

    it("keeps the existing 100 warning, reworded to no longer call itself an advisory", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "adv2", manySpecs(ENDPOINT_COUNT_WARNING + 1));
            assert.ok(
                session.logged.some(
                    line => line.startsWith(`${ENDPOINT_COUNT_WARNING + 1} exported endpoints exceeds ` +
                        `${ENDPOINT_COUNT_WARNING}`) && line.includes("bite before memory does"),
                ),
                "the 100 warning must still fire",
            );
            assert.ok(
                !session.logged.some(line => line.includes("advisory")),
                "review fix: the harder warning's OWN wording must not call itself an advisory " +
                    "now that 50 has a real advisory of its own",
            );
            assert.ok(
                !session.logged.some(line => line.includes("community-reported")),
                "the softer 50 advisory must not ALSO fire once past 100",
            );
        } finally {
            await session.close();
        }
    });

    it("stays at the advisory tier at exactly 100 — only PAST it trips the harder warning", async () => {
        // Pins the `>` (not `>=`) comparison on ENDPOINT_COUNT_WARNING.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "adv3", manySpecs(ENDPOINT_COUNT_WARNING));
            assert.ok(
                session.logged.some(line => line.includes("community-reported")),
                "100 itself is still only the advisory tier",
            );
            assert.ok(
                !session.logged.some(line => line.includes("bite before memory does")),
                "100 itself must not trip the harder warning",
            );
        } finally {
            await session.close();
        }
    });

    it("crosses the advisory via one-at-a-time upsertEndpoint, not only a full reconcile", async () => {
        // #222 review: warnOnEndpointCount() used to live only in `reconcile`, so
        // growing the export list one device at a time via `upsert_endpoint` crossed
        // the threshold with no message until the next full reconcile happened.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "up0", manySpecs(ENDPOINT_COUNT_ADVISORY));
            session.logged.length = 0;
            await upsertOne(session.client, "up1", 260000000);
            assert.ok(
                session.logged.some(
                    line => line.startsWith(`${ENDPOINT_COUNT_ADVISORY + 1} exported endpoints exceeds`),
                ),
                "the advisory must fire from upsertEndpoint, not wait for the next reconcile",
            );
        } finally {
            await session.close();
        }
    });

    it("crosses the 100 warning via upsertEndpoint too", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "up2", manySpecs(ENDPOINT_COUNT_WARNING));
            session.logged.length = 0;
            await upsertOne(session.client, "up3", 270000000);
            assert.ok(
                session.logged.some(
                    line => line.startsWith(`${ENDPOINT_COUNT_WARNING + 1} exported endpoints exceeds`) &&
                        line.includes("bite before memory does"),
                ),
                "the harder warning must fire from upsertEndpoint too",
            );
        } finally {
            await session.close();
        }
    });

    it("does not re-log a standing count on every subsequent upsert", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "up4", manySpecs(ENDPOINT_COUNT_ADVISORY + 1)); // fires once
            session.logged.length = 0;
            await upsertOne(session.client, "up5", 280000000);
            await upsertOne(session.client, "up6", 280000001);
            assert.ok(
                !session.logged.some(line => line.includes("exported endpoints exceeds")),
                "a standing count above an already-logged tier must not re-log on every upsert",
            );
        } finally {
            await session.close();
        }
    });

    it("logs the harder tier after the softer one already fired, on the SAME session", async () => {
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "up7", manySpecs(ENDPOINT_COUNT_ADVISORY + 1)); // tier 1 fires
            assert.ok(session.logged.some(line => line.includes("community-reported")));
            await attach(session.client, "up8", manySpecs(ENDPOINT_COUNT_WARNING + 1)); // now tier 2
            assert.ok(
                session.logged.some(
                    line => line.startsWith(`${ENDPOINT_COUNT_WARNING + 1} exported endpoints exceeds`) &&
                        line.includes("bite before memory does"),
                ),
                "each tier is reported once, not the pair as a single unit",
            );
        } finally {
            await session.close();
        }
    });
});

describe("issue #220: a battery survives a restart", () => {
    it("rebuilds the endpoint with PowerSource BEFORE the plugin attaches", async () => {
        // ⊗ node.ts's `restoreEndpoints` reads `entry.battery` off the map —
        // the one line that stops a battery accessory losing PowerSource on
        // every restart. This restore path never goes through
        // `attach`/`upsert_endpoint`, so `battery` is never asked for
        // elsewhere; without that line the endpoint would rebuild bare, and
        // PowerSource — a cluster that can only be declared at construction —
        // would never come back without a remove/re-add the user has no way
        // to trigger. `boot()` here does NOT attach, so this pins the state
        // strictly before the plugin has said anything.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);
        const handWritten: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: {
                [uniqueIdFor(KITCHEN)]: {
                    number: numbers[KITCHEN]!,
                    role: "onOffLight",
                    label: "Kitchen Lamp",
                    battery: true,
                },
                [uniqueIdFor(LOUNGE)]: {
                    number: numbers[LOUNGE]!,
                    role: "dimmableLight",
                    label: "Lounge Lamp",
                },
            },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(handWritten));

        const session = await boot(storagePath);
        try {
            const kitchen = childOf(session.bridge.server, KITCHEN);
            assert.ok(kitchen !== undefined, "the battery entry must restore at all");
            const kitchenServers = (kitchen!.stateOf("descriptor") as { serverList: unknown[] }).serverList;
            assert.ok(
                kitchenServers.map(Number).includes(47),
                `serverList ${JSON.stringify(kitchenServers)} missing PowerSource (47) on restore`,
            );

            const lounge = childOf(session.bridge.server, LOUNGE);
            const loungeServers = (lounge!.stateOf("descriptor") as { serverList: unknown[] }).serverList;
            assert.ok(
                !loungeServers.map(Number).includes(47),
                "a non-battery entry must not gain PowerSource",
            );
        } finally {
            await session.close();
        }
    });
});

describe("issues #219/#240 at node level: re-adopt and the two-command supersede", () => {
    it("logs the re-adopt line and rebinds deviceId when a different device claims the same identity", async () => {
        // #219: a device is deleted, its export is un-exported (`remove_endpoint`),
        // and a REPLACEMENT device claims the same published identity by name —
        // exactly what the (future) Re-adopt menu action sends. Nothing on the
        // wire should move: same `Endpoint.id`, same number (PR5 design F1), same UniqueID.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "a0", [KITCHEN_SPEC]);
            const numberBefore = numbersOf(session.bridge)[KITCHEN];

            session.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "r0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }
            session.logged.length = 0;

            const REPLACEMENT_DEVICE_ID = 700_000_001;
            session.client.send({
                message_id: "u0",
                command: "upsert_endpoint",
                args: {
                    endpoint: {
                        indigoDeviceId: REPLACEMENT_DEVICE_ID,
                        publishedAs: uniqueIdFor(KITCHEN),
                        role: "onOffLight",
                        label: "Kitchen Lamp (new)",
                        reachable: true,
                        states: { onOff: false },
                        options: {},
                    },
                },
            });
            let upsertFrame: Record<string, unknown> | undefined;
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "u0") {
                    upsertFrame = frame;
                    break;
                }
            }
            assert.equal(upsertFrame?.error_code, undefined, JSON.stringify(upsertFrame));
            assert.equal(
                (upsertFrame?.result as { endpointNumber: number } | undefined)?.endpointNumber,
                numberBefore,
                "PR5 design F1: the SAME Endpoint.id gets the SAME number back",
            );

            assert.deepEqual(
                session.bridge.getStatus().endpoints.map(endpoint => endpoint.indigoDeviceId),
                [REPLACEMENT_DEVICE_ID],
            );
            const map = readMap(storagePath);
            assert.equal(map.endpoints[uniqueIdFor(KITCHEN)]?.deviceId, REPLACEMENT_DEVICE_ID);
            assert.equal(map.endpoints[uniqueIdFor(KITCHEN)]?.orphaned, undefined, "live again, so un-orphaned");

            assert.ok(
                session.logged.some(
                    line =>
                        line.includes(`Accessory ${uniqueIdFor(KITCHEN)}`) &&
                        line.includes(`is now driven by Indigo device ${REPLACEMENT_DEVICE_ID}`) &&
                        line.includes(`replacing device ${KITCHEN}`),
                ),
                `expected the re-adopt log line, got ${session.logged.join(" | ")}`,
            );
        } finally {
            await session.close();
        }
    });

    it("nudges towards Re-adopt when an attach creates a NEW identity matching an orphan's role+label (PR5 design owner ruling 4)", async () => {
        // Distinct from the re-adopt test above: here the REPLACEMENT device is
        // exported under its OWN brand-new identity (not the orphan's), the way
        // an ordinary export naturally would be if the user never knew the old
        // accessory was still sitting in the map. The node cannot act on this —
        // only the plugin owns publishedAs — so the best it can do is say so.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "a0", [KITCHEN_SPEC]);

            session.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "r0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }
            session.logged.length = 0;

            const REPLACEMENT_DEVICE_ID = 700_000_002;
            session.client.send({
                message_id: "u0",
                command: "upsert_endpoint",
                args: {
                    endpoint: {
                        indigoDeviceId: REPLACEMENT_DEVICE_ID,
                        // No publishedAs — the default derivation, a BRAND-NEW
                        // identity distinct from the orphaned KITCHEN one.
                        role: KITCHEN_SPEC.role,
                        label: KITCHEN_SPEC.label,
                        reachable: true,
                        states: { onOff: false },
                        options: {},
                    },
                },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "u0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }

            assert.ok(
                session.logged.some(
                    line =>
                        line.includes("Re-adopt a Matter accessory…") &&
                        line.includes(uniqueIdFor(KITCHEN)) &&
                        line.includes(uniqueIdFor(REPLACEMENT_DEVICE_ID)),
                ),
                `expected the readopt nudge naming both identities, got ${session.logged.join(" | ")}`,
            );
            // ...and it has to leave stdout. The node is launched by launchd,
            // so `session.logged` is a terminal nobody is watching; §4.3
            // `warnings` is what `export_bridge.py` mirrors into the Indigo
            // event log, and this nudge is the whole discoverability moment
            // for `Re-adopt a Matter accessory…`.
            assert.ok(
                session.bridge
                    .getStatus()
                    .warnings.some(line => line.includes("Re-adopt a Matter accessory…")),
                `the nudge must ride StatusReport.warnings, got ${JSON.stringify(
                    session.bridge.getStatus().warnings,
                )}`,
            );
        } finally {
            await session.close();
        }
    });

    it("marks the old identity supersededBy the new one when remove and create arrive as two separate commands", async () => {
        // #240 §3 steps 3/5: the plugin's future `replace()` sends
        // `remove_endpoint` then `upsert_endpoint` as two SEPARATE commands, not
        // one `attach` batch — this is the two-command half of a role change,
        // and it is the ordinary path a role change actually takes.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "a0", [KITCHEN_SPEC]);

            session.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "r0") break;
            }

            // No supersededBy yet — the create has not happened (§3 step 3).
            assert.equal(readMap(storagePath).endpoints[uniqueIdFor(KITCHEN)]?.supersededBy, undefined);

            const newIdentity = `${uniqueIdFor(KITCHEN)}~2`;
            session.client.send({
                message_id: "u0",
                command: "upsert_endpoint",
                args: {
                    endpoint: {
                        indigoDeviceId: KITCHEN,
                        publishedAs: newIdentity,
                        role: "dimmableLight",
                        label: "Kitchen Lamp",
                        reachable: true,
                        states: { onOff: false, level: 50 },
                        options: {},
                    },
                },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "u0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }

            const map = readMap(storagePath);
            assert.equal(map.endpoints[uniqueIdFor(KITCHEN)]?.orphaned, true);
            assert.equal(
                map.endpoints[uniqueIdFor(KITCHEN)]?.supersededBy,
                newIdentity,
                "the create half landed as a SEPARATE command, and still paired with the removal",
            );
            assert.equal(map.endpoints[newIdentity]?.deviceId, KITCHEN);
        } finally {
            await session.close();
        }
    });

    it("leaves the interim identity an ORDINARY orphan when a re-adopt replaces it (PR5 design E5)", async () => {
        // PR5 design E2/E5: the device was recreated and re-exported under its OWN
        // identity before the user noticed the empty room, so re-adopt has to
        // remove THAT accessory and publish the orphaned one in its place.
        // That is one removal plus one create for one device — the same shape
        // as a supersede — but it is NOT one: the identity left behind was
        // never replaced by a later generation of itself, its number is not
        // retired, and PR5 design E5 rules it "itself re-adoptable later, which is
        // harmless". Marking it superseded would hide it from the picker for
        // good.
        const storagePath = storage();
        const session = await boot(storagePath);
        const answered = async (messageId: string): Promise<void> => {
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === messageId) {
                    // `error_code`, not `error` — the refusal frame's own key
                    // (`ws-server.ts`'s `sendError`).
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    return;
                }
            }
        };
        try {
            await attach(session.client, "a0", [KITCHEN_SPEC]);

            // The original accessory is un-exported: its identity is now an
            // orphan, and its number is held for it (§3.3).
            session.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            await answered("r0");

            // The user recreates the device and exports it normally — a
            // SECOND, brand-new accessory under its own default identity.
            const REPLACEMENT_DEVICE_ID = 700_000_004;
            await upsertOne(session.client, "u0", REPLACEMENT_DEVICE_ID);

            // Then notices the empty room and re-adopts, which
            // `ExportBridge.replace()` sends as remove-then-upsert.
            session.client.send({
                message_id: "r1",
                command: "remove_endpoint",
                args: { indigoDeviceId: REPLACEMENT_DEVICE_ID },
            });
            await answered("r1");
            session.client.send({
                message_id: "u1",
                command: "upsert_endpoint",
                args: {
                    endpoint: {
                        indigoDeviceId: REPLACEMENT_DEVICE_ID,
                        publishedAs: uniqueIdFor(KITCHEN),
                        role: "onOffLight",
                        label: "Kitchen Lamp (new)",
                        reachable: true,
                        states: { onOff: false },
                        options: {},
                    },
                },
            });
            await answered("u1");

            const interim = uniqueIdFor(REPLACEMENT_DEVICE_ID);
            const record = readMap(storagePath).endpoints[interim];
            assert.equal(record?.orphaned, true, "the interim accessory did leave the ecosystems");
            assert.equal(
                record?.supersededBy,
                undefined,
                "but nothing SUPERSEDED it — no later generation of it was ever published",
            );
            assert.ok(
                session.bridge.listOrphans().some(orphan => orphan.uniqueId === interim),
                `§3.12 must go on offering it (PR5 design E5), got ${JSON.stringify(session.bridge.listOrphans())}`,
            );
        } finally {
            await session.close();
        }
    });

    it("does not pair an unrelated upsert against a stale removal of a DIFFERENT device", async () => {
        // The `#lastRemoved` bookkeeping is keyed per device id — removing
        // KITCHEN must never taint a plain, unrelated create for some other,
        // brand-new device.
        const storagePath = storage();
        const session = await boot(storagePath);
        try {
            await attach(session.client, "a0", [KITCHEN_SPEC]);

            session.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "r0") break;
            }

            const UNRELATED_DEVICE_ID = 700_000_003;
            await upsertOne(session.client, "u0", UNRELATED_DEVICE_ID);

            assert.equal(readMap(storagePath).endpoints[uniqueIdFor(UNRELATED_DEVICE_ID)]?.supersededBy, undefined);
            assert.equal(
                readMap(storagePath).endpoints[uniqueIdFor(KITCHEN)]?.supersededBy,
                undefined,
                "KITCHEN's own removal must also stay an ordinary orphan — nothing paired with it",
            );
        } finally {
            await session.close();
        }
    });
});

describe("restore binds to the DRIVING device, and never to a retired identity (issues #219/#240)", () => {
    it("restores an accessory bound to the map's deviceId, not to its key", async () => {
        // The whole of #219 in the one place nothing else covers: `restore()`
        // runs BEFORE any attach, from the file alone, so a bug here is a
        // re-adopted accessory that comes back bound to the DELETED device —
        // commands routed nowhere and state pushed by nobody, silently, until
        // the plugin's next attach happens to correct it.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);
        const REPLACEMENT_DEVICE_ID = 700_000_010;
        const handWritten: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: {
                // Key = the accessory's identity (the OLD device's derivation);
                // deviceId = who drives it now. They deliberately disagree.
                [uniqueIdFor(KITCHEN)]: {
                    number: numbers[KITCHEN]!,
                    role: "onOffLight",
                    label: "Kitchen Lamp",
                    deviceId: REPLACEMENT_DEVICE_ID,
                },
            },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(handWritten));

        const session = await boot(storagePath);
        try {
            // The accessory keeps the identity (so every ecosystem keeps the
            // room) but reports the NEW driving device.
            assert.deepEqual(
                session.bridge.getStatus().endpoints.map(endpoint => ({
                    indigoDeviceId: endpoint.indigoDeviceId,
                    publishedAs: endpoint.publishedAs,
                    endpointNumber: endpoint.endpointNumber,
                })),
                [{
                    indigoDeviceId: REPLACEMENT_DEVICE_ID,
                    publishedAs: uniqueIdFor(KITCHEN),
                    endpointNumber: numbers[KITCHEN],
                }],
            );

            // The rest needs an attached client (`set_state` and the §5
            // command event both require one), so attach with exactly what a
            // re-adopted export sends. It must be an UPDATE of the restored
            // endpoint, not a recreate — the number proves which.
            await attach(session.client, "a0", [{
                ...KITCHEN_SPEC,
                indigoDeviceId: REPLACEMENT_DEVICE_ID,
                publishedAs: uniqueIdFor(KITCHEN),
                states: { onOff: false },
            }]);
            assert.deepEqual(numbersOf(session.bridge), { [REPLACEMENT_DEVICE_ID]: numbers[KITCHEN] });

            // State pushed for the NEW device reaches the OLD identity's
            // accessory — the half an ecosystem reads.
            session.client.send({
                message_id: "s0",
                command: "set_state",
                args: { indigoDeviceId: REPLACEMENT_DEVICE_ID, states: { onOff: true } },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "s0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }
            assert.equal(onOffOf(session.bridge.server, KITCHEN), true);

            // ...and a command originating in an ecosystem comes back named
            // for the NEW device — the half Indigo acts on.
            const accessory = childOf(session.bridge.server, KITCHEN);
            assert.ok(accessory !== undefined);
            await accessory!.act(agent => (agent.get(OnOffLighting) as { off: () => void }).off());
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.event === "command") {
                    assert.deepEqual(frame.data, {
                        indigoDeviceId: REPLACEMENT_DEVICE_ID,
                        command: "onOff",
                        args: { value: false },
                    });
                    break;
                }
            }
        } finally {
            await session.close();
        }
    });

    it("does not restore an identity the map records as superseded", async () => {
        // `restorable()` filters `supersededBy` in its own right, and this is
        // the level that proves it matters: rebuilding a retired identity puts
        // an OLD-ROLE accessory back under a number every paired ecosystem has
        // already processed a removal for — the wedge #240 exists to remove,
        // re-created at every restart with no plugin involved at all.
        const storagePath = storage();
        const numbers = await seedTwoAccessories(storagePath);
        const handWritten: EndpointMapFile = {
            version: ENDPOINT_MAP_VERSION,
            endpoints: {
                [uniqueIdFor(KITCHEN)]: {
                    number: numbers[KITCHEN]!,
                    role: "onOffLight",
                    label: "Kitchen Lamp",
                    orphaned: true,
                    supersededBy: `${uniqueIdFor(KITCHEN)}~2`,
                },
                // Deliberately WITHOUT `orphaned`, so the skip cannot be
                // riding on that marker instead.
                [uniqueIdFor(LOUNGE)]: {
                    number: numbers[LOUNGE]!,
                    role: "dimmableLight",
                    label: "Lounge Lamp",
                    supersededBy: `${uniqueIdFor(LOUNGE)}~2`,
                },
            },
        };
        writeFileSync(join(storagePath, ENDPOINT_MAP_FILE), JSON.stringify(handWritten));

        const session = await boot(storagePath);
        try {
            assert.deepEqual(session.bridge.getStatus().endpoints, []);
            assert.equal(childOf(session.bridge.server, KITCHEN), undefined);
            assert.equal(childOf(session.bridge.server, LOUNGE), undefined);
        } finally {
            await session.close();
        }
    });

    it("still offers an ordinary orphan after a restart when no create ever paired with it (PR5 design E7)", async () => {
        // E7: the bridge or plugin dies between a supersede's remove and its
        // add. `#lastRemoved` is in-memory only, so the pairing is lost — and
        // the ruling is that the survivor must be an ORDINARY orphan, offered
        // by §3.12 like any other, rather than a speculatively-retired one.
        const storagePath = storage();
        const first = await boot(storagePath);
        try {
            await attach(first.client, "a0", [KITCHEN_SPEC]);
            first.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            for (;;) {
                const frame = await first.client.next(10_000);
                if (frame.message_id === "r0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }
        } finally {
            await first.close();
        }
        // The create never happens — the process ends here.

        const second = await boot(storagePath);
        try {
            assert.equal(
                readMap(storagePath).endpoints[uniqueIdFor(KITCHEN)]?.supersededBy,
                undefined,
                "a supersession marker must never be written speculatively",
            );
            assert.deepEqual(
                second.bridge.listOrphans().map(orphan => orphan.uniqueId),
                [uniqueIdFor(KITCHEN)],
                `§3.12 must go on offering it, got ${JSON.stringify(second.bridge.listOrphans())}`,
            );
            assert.equal(
                childOf(second.bridge.server, KITCHEN),
                undefined,
                "offered for re-adoption, but NOT rebuilt — it is orphaned",
            );
        } finally {
            await second.close();
        }
    });

    it("supersedes a SECOND time: indigo-N~2 retires when indigo-N~3 arrives", async () => {
        // The gap a "does the new identity have a suffix?" test would leave:
        // a role change on an already-role-changed export. Driven through the
        // two-command path, which is the one a real `replace()` takes.
        const storagePath = storage();
        const session = await boot(storagePath);
        const GEN2 = `${uniqueIdFor(KITCHEN)}~2`;
        const GEN3 = `${uniqueIdFor(KITCHEN)}~3`;
        try {
            await attach(session.client, "a0", [{ ...KITCHEN_SPEC, publishedAs: GEN2 }]);

            session.client.send({
                message_id: "r0",
                command: "remove_endpoint",
                args: { indigoDeviceId: KITCHEN },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "r0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }

            session.client.send({
                message_id: "u0",
                command: "upsert_endpoint",
                args: {
                    endpoint: {
                        indigoDeviceId: KITCHEN,
                        publishedAs: GEN3,
                        role: "dimmableLight",
                        label: "Kitchen Lamp",
                        reachable: true,
                        states: { onOff: false, level: 40 },
                        options: {},
                    },
                },
            });
            for (;;) {
                const frame = await session.client.next(10_000);
                if (frame.message_id === "u0") {
                    assert.equal(frame.error_code, undefined, JSON.stringify(frame));
                    break;
                }
            }

            const map = readMap(storagePath);
            assert.equal(map.endpoints[GEN2]?.supersededBy, GEN3);
            assert.equal(map.endpoints[GEN2]?.orphaned, true);
            assert.deepEqual(
                session.bridge.listOrphans().map(orphan => orphan.uniqueId),
                [],
                "a retired identity is never offered for re-adoption",
            );
        } finally {
            await session.close();
        }
    });
});
