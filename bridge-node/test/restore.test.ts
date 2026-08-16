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
            afterRemoval.endpoints[uniqueIdFor(LOUNGE)],
            { number: numbers[LOUNGE], role: "dimmableLight", label: "Lounge Lamp", orphaned: true },
            "the number AND role/label survive (§3.3, #219) so a re-export returns the SAME " +
                "accessory and the entry remains re-adopt evidence, but `orphaned` keeps it " +
                "out of the pre-attach rebuild",
        );
        assert.deepEqual(
            afterRemoval.endpoints[uniqueIdFor(KITCHEN)],
            { number: numbers[KITCHEN], role: "onOffLight", label: "Kitchen Lamp" },
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
        });
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

        assert.deepEqual(readMap(storagePath).endpoints[uniqueIdFor(LOUNGE)], {
            number: numbers[LOUNGE],
            role: "dimmableLight",
            label: "Lounge Lamp",
            orphaned: true,
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
            [uniqueIdFor(KITCHEN)]: { number: numbers[KITCHEN], role: "onOffLight", label: "Kitchen Lamp" },
            [uniqueIdFor(LOUNGE)]: { number: numbers[LOUNGE], role: "dimmableLight", label: "Lounge Lamp" },
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
                    assert.equal(frame.error, undefined, JSON.stringify(frame.error));
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
        // bridge boots empty, which is the bug. `indigo-0123` and `indigo-123`
        // are different map keys that parse to the SAME device id, so the second
        // one to be built collides on `Endpoint.id` inside matter.js: a spec
        // that passes every check node.ts can make and still fails to add.
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
                [`indigo-0${KITCHEN}`]: { number: 902, role: "onOffLight", label: "Collides" },
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
