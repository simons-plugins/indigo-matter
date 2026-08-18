/**
 * Endpoint CRUD against a **real, un-commissioned** matter.js ServerNode.
 *
 * The protocol tests deliberately stub the Matter stack out; these do not,
 * because the things E3 has to get right — that each role's device type builds
 * at all, that `Endpoint.id` fixes the number, that a local `set_state` write
 * arrives in an offline context — are all properties of matter.js 0.17.8 and of
 * nothing we wrote. A double would only ever confirm our own assumptions.
 *
 * Each node gets its own {@link Environment} and its own scratch storage
 * directory: matter.js takes an exclusive lock per storage path, and
 * `Environment.default` is a process-wide singleton whose `storage.path` the
 * nodes would otherwise fight over.
 */

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, describe, it } from "node:test";

import { Endpoint, type EndpointType, Environment, Logger, ServerNode, VendorId } from "@matter/main";
import { BasicInformationServer } from "@matter/main/behaviors/basic-information";
import { BridgedDeviceBasicInformationServer } from "@matter/main/behaviors/bridged-device-basic-information";
import { ColorControlServer } from "@matter/main/behaviors/color-control";
import { DoorLockServer } from "@matter/main/behaviors/door-lock";
import { LevelControlServer } from "@matter/main/behaviors/level-control";
import { OnOffServer } from "@matter/main/behaviors/on-off";
import { PowerSourceServer } from "@matter/main/behaviors/power-source";
import { WindowCoveringServer } from "@matter/main/behaviors/window-covering";
import { ColorControl } from "@matter/main/clusters/color-control";
import { DoorLock } from "@matter/main/clusters/door-lock";
import { LevelControl } from "@matter/main/clusters/level-control";
import { ColorTemperatureLightDevice } from "@matter/main/devices/color-temperature-light";
import { DimmableLightDevice } from "@matter/main/devices/dimmable-light";
import { ExtendedColorLightDevice } from "@matter/main/devices/extended-color-light";
import { OnOffLightDevice } from "@matter/main/devices/on-off-light";
import { AggregatorEndpoint } from "@matter/main/endpoints/aggregator";

import {
    applyStates,
    endpointIdFor,
    percentToBatteryRemaining,
    percentToCurrentLevel,
    SUPPORTED_ROLES,
    uniqueIdFor,
    watchCommands,
} from "../src/endpoints.js";
import {
    type CommandEventData,
    type EndpointSpec,
    ErrorCode,
    ProtocolError,
    Role,
    type RoleValue,
} from "../src/protocol.js";
import { EndpointRegistry } from "../src/registry.js";

const PRODUCT_NAME = "Indigo Matter Bridge";

/**
 * The bridge identity every child endpoint publishes, as node.ts builds it.
 * Spelled out here rather than imported from node.ts so these tests keep their
 * distance from the ServerNode wiring.
 *
 * **This literal pins nothing about node.ts, and used to claim it did.** It is a
 * hand-written copy, so it and `node.ts`'s `bridgedIdentity` agreed with each
 * other and with nothing else — publishing Apple's vendor id on every bridged
 * accessory left this suite green. What these tests actually pin is that the
 * registry PROPAGATES whatever identity it is given; that the identity is the
 * root node's own is pinned in `integration.test.ts`, against a real ServerNode.
 */
const BRIDGE_IDENTITY = {
    vendorName: "simons-plugins",
    vendorId: 0xfff1,
    productName: PRODUCT_NAME,
    hardwareVersion: 1,
    hardwareVersionString: "1",
    softwareVersion: 500,
    softwareVersionString: "0.5.0",
};
const scratchRoots: string[] = [];

/**
 * Scratch storage root. Honour the harness-provided scratchpad when the runner
 * sets one; otherwise a temp directory, so `npm test` works anywhere.
 */
const SCRATCH_ROOT = process.env.INDIGO_MATTER_TEST_SCRATCH ?? tmpdir();
mkdirSync(SCRATCH_ROOT, { recursive: true });

// matter.js logs a screenful per endpoint at its default level, which buries the
// TAP output. Fatal keeps a genuine stack visible without the narration.
Logger.level = "fatal";

after(() => {
    for (const root of scratchRoots) {
        rmSync(root, { recursive: true, force: true });
    }
});

interface Harness {
    registry: EndpointRegistry;
    aggregator: Endpoint;
    node: ServerNode;
    commands: CommandEventData[];
    logs: string[];
    storagePath: string;
    close: () => Promise<void>;
}

/**
 * Build a live node. The Matter port is 0 — matter.js binds an ephemeral one,
 * so a parallel run cannot collide — and the node is never started, because
 * nothing here needs the network: endpoint numbers, state and change events are
 * all assigned at `add()` time.
 */
async function harness(
    options: { storagePath?: string; removalPacingMs?: number; emitThrows?: boolean } = {},
): Promise<Harness> {
    const storagePath = options.storagePath ?? mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-registry-"));
    if (options.storagePath === undefined) {
        scratchRoots.push(storagePath);
    }

    const environment = new Environment("test", Environment.default);
    environment.vars.set("storage.path", storagePath);
    environment.vars.set("runtime.signals", false);

    const node = await ServerNode.create({
        id: "indigo-matter-bridge",
        environment,
        network: { port: 0 },
        commissioning: { passcode: 20202021, discriminator: 3840 },
        productDescription: { name: PRODUCT_NAME, deviceType: AggregatorEndpoint.deviceType },
        basicInformation: {
            vendorName: "simons-plugins",
            vendorId: VendorId(0xfff1),
            productName: PRODUCT_NAME,
            productId: 0x8000,
            serialNumber: "testserial000001",
            uniqueId: "testuniqueid00000000000000000001",
        },
    });
    const aggregator = new Endpoint(AggregatorEndpoint, { id: "aggregator" });
    await node.add(aggregator);

    const commands: CommandEventData[] = [];
    const logs: string[] = [];
    const registry = new EndpointRegistry({
        aggregator,
        bridgeIdentity: BRIDGE_IDENTITY,
        emit: data => {
            commands.push(data);
            if (options.emitThrows === true) {
                throw new Error("the command sink blew up");
            }
        },
        log: message => logs.push(message),
        removalPacingMs: options.removalPacingMs ?? 0,
        // The real wiring from node.ts, not a counter: the point of testing it
        // here is that matter.js accepts the call at all.
        onConfigurationChange: async () => {
            await node.act(agent => agent.get(BasicInformationServer).increaseConfigurationVersion());
        },
    });

    return {
        registry,
        aggregator,
        node,
        commands,
        logs,
        storagePath,
        close: async () => {
            registry.close();
            await node.close();
        },
    };
}

function spec(indigoDeviceId: number, role: RoleValue, overrides: Partial<EndpointSpec> = {}): EndpointSpec {
    return {
        indigoDeviceId,
        role,
        label: `Device ${indigoDeviceId}`,
        reachable: true,
        states: {},
        options: {},
        battery: false,
        // Today's default derivation (issues #219/#240) — a test that cares
        // about a different published identity overrides it explicitly.
        publishedAs: uniqueIdFor(indigoDeviceId),
        ...overrides,
    };
}

/**
 * A role that is in no version of the §4.2 enum.
 *
 * E4 completed the v1 table, so there is no longer a *lawful* role the node
 * cannot build — but the refusal path is not dead code: PM-B deliberately keeps
 * an old bridge node alive across plugin reloads, so "a role this build has
 * never heard of" is exactly what a newer plugin sends an older node. The cast
 * is the only way to express that from inside the current typings, and it is
 * what the refusal tests below use in place of the E4 roles they used to name.
 */
const FUTURE_ROLE = "waterValve" as RoleValue;

async function rejects(run: () => Promise<unknown>, code: string): Promise<ProtocolError> {
    try {
        await run();
    } catch (error) {
        assert.ok(error instanceof ProtocolError, `expected a ProtocolError, got ${String(error)}`);
        assert.equal(error.code, code);
        return error;
    }
    throw new Error(`expected a ${code} refusal`);
}

describe("role factory", () => {
    it("builds every E3 role with bridged info and its own clusters", async () => {
        const h = await harness();
        try {
            const roles: RoleValue[] = [
                Role.onOffPlugInUnit,
                Role.onOffLight,
                Role.dimmableLight,
                Role.colorTemperatureLight,
                Role.extendedColorLight,
            ];
            await h.registry.reconcile(
                roles.map((role, index) => spec(900_001 + index, role, { label: `L${index}` })),
                false,
            );

            const byRole = new Map(h.registry.summaries().map(s => [s.role, s]));
            assert.deepEqual([...byRole.keys()].sort(), [...roles].sort());

            for (const [index, role] of roles.entries()) {
                const id = 900_001 + index;
                const endpoint = [...h.aggregator.parts].find(part => part.id === endpointIdFor(id));
                assert.ok(endpoint !== undefined, `${role} endpoint missing`);

                const info = endpoint.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;
                assert.equal(info.nodeLabel, `L${index}`);
                assert.equal(info.uniqueId, uniqueIdFor(id));
                assert.equal(info.reachable, true);
                // Matter rejects a UniqueID equal to the SerialNumber.
                assert.notEqual(info.uniqueId, info.serialNumber);
                // ConfigurationVersion must be seeded or matter.js refuses to
                // increment it later.
                assert.equal(info.configurationVersion, 1);

                const behaviors = Object.keys(endpoint.state);
                assert.ok(behaviors.includes("onOff"), `${role} has no OnOff`);
                const needsLevel = role !== Role.onOffPlugInUnit && role !== Role.onOffLight;
                assert.equal(behaviors.includes("levelControl"), needsLevel, `${role} levelControl`);
                const needsColor = role === Role.colorTemperatureLight || role === Role.extendedColorLight;
                assert.equal(behaviors.includes("colorControl"), needsColor, `${role} colorControl`);
            }
        } finally {
            await h.close();
        }
    });

    it("gives extendedColorLight the HueSaturation feature matter.js omits", async () => {
        // matter.js 0.17.8 builds ExtendedColorLightDevice with Xy+ColorTemperature
        // only, so without the override §4.2's hue/saturation vocabulary would
        // have no attributes to write.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);
            const features = endpoint.featuresOf("colorControl") as Record<string, boolean>;
            assert.equal(features.hueSaturation, true);
            assert.equal(features.colorTemperature, true);
            const color = endpoint.stateOf("colorControl") as Record<string, unknown>;
            assert.equal(typeof color.currentHue, "number");
            assert.equal(typeof color.currentSaturation, "number");
        } finally {
            await h.close();
        }
    });

    it("builds a role whose spec already carries its full initial state", async () => {
        // A plain object spread would replace the whole colorControl patch here,
        // dropping the mandatory attributes matter.js needs, and the endpoint
        // would fail construction with a bare "Behaviors have errors". Every
        // other test in this file passes `states: {}`, which is why this one
        // exists: the failure only shows when a spec supplies colour state.
        const h = await harness();
        try {
            await h.registry.reconcile(
                [
                    spec(1, Role.dimmableLight, { states: { onOff: true, level: 60 } }),
                    spec(2, Role.colorTemperatureLight, { states: { onOff: true, level: 30, colorTempMireds: 250 } }),
                    spec(3, Role.extendedColorLight, {
                        states: { onOff: true, level: 100, colorTempMireds: 320, hue: 210, saturation: 80 },
                    }),
                ],
                false,
            );
            assert.equal(h.registry.size, 3);

            const ext = [...h.aggregator.parts].find(part => part.id === endpointIdFor(3));
            const color = ext?.stateOf("colorControl") as Record<string, unknown>;
            assert.equal(color.currentHue, 148);
            assert.equal(color.currentSaturation, 203);
            assert.equal(color.colorTempPhysicalMinMireds, 153);
            assert.equal(color.colorTempPhysicalMaxMireds, 500);
            assert.equal((ext?.stateOf("levelControl") as Record<string, unknown>).currentLevel, 254);
        } finally {
            await h.close();
        }
    });

    it("refuses a role this bridge version cannot build", async () => {
        const h = await harness();
        try {
            const error = await rejects(
                () => h.registry.reconcile([spec(1, FUTURE_ROLE)], false),
                ErrorCode.internal,
            );
            assert.match(error.message, /not implemented by this bridge version/);
            // A role refusal is decided from the plan alone, so it is a genuine
            // gate — nothing was touched. (Failures *after* mutation starts get
            // no such promise; see the partial-reconcile test below.)
            assert.equal(h.registry.size, 0);
        } finally {
            await h.close();
        }
    });
});

describe("reconcile (§3.1)", () => {
    it("creates, updates and removes in one pass", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.onOffLight)], false);
            assert.deepEqual(h.registry.summaries().map(s => [s.indigoDeviceId, s.endpointNumber]), [
                [1, 2],
                [2, 3],
            ]);

            await h.registry.reconcile(
                [spec(1, Role.onOffLight, { label: "Renamed", reachable: false }), spec(3, Role.dimmableLight)],
                false,
            );
            assert.deepEqual(h.registry.summaries().map(s => s.indigoDeviceId), [1, 3]);

            const renamed = [...h.aggregator.parts].find(part => part.id === endpointIdFor(1));
            const info = renamed?.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;
            assert.equal(info.nodeLabel, "Renamed");
            assert.equal(info.reachable, false);
        } finally {
            await h.close();
        }
    });

    it("refuses to empty a non-empty live set, and changes nothing when it does", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.onOffLight)], false);
            const error = await rejects(() => h.registry.reconcile([], false), ErrorCode.massRemovalRefused);
            assert.equal(error.message, "attach would remove all 2 live endpoints without intent: replace_all");
            assert.equal(h.registry.size, 2);
            assert.equal([...h.aggregator.parts].length, 2);
        } finally {
            await h.close();
        }
    });

    it("empties it when the client asks for replace_all", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.onOffLight)], false);
            await h.registry.reconcile([], true);
            assert.equal(h.registry.size, 0);
            assert.equal([...h.aggregator.parts].length, 0);
        } finally {
            await h.close();
        }
    });

    it("recreates an endpoint whose role changed", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.onOffLight)], false);
            await h.registry.reconcile([spec(1, Role.dimmableLight), spec(2, Role.onOffLight)], false);

            assert.deepEqual(h.registry.summaries().map(s => [s.indigoDeviceId, s.role]), [
                [1, Role.dimmableLight],
                [2, Role.onOffLight],
            ]);
            assert.ok(h.logs.some(line => line.includes("Recreating endpoint 1")));
            // The id is the identity, so the recreated endpoint keeps its number.
            assert.equal(h.registry.summaries()[0]?.endpointNumber, 2);
        } finally {
            await h.close();
        }
    });

    it("refuses a mixed set whole, leaving even the planned removals in place", async () => {
        // The dangerous shape: one supported role and one unbuildable role,
        // against a POPULATED live set. If the role check ever moved after planning — or
        // worse, into the per-spec loop — the removals would run first and the
        // user would lose accessories to a request that was refused anyway.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.dimmableLight)], false);
            const before = h.registry.summaries();

            await rejects(
                // 2 is dropped from the desired set, so it is a planned removal;
                // 3 is a role this build cannot construct.
                () => h.registry.reconcile([spec(1, Role.onOffLight), spec(3, FUTURE_ROLE)], false),
                ErrorCode.internal,
            );

            assert.deepEqual(h.registry.summaries(), before, "the live set moved under a refused reconcile");
            assert.equal([...h.aggregator.parts].length, 2, "the Matter tree moved under a refused reconcile");
        } finally {
            await h.close();
        }
    });

    it("refuses an upsert of an unbuildable role without touching the live set", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const before = h.registry.summaries();
            await rejects(() => h.registry.upsert(spec(2, FUTURE_ROLE)), ErrorCode.internal);
            assert.deepEqual(h.registry.summaries(), before);
            assert.equal([...h.aggregator.parts].length, 1);
        } finally {
            await h.close();
        }
    });

    it("accounts for a reconcile that fails after it has started mutating", async () => {
        // Once matter.js has been asked to add or close anything there is no
        // transaction to roll back to, so the contract is honesty rather than
        // atomicity: bump ConfigurationVersion because the set really did
        // change, and say how far it got and what is live now.
        const h = await harness();
        const version = (): number =>
            (h.node.state.basicInformation as { configurationVersion?: number }).configurationVersion ?? 0;
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.onOffLight)], false);
            const start = version();

            // 2 is removed (mutation happens), then 3 is created with states no
            // onOffLight can consume — a failure with the batch half-applied.
            await rejects(
                () =>
                    h.registry.reconcile(
                        [spec(1, Role.onOffLight), spec(3, Role.onOffLight, { states: { level: 50 } })],
                        false,
                    ),
                ErrorCode.malformedArgs,
            );

            assert.deepEqual(h.registry.summaries().map(s => s.indigoDeviceId), [1]);
            assert.equal(version(), start + 1, "a part-applied reconcile still changed the bridged-node set");
            const aborted = h.logs.filter(line => line.startsWith("reconcile aborted after"));
            assert.equal(aborted.length, 1, h.logs.join("\n"));
            assert.equal(aborted[0], "reconcile aborted after 1/3 operations; live set now [1] (requested [1, 3])");
        } finally {
            await h.close();
        }
    });

    it("paces bulk removals, and the pacing is injectable", async () => {
        const h = await harness({ removalPacingMs: 20 });
        try {
            await h.registry.reconcile([1, 2, 3].map(id => spec(id, Role.onOffLight)), false);
            const started = Date.now();
            await h.registry.reconcile([], true);
            // Three removals, two gaps. Asserted as a floor only: the point is
            // that the delay exists and is the injected one, not its precision.
            assert.ok(Date.now() - started >= 30, `bulk removal took ${Date.now() - started}ms`);
        } finally {
            await h.close();
        }
    });
});

describe("upsert / remove (§3.2, §3.3)", () => {
    it("creates when absent and returns the live endpoint number", async () => {
        const h = await harness();
        try {
            assert.deepEqual(await h.registry.upsert(spec(1, Role.onOffLight)), { endpointNumber: 2 });
            assert.deepEqual(await h.registry.upsert(spec(1, Role.onOffLight, { label: "Again" })), {
                endpointNumber: 2,
            });
        } finally {
            await h.close();
        }
    });

    it("rejects a role change with role_change (§4.1)", async () => {
        const h = await harness();
        try {
            await h.registry.upsert(spec(123456789, Role.onOffLight));
            const error = await rejects(
                () => h.registry.upsert(spec(123456789, Role.dimmableLight)),
                ErrorCode.roleChange,
            );
            assert.equal(error.message, "endpoint 123456789 is onOffLight; remove and re-add to change role");
            // Unchanged: the refusal must not half-apply the new role.
            assert.equal(h.registry.summaries()[0]?.role, Role.onOffLight);
        } finally {
            await h.close();
        }
    });

    it("removes idempotently", async () => {
        const h = await harness();
        try {
            await h.registry.upsert(spec(1, Role.onOffLight));
            assert.deepEqual(await h.registry.remove(1), { removed: true });
            assert.deepEqual(await h.registry.remove(1), { removed: false });
            assert.deepEqual(await h.registry.remove(999), { removed: false });
        } finally {
            await h.close();
        }
    });

    it("reports unknown_device for set_state and set_reachable", async () => {
        const h = await harness();
        try {
            const error = await rejects(() => h.registry.setState(123456791, { onOff: true }), ErrorCode.unknownDevice);
            assert.equal(error.message, "no live endpoint for indigoDeviceId 123456791");
            await rejects(() => h.registry.setReachable(123456791, false), ErrorCode.unknownDevice);
        } finally {
            await h.close();
        }
    });
});

/** Make one endpoint's `close()` fail, and return the switch that stops it. */
function breakClose(endpoint: Endpoint): { restore: () => void } {
    const real = endpoint.close.bind(endpoint);
    let broken = true;
    (endpoint as unknown as { close: () => Promise<void> }).close = async () => {
        if (broken) {
            throw new Error("matter.js refused to close the endpoint");
        }
        await real();
    };
    return {
        restore: () => {
            broken = false;
        },
    };
}

describe("a close that fails (§3.3)", () => {
    it("keeps the endpoint rather than orphaning it in the Matter tree", async () => {
        // The zombie this prevents: evict-then-close meant a throwing close left
        // an accessory that every paired ecosystem still shows and still routes
        // taps to, while the registry denied it existed — so its commands went
        // nowhere and the plugin's retry got `{removed: false}`, which §3.3
        // defines as *success*. Nothing anywhere would ever have reported it.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);
            const close = breakClose(endpoint);

            const error = await rejects(() => h.registry.remove(1), ErrorCode.internal);
            assert.match(error.message, /endpoint 1 failed to close/);
            assert.ok(
                h.logs.some(line => line.includes("registry and Matter tree may disagree")),
                h.logs.join("\n"),
            );

            // Still known, so a retry is an honest retry rather than a lie.
            assert.equal(h.registry.size, 1);
            // And still listening: the accessory is live in the ecosystem, so
            // the commands it produces must still reach the plugin.
            ecosystemWrite(endpoint, "onOff", "onOff", true, false);
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "onOff", args: { value: true } }]);

            close.restore();
            assert.deepEqual(await h.registry.remove(1), { removed: true });
            assert.equal(h.registry.size, 0);
        } finally {
            await h.close();
        }
    });

    it("aborts a paced batch mid-way with the failed endpoint still accounted for", async () => {
        const h = await harness({ removalPacingMs: 0 });
        try {
            await h.registry.reconcile([1, 2, 3].map(id => spec(id, Role.onOffLight)), false);
            const second = [...h.aggregator.parts].find(part => part.id === endpointIdFor(2));
            assert.ok(second !== undefined);
            breakClose(second);

            await rejects(() => h.registry.reconcile([], true), ErrorCode.internal);

            // 1 was removed, 2 failed and stayed, 3 was never attempted — and
            // the log says exactly that rather than implying a clean rollback.
            assert.deepEqual(h.registry.summaries().map(s => s.indigoDeviceId), [2, 3]);
            assert.ok(
                h.logs.includes("reconcile aborted after 1/3 operations; live set now [2, 3] (requested [])"),
                h.logs.join("\n"),
            );
        } finally {
            await h.close();
        }
    });
});

describe("one mutation at a time (§3.1)", () => {
    it("makes a second reconcile wait for the one in flight", async () => {
        // The real window: a plugin crash leaves a half-open socket, launchd
        // restarts the plugin, and its attach lands on a new socket while the
        // incumbent's paced reconcile is still seconds from finishing. Both
        // then diff and mutate the same live set. Asserted by log order,
        // because that is what shows the second one genuinely waited rather
        // than interleaving into a set that happens to look right at the end.
        const h = await harness({ removalPacingMs: 25 });
        try {
            await h.registry.reconcile([1, 2, 3].map(id => spec(id, Role.onOffLight)), false);
            h.logs.length = 0;

            const first = h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const second = h.registry.reconcile(
                [spec(1, Role.onOffLight), spec(4, Role.dimmableLight)],
                false,
            );
            await Promise.all([first, second]);

            assert.deepEqual(h.registry.summaries().map(s => s.indigoDeviceId), [1, 4]);
            assert.ok(
                h.logs.some(line => line === "reconcile is waiting for an unfinished reconcile to release the registry"),
                h.logs.join("\n"),
            );

            const firstDone = h.logs.findIndex(line => line.startsWith("Reconciled endpoints:"));
            const secondStarted = h.logs.findIndex(line => line.startsWith("Endpoint 4 (dimmableLight) added"));
            assert.ok(firstDone >= 0 && secondStarted >= 0, h.logs.join("\n"));
            assert.ok(
                firstDone < secondStarted,
                `the second reconcile mutated before the first finished:\n${h.logs.join("\n")}`,
            );
        } finally {
            await h.close();
        }
    });

    it("queues set_state and remove behind an in-flight reconcile", async () => {
        const h = await harness({ removalPacingMs: 25 });
        try {
            await h.registry.reconcile([1, 2, 3].map(id => spec(id, Role.dimmableLight)), false);
            h.logs.length = 0;

            const reconcile = h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            // 2 is about to be removed by the reconcile above. Without the
            // queue this races it; with it, the removal is simply already done
            // and §3.3 answers the idempotent `false`.
            const removal = h.registry.remove(2);
            const write = h.registry.setState(1, { level: 40 });
            await reconcile;
            assert.deepEqual(await removal, { removed: false });
            await write;

            const dim = [...h.aggregator.parts].find(part => part.id === endpointIdFor(1));
            assert.equal((dim?.stateOf("levelControl") as Record<string, unknown>).currentLevel, 102);
            assert.equal(
                h.logs.filter(line => line.includes("waiting for an unfinished reconcile")).length,
                2,
                h.logs.join("\n"),
            );
        } finally {
            await h.close();
        }
    });
});

describe("set_state (§3.4)", () => {
    it("writes each role's state keys through its converter", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile(
                [spec(1, Role.dimmableLight), spec(2, Role.extendedColorLight)],
                false,
            );
            await h.registry.setState(1, { onOff: true, level: 60 });
            await h.registry.setState(2, { onOff: true, level: 100, colorTempMireds: 9999, hue: 210, saturation: 80 });

            const dim = [...h.aggregator.parts].find(part => part.id === endpointIdFor(1));
            assert.equal((dim?.stateOf("onOff") as Record<string, unknown>).onOff, true);
            // 60% → round(60 × 254 / 100) = 152.
            assert.equal((dim?.stateOf("levelControl") as Record<string, unknown>).currentLevel, 152);

            const ext = [...h.aggregator.parts].find(part => part.id === endpointIdFor(2));
            const color = ext?.stateOf("colorControl") as Record<string, unknown>;
            assert.equal(color.colorTemperatureMireds, 500, "mireds should clamp to the physical maximum");
            assert.equal(color.currentHue, 148); // 210° → round(210 × 254 / 360)
            assert.equal(color.currentSaturation, 203); // 80% → round(80 × 254 / 100)
            // Hue and saturation win over a colour temperature sent alongside.
            assert.equal(color.colorMode, 0);
        } finally {
            await h.close();
        }
    });

    it("writes 0% as currentLevel 1, which reads back as 0%", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight, { states: { level: 100 } })], false);
            await h.registry.setState(1, { level: 0 });
            const dim = [...h.aggregator.parts][0];
            assert.equal((dim?.stateOf("levelControl") as Record<string, unknown>).currentLevel, 1);
        } finally {
            await h.close();
        }
    });

    it("refuses a set_state whose keys the role does not speak", async () => {
        // This used to answer success. An onOffLight has no LevelControl, so
        // `{level: 50}` produced an empty patch, `applyStates` returned early
        // and §3.4 replied `{}` — and because the plugin does not await
        // `set_state`, a whole role's worth of writes could go nowhere with
        // nothing anywhere saying so. The symptom (ecosystem shows stale state)
        // is identical to the bridge being down.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            for (const states of [
                { level: 50, occupied: true }, // another role's vocabulary
                { level: 55 }, // the golden set_state_bad_keys frame
                { onOf: true }, // misspelt
                { onOff: "true" }, // right key, wrong type
            ]) {
                const error = await rejects(() => h.registry.setState(1, states), ErrorCode.malformedArgs);
                for (const key of Object.keys(states)) {
                    assert.ok(error.message.includes(key), `${error.message} should name ${key}`);
                }
            }
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });

    it("produces the golden set_state_bad_keys details verbatim (§7)", async () => {
        // The golden file carries this refusal for both suites; the string in
        // it has to be the one the node really builds.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(123456789, Role.onOffLight)], false);
            const error = await rejects(
                () => h.registry.setState(123456789, { level: 55 }),
                ErrorCode.malformedArgs,
            );
            assert.equal(
                error.message,
                "role onOffLight consumed none of the states given; rejected key(s): level (§4.2)",
            );
        } finally {
            await h.close();
        }
    });

    it("still accepts an empty states, and a partly-understood one", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            // §3.4 with nothing to say is a lawful no-op, not a refusal.
            await h.registry.setState(1, {});
            // Version skew: a newer plugin may send a key this bridge does not
            // know yet. The keys it does know must still be applied.
            await h.registry.setState(1, { onOff: true, futureKey: 1 });
            const dim = [...h.aggregator.parts][0];
            assert.equal((dim?.stateOf("onOff") as Record<string, unknown>).onOff, true);
        } finally {
            await h.close();
        }
    });

    it("publishes the FULL bridged-accessory identity on every child", async () => {
        // Every one of these is optional in the cluster (@matter/types 0.17.8:
        // only `reachable` is mandatory), so nothing enforces their presence
        // except this test — and left off, an ecosystem's accessory-detail pane
        // reads blank or "Unknown" for a bridge that does know the answers.
        // Pinned as a SET so the list cannot silently shrink.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { label: "Kitchen Lamp" })], false);
            const endpoint = [...h.aggregator.parts][0];
            const info = endpoint?.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;

            // Per-accessory: from the spec.
            assert.equal(info.nodeLabel, "Kitchen Lamp");
            assert.equal(info.productLabel, "Kitchen Lamp");
            assert.equal(info.uniqueId, uniqueIdFor(1));
            assert.equal(info.reachable, true);
            assert.equal(typeof info.serialNumber, "string");

            // Bridge-wide: from the identity, and identical to what the root
            // node's BasicInformation carries.
            assert.equal(info.vendorName, BRIDGE_IDENTITY.vendorName);
            assert.equal(Number(info.vendorId), BRIDGE_IDENTITY.vendorId);
            assert.equal(info.productName, BRIDGE_IDENTITY.productName);
            assert.equal(info.hardwareVersion, BRIDGE_IDENTITY.hardwareVersion);
            assert.equal(info.hardwareVersionString, BRIDGE_IDENTITY.hardwareVersionString);
            assert.equal(info.softwareVersion, BRIDGE_IDENTITY.softwareVersion);
            assert.equal(info.softwareVersionString, BRIDGE_IDENTITY.softwareVersionString);

            // Seeded so `increaseConfigurationVersion` has something to bump.
            assert.equal(info.configurationVersion, 1);
        } finally {
            await h.close();
        }
    });

    it("matter.js RESTORES a bridged accessory's ConfigurationVersion across a restart", async () => {
        // Pins a matter.js 0.17.8 behaviour that the data model actively
        // suggests is false, so nobody re-derives the wrong answer from it.
        //
        // The bridged cluster's ConfigurationVersion carries NO data-model
        // quality (`bridged-device-basic-information.element.js`), unlike the
        // root's `quality: "N"`, and `BridgedDeviceBasicInformationServer`
        // extends only `uniqueId` to be persistent. Read that way, the seed in
        // `bridgedInfoFor` would be the live value on every start and every
        // accessory would reset to 1 on every restart — a backwards jump on an
        // attribute Matter defines as monotonic, and one matter.js guards
        // against only on the root.
        //
        // It does not happen. matter.js writes the value into its own store as
        // `root.parts.aggregator.parts.<id>.bridgedDeviceBasicInformation.configurationVersion`
        // (measured) and restores it OVER the constructor seed, so the seed
        // applies only the first time an endpoint id is created. Nothing needs
        // to persist it alongside the endpoint numbers.
        const storagePath = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-configver-"));
        scratchRoots.push(storagePath);
        const readVersion = (h: Harness): number => {
            const endpoint = [...h.aggregator.parts][0];
            const info = endpoint?.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;
            return info.configurationVersion as number;
        };

        let raised: number;
        const first = await harness({ storagePath });
        try {
            await first.registry.reconcile([spec(1, Role.onOffLight)], false);
            assert.equal(readVersion(first), 1, "a brand-new accessory starts at the seed");
            // Drive it up through matter.js's own API, which is the only thing
            // that may change it (the value is read-only over the wire).
            await [...first.aggregator.parts][0]!.act(agent =>
                agent.get(BridgedDeviceBasicInformationServer).increaseConfigurationVersion(),
            );
            raised = readVersion(first);
            assert.ok(raised > 1, `expected the version to climb, got ${raised}`);
        } finally {
            await first.close();
        }

        // A genuinely new node and a new storage lock on the same directory.
        const second = await harness({ storagePath });
        try {
            await second.registry.reconcile([spec(1, Role.onOffLight)], false);
            assert.equal(readVersion(second), raised,
                "matter.js must restore the persisted version over the seed");
        } finally {
            await second.close();
        }
    });

    it("applies §3.5 reachability to Bridged Device Basic Information", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            await h.registry.setReachable(1, false);
            const endpoint = [...h.aggregator.parts][0];
            const info = endpoint?.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;
            assert.equal(info.reachable, false);
        } finally {
            await h.close();
        }
    });
});

/** A matter.js `$Changed` observable, as much of it as these tests drive. */
interface ChangeObservable {
    emit(value: unknown, oldValue: unknown, context: unknown): void;
}

/** A matter.js cluster *event* observable, as much of it as these tests drive. */
interface EventObservable {
    on(handler: (payload: Record<string, unknown>) => void): void;
}

/**
 * Stand in for a controller writing an attribute.
 *
 * A real remote write needs a commissioned fabric and a session, which is a
 * disproportionate amount of machinery for what is being checked: that our
 * handler is on the *real* observable and reads the context matter.js passes
 * it. Emitting on that observable with a non-offline context exercises exactly
 * that path — `RemoteActorContext` declares `offline?: false`.
 */
function ecosystemWrite(
    endpoint: Endpoint,
    behavior: string,
    attribute: string,
    value: unknown,
    previous: unknown,
): void {
    const events = endpoint.eventsOf(behavior) as unknown as Record<string, ChangeObservable | undefined>;
    const observable = events[`${attribute}$Changed`];
    assert.ok(observable !== undefined, `${behavior}.${attribute}$Changed does not exist`);
    observable.emit(value, previous, { offline: false });
}

describe("command events and the echo guard (§4.2, §6.4)", () => {
    it("emits no command event for our own set_state writes", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            await h.registry.setState(1, { onOff: true, level: 60, colorTempMireds: 320 });
            await h.registry.setState(1, { hue: 210, saturation: 80 });
            await h.registry.setState(1, { onOff: false });
            assert.deepEqual(h.commands, [], "a local write echoed back as a command event");
        } finally {
            await h.close();
        }
    });

    it("emits no command event for a plain onOff role's set_state (#143 twin)", async () => {
        // The extendedColorLight test above already pins this for a role whose
        // onOff cluster is a side effect of the colour/level clusters; this pins
        // it for the roles whose *only* cluster is OnOff, since #143 changes
        // exactly how those roles' pushes reach the attribute.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            await h.registry.setState(1, { onOff: true });
            await h.registry.setState(1, { onOff: false });
            assert.deepEqual(h.commands, [], "a local onOff write echoed back as a command event");
        } finally {
            await h.close();
        }
    });

    it("emits the §4.2 payload for each attribute an ecosystem changes (WATCH_* backstop pin)", async () => {
        // Since #143, onOff/currentLevel/colorTemperatureMireds/currentHue
        // are no longer written by any code path this bridge controls — the
        // command-conversion overrides never write an attribute, so under
        // normal operation none of these observables ever fires. They stay
        // wired anyway as backstops (see WATCH_LEVEL's own doc): this test
        // pins that IF something ever did write one of these attributes in a
        // remote context again — a matter.js upgrade, a code path this PR
        // did not anticipate — the watcher would still turn it into the
        // right §4.2 frame, rather than the change vanishing silently.
        // ecosystemWrite() simulates that "something", since a real
        // invocation cannot any more (the overrides intercept it first).
        const h = await harness();
        try {
            await h.registry.reconcile([spec(7, Role.extendedColorLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);
            // Seed hue/saturation locally so the paired reads have known values.
            await h.registry.setState(7, { hue: 210, saturation: 80 });
            h.commands.length = 0;

            ecosystemWrite(endpoint, "onOff", "onOff", true, false);
            ecosystemWrite(endpoint, "levelControl", "currentLevel", 152, 1);
            ecosystemWrite(endpoint, "colorControl", "colorTemperatureMireds", 320, 153);
            ecosystemWrite(endpoint, "colorControl", "currentHue", 148, 0);
            // WATCH_SATURATION is the one backstop the rows above never
            // exercised — it pairs with hue from state the same way
            // WATCH_HUE pairs with saturation from state.
            ecosystemWrite(endpoint, "colorControl", "currentSaturation", 127, 203);

            assert.deepEqual(h.commands, [
                { indigoDeviceId: 7, command: "onOff", args: { value: true } },
                { indigoDeviceId: 7, command: "setLevel", args: { level: 60 } },
                { indigoDeviceId: 7, command: "setColorTemp", args: { colorTempMireds: 320 } },
                // setColor carries both halves; the saturation comes from state.
                { indigoDeviceId: 7, command: "setColor", args: { hue: 210, saturation: 80 } },
                // setColor carries both halves; the hue comes from state.
                { indigoDeviceId: 7, command: "setColor", args: { hue: 210, saturation: 50 } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("a real moveToLevelWithOnOff emits both frames and moves NEITHER attribute (#143 boundary fence)", async () => {
        // Before #143, LevelControlServer.couple() (LevelControlServer.js:
        // 363-398) wrote `agent.get(OnOffServer).state.onOff` DIRECTLY when a
        // *WithOnOff command turned the light on or off — it never called
        // on()/off(), so it reached Indigo purely through WATCH_ON_OFF, and a
        // real invocation could not exercise it (endpoint.act() always opens
        // an offline LocalActorContext, which the echo guard would have
        // swallowed). #143's override never calls couple() at all — the
        // whole method body is replaced — so this is now a REAL invocation,
        // and the assertion is stronger than the old fence's: not just that
        // both frames arrive, but that neither attribute moves. Both facts
        // are the direct consequence of never calling couple() or writing
        // `this.state.currentLevel`/`agent.get(OnOffServer).state.onOff`.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight, { states: { onOff: false } })], false);
            const endpoint = only(h);

            await endpoint.act(agent =>
                agent.get(LevelLighting).moveToLevelWithOnOff({
                    level: 128,
                    transitionTime: null,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );

            // setLevel ALONE for the up direction (#239): Indigo's
            // turn-on-to-level does the turn-on, and the leading onOff frame
            // was live-observed racing the level command's Z-Wave reports.
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setLevel", args: { level: 50 } },
            ]);
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onOff,
                false,
                "the invocation must not have moved onOff",
            );
            assert.equal(
                (endpoint.stateOf("levelControl") as Record<string, unknown>).currentLevel,
                1,
                "the invocation must not have moved currentLevel",
            );
        } finally {
            await h.close();
        }
    });

    it("stops listening once the endpoint is removed", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);

            ecosystemWrite(endpoint, "onOff", "onOff", true, false);
            assert.equal(h.commands.length, 1, "the listener should be live before removal");

            await h.registry.remove(1);
            assert.equal(h.registry.size, 0);

            // The endpoint object survives in this test's hand; the listener must
            // not, or a removed export would keep talking to the plugin.
            ecosystemWrite(endpoint, "onOff", "onOff", false, true);
            assert.equal(h.commands.length, 1, "a removed endpoint still emitted");

            // #143: `remove()` closes the underlying matter.js endpoint (unlike
            // `registry.close()`, which only unwatches), so an invocation on it
            // is refused by matter.js's own construction lifecycle before it
            // ever reaches COMMAND_SINKS — the same guarantee the door lock's
            // "refuses an invocation on a removed endpoint outright" test pins.
            await assert.rejects(async () => endpoint.act(agent => agent.get(OnOffServer).on()));
            assert.equal(h.commands.length, 1, "a removed endpoint still reported an invocation");
        } finally {
            await h.close();
        }
    });

    it("stops listening when the registry closes", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);
            h.registry.close();
            ecosystemWrite(endpoint, "onOff", "onOff", true, false);
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });
});

describe("listener wiring is not allowed to fail quietly (§4.2)", () => {
    it("throws at construction when a watched observable is missing", async () => {
        // Simulates the regression the throw exists for: matter.js renames an
        // attribute, or drops one with a feature, and the observable this role
        // subscribes to is simply not there. Skipping it — which is what the
        // code used to do — leaves the accessory fully controllable in the
        // ecosystem with its entire `setLevel` family silently disconnected,
        // and nothing in any log to say why Indigo stopped responding.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);

            // A Proxy, not a spread: matter.js serves these observables from the
            // prototype, so a spread would drop *all* of them and the test would
            // pass for the wrong reason (on the first watch, not the one meant).
            const realEventsOf = endpoint.eventsOf.bind(endpoint);
            (endpoint as unknown as { eventsOf: (behavior: string) => unknown }).eventsOf = (behavior: string) => {
                const events = realEventsOf(behavior) as Record<string, unknown>;
                if (behavior !== "levelControl") {
                    return events;
                }
                return new Proxy(events, {
                    get: (target, property, receiver) =>
                        property === "currentLevel$Changed" ? undefined : Reflect.get(target, property, receiver),
                });
            };

            assert.throws(
                () => watchCommands(
                    endpoint, { indigoDeviceId: 1, role: Role.dimmableLight, publishedAs: uniqueIdFor(1) }, () => {},
                ),
                (error: unknown) => {
                    assert.ok(error instanceof ProtocolError);
                    assert.equal(error.code, ErrorCode.internal);
                    assert.match(error.message, /endpoint 1 \(dimmableLight\)/);
                    assert.match(error.message, /levelControl\.currentLevel\$Changed/);
                    return true;
                },
            );
        } finally {
            await h.close();
        }
    });

    it("contains a throwing command sink instead of taking the process down", async () => {
        // The handler runs inside matter.js's observable, inside the commit of
        // a remote write. An escaping throw becomes an unhandled rejection, and
        // this process exits on those — one bad payload would take the bridge
        // and every exported accessory with it.
        const h = await harness({ emitThrows: true });
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = [...h.aggregator.parts][0];
            assert.ok(endpoint !== undefined);

            assert.doesNotThrow(() => ecosystemWrite(endpoint, "onOff", "onOff", true, false));

            const dropped = h.logs.filter(line => line.includes("Dropped a onOff.onOff change for endpoint 1"));
            assert.equal(dropped.length, 1, h.logs.join("\n"));
            // The stack, not just the message: this is a node-side bug report.
            assert.match(dropped[0] ?? "", /the command sink blew up/);
            assert.match(dropped[0] ?? "", /at /);

            // And the endpoint is still usable afterwards.
            assert.doesNotThrow(() => ecosystemWrite(endpoint, "onOff", "onOff", false, true));
            assert.equal(h.commands.length, 2);
        } finally {
            await h.close();
        }
    });
});

describe("LevelControl transition handling (§6.4)", () => {
    it("pins managedTransitionTimeHandling off on every level-bearing role", async () => {
        // With managed transitions on, matter.js drives a moveToLevel in steps
        // of its own — written offline — and the echo guard would eat every one
        // of them, so an ecosystem dimming a lamp would never reach Indigo.
        // 0.17.8 already defaults this off; the pin is what stops a future
        // default flip severing the whole setLevel family in silence.
        const h = await harness();
        try {
            const roles = [Role.dimmableLight, Role.colorTemperatureLight, Role.extendedColorLight];
            await h.registry.reconcile(
                roles.map((role, index) => spec(700_001 + index, role)),
                false,
            );
            for (const [index, role] of roles.entries()) {
                const endpoint = [...h.aggregator.parts].find(part => part.id === endpointIdFor(700_001 + index));
                const level = endpoint?.stateOf("levelControl") as Record<string, unknown>;
                assert.equal(level.managedTransitionTimeHandling, false, `${role} manages transitions`);
            }
        } finally {
            await h.close();
        }
    });
});

describe("ConfigurationVersion (PRD §5.3)", () => {
    it("bumps the bridge's version when the endpoint set changes, and not otherwise", async () => {
        const h = await harness();
        const version = (): number =>
            (h.node.state.basicInformation as { configurationVersion?: number }).configurationVersion ?? 0;
        try {
            const start = version();
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            assert.equal(version(), start + 1, "an added endpoint is a configuration change");

            // A label/state update is not a configuration change: the bridged
            // node set is the same, and controllers do not need to re-read it.
            await h.registry.reconcile([spec(1, Role.onOffLight, { label: "Renamed" })], false);
            assert.equal(version(), start + 1);

            await h.registry.remove(1);
            assert.equal(version(), start + 2, "a removed endpoint is a configuration change");
        } finally {
            await h.close();
        }
    });
});

describe("endpoint-number stability (PRD §4.3 / XAC5)", () => {
    it("gives an id the same number across a node restart", async () => {
        const storagePath = mkdtempSync(join(SCRATCH_ROOT, "indigo-matter-restart-"));
        scratchRoots.push(storagePath);

        const first = await harness({ storagePath });
        let before: [number, number][];
        try {
            await first.registry.reconcile([1, 2, 3].map(id => spec(id, Role.onOffLight)), false);
            before = first.registry.summaries().map(s => [s.indigoDeviceId, s.endpointNumber]);
        } finally {
            await first.close();
        }

        const second = await harness({ storagePath });
        try {
            // Deliberately a different creation order: if numbers came from
            // iteration order rather than from the persisted id map, this is
            // where the accessories would swap identities.
            await second.registry.reconcile([3, 1, 2].map(id => spec(id, Role.onOffLight)), false);
            assert.deepEqual(
                second.registry.summaries().map(s => [s.indigoDeviceId, s.endpointNumber]),
                before,
            );
            // Nothing on the wire from a restart. Rebuilding the endpoints
            // writes their initial state, and if any of those writes reached
            // the plugin as a `command` the bridge would replay the whole
            // export back at Indigo on every node restart — every exported
            // device commanded to whatever the node happened to have persisted.
            assert.deepEqual(second.commands, [], "a restart sprayed command events");
        } finally {
            await second.close();
        }
    });
});

// ---------------------------------------------------------------------------
// E4: the roles that complete the v1 §4.2 table
// ---------------------------------------------------------------------------

/** The §4.2 roles E4 added, with the behaviour each one must carry. */
const E4_BEHAVIORS: [RoleValue, string][] = [
    [Role.occupancySensor, "occupancySensing"],
    [Role.contactSensor, "booleanState"],
    [Role.temperatureSensor, "temperatureMeasurement"],
    [Role.humiditySensor, "relativeHumidityMeasurement"],
    [Role.lightSensor, "illuminanceMeasurement"],
    [Role.pressureSensor, "pressureMeasurement"],
    [Role.flowSensor, "flowMeasurement"],
    [Role.thermostat, "thermostat"],
    [Role.doorLock, "doorLock"],
    [Role.windowCovering, "windowCovering"],
];

/**
 * The covering behaviour as the bridge builds it. `goToLiftPercentage` only
 * exists on the position-aware variant, so `agent.get()` needs the same
 * `.with()` the role factory used — the bare `WindowCoveringServer` type does
 * not carry the command.
 */
const PositionAwareCovering = WindowCoveringServer.with("Lift", "PositionAwareLift");

/**
 * The OnOff behaviour as every onOff-bearing role builds it. `onTime`/
 * `offWaitTime`/`onWithTimedOff` only exist with the Lighting feature, so
 * `agent.get()` needs the same `.with()` the role factory used — mirrors
 * {@link PositionAwareCovering}.
 */
const OnOffLighting = OnOffServer.with("Lighting");

/**
 * The LevelControl behaviour as every level-bearing role builds it, post-#143.
 * `agent.get()` needs the exact `.with()` the role factory used — mirrors
 * {@link OnOffLighting}/{@link PositionAwareCovering}.
 */
const LevelLighting = LevelControlServer.with("Lighting", "OnOff");

/**
 * The ColorControl behaviour as `extendedColorLight`/`colorTemperatureLight`
 * build it, post-#143. Mirrors {@link LevelLighting}.
 */
const ColorFull = ColorControlServer.with("HueSaturation", "Xy", "ColorTemperature");
const ColorCt = ColorControlServer.with("ColorTemperature");

/**
 * Poll until `check` passes (or the cap expires — the following assertion then
 * reports the real shortfall). The countdown tests used to sleep a fixed 400ms
 * against ~200ms timer deadlines, which a loaded CI runner can blow through;
 * polling keeps them fast when healthy and honest when not.
 */
async function waitFor(check: () => boolean, capMs = 1500): Promise<void> {
    const deadline = Date.now() + capMs;
    while (!check() && Date.now() < deadline) {
        await new Promise(resolve => setTimeout(resolve, 25));
    }
}

/** The one endpoint a single-spec reconcile produced. */
function only(h: Harness): Endpoint {
    const endpoint = [...h.aggregator.parts][0];
    assert.ok(endpoint !== undefined, "the reconcile built no endpoint");
    return endpoint;
}

describe("role factory (E4)", () => {
    it("supports every role in the §4.2 enum", () => {
        // The E4 completion criterion, asserted rather than described: the
        // table and the enum are now the same set, so an enum addition without
        // a factory fails here first.
        assert.deepEqual([...SUPPORTED_ROLES].sort(), [...(Object.values(Role) as RoleValue[])].sort());
    });

    it("builds each E4 role with bridged info and exactly its own cluster", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile(
                E4_BEHAVIORS.map(([role], index) => spec(910_001 + index, role, { label: `E4-${index}` })),
                false,
            );
            assert.equal(h.registry.size, E4_BEHAVIORS.length);

            for (const [index, [role, behavior]] of E4_BEHAVIORS.entries()) {
                const id = 910_001 + index;
                const endpoint = [...h.aggregator.parts].find(part => part.id === endpointIdFor(id));
                assert.ok(endpoint !== undefined, `${role} endpoint missing`);

                const info = endpoint.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>;
                assert.equal(info.nodeLabel, `E4-${index}`);
                assert.equal(info.uniqueId, uniqueIdFor(id));
                assert.notEqual(info.uniqueId, info.serialNumber);
                assert.equal(info.configurationVersion, 1);

                const behaviors = Object.keys(endpoint.state);
                assert.ok(behaviors.includes(behavior), `${role} has no ${behavior}`);
                // None of the E4 roles is a light: an accidental OnOff would
                // mean the device type was picked wrong.
                assert.ok(!behaviors.includes("onOff"), `${role} unexpectedly has OnOff`);
            }
        } finally {
            await h.close();
        }
    });

    it("builds every E4 role from a spec that already carries its full state", async () => {
        // The colour-state construction bug in reverse: thermostat and doorLock
        // both supply mandatory attributes through `initialState`, and a spec's
        // own `states` has to merge with those rather than replace them.
        const h = await harness();
        try {
            await h.registry.reconcile(
                [
                    spec(1, Role.thermostat, {
                        states: {
                            localTemperatureC: 20.5,
                            heatingSetpointC: 21,
                            coolingSetpointC: 24,
                            systemMode: "heat",
                        },
                    }),
                    spec(2, Role.doorLock, { states: { locked: true } }),
                    spec(3, Role.windowCovering, { states: { position: 70 } }),
                ],
                false,
            );
            assert.equal(h.registry.size, 3);

            const thermostat = [...h.aggregator.parts].find(part => part.id === endpointIdFor(1));
            const state = thermostat?.stateOf("thermostat") as Record<string, unknown>;
            assert.equal(state.localTemperature, 2050);
            assert.equal(state.occupiedHeatingSetpoint, 2100);
            assert.equal(state.occupiedCoolingSetpoint, 2400);
            assert.equal(state.systemMode, 4, "SystemMode.Heat");
            // Supplied by initialState and NOT clobbered by the spec's states.
            assert.equal(state.controlSequenceOfOperation, 4, "ControlSequenceOfOperation.CoolingAndHeating");

            const lock = [...h.aggregator.parts].find(part => part.id === endpointIdFor(2));
            const lockState = lock?.stateOf("doorLock") as Record<string, unknown>;
            assert.equal(lockState.lockState, 1, "LockState.Locked");
            assert.equal(lockState.operatingMode, 0, "OperatingMode.Normal — from initialState");

            const covering = [...h.aggregator.parts].find(part => part.id === endpointIdFor(3));
            const position = covering?.stateOf("windowCovering") as Record<string, unknown>;
            // 70 % open is 30 % closed. This is the inversion, on a real endpoint.
            assert.equal(position.currentPositionLiftPercent100ths, 3000);
            assert.equal(position.targetPositionLiftPercent100ths, 3000);
        } finally {
            await h.close();
        }
    });
});

describe("sensor set_state (§3.4, E4)", () => {
    /** role → the §4.2 states to push, and the `measuredValue` they must produce. */
    const CASES: [RoleValue, string, Record<string, unknown>, unknown][] = [
        [Role.temperatureSensor, "temperatureMeasurement", { temperatureC: 21.5 }, 2150],
        [Role.humiditySensor, "relativeHumidityMeasurement", { humidityPct: 48 }, 4800],
        [Role.lightSensor, "illuminanceMeasurement", { lux: 100 }, 20_001],
        // 0.1 kPa, not kPa — the conversion most likely to be wrong by 10x.
        [Role.pressureSensor, "pressureMeasurement", { pressureKPa: 101.3 }, 1013],
        [Role.flowSensor, "flowMeasurement", { flowM3h: 1.25 }, 13],
    ];

    for (const [role, behavior, states, expected] of CASES) {
        it(`writes ${role} on the cluster's own scale`, async () => {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, role)], false);
                await h.registry.setState(1, states);
                assert.equal((only(h).stateOf(behavior) as Record<string, unknown>).measuredValue, expected);
                assert.deepEqual(h.commands, [], "a sensor emitted a command");
            } finally {
                await h.close();
            }
        });
    }

    it("writes the occupancy bitmap, not a bare boolean", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.occupancySensor)], false);
            await h.registry.setState(1, { occupied: true });
            // matter.js models the attribute as an `Occupancy` bitmap instance
            // whose bit 0 is the whole meaning, so the field is read rather
            // than the object compared — a deepEqual against a plain object
            // fails on the class, not on the value.
            const occupancy = (): unknown =>
                ((only(h).stateOf("occupancySensing") as Record<string, unknown>).occupancy as { occupied: boolean })
                    .occupied;
            assert.equal(occupancy(), true);
            await h.registry.setState(1, { occupied: false });
            assert.equal(occupancy(), false);
        } finally {
            await h.close();
        }
    });

    it("writes contact with no inversion — both sides call true 'closed'", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.contactSensor)], false);
            await h.registry.setState(1, { contact: true });
            assert.equal((only(h).stateOf("booleanState") as Record<string, unknown>).stateValue, true);
            await h.registry.setState(1, { contact: false });
            assert.equal((only(h).stateOf("booleanState") as Record<string, unknown>).stateValue, false);
        } finally {
            await h.close();
        }
    });

    it("refuses a sensor state key belonging to another sensor", async () => {
        // Every sensor writes `measuredValue`, so a mixed-up role would look
        // like it worked; the §4.2 key is the only thing that distinguishes them.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.temperatureSensor)], false);
            const error = await rejects(() => h.registry.setState(1, { humidityPct: 48 }), ErrorCode.malformedArgs);
            assert.match(error.message, /humidityPct/);
        } finally {
            await h.close();
        }
    });
});

describe("thermostat commands (§4.2, E4)", () => {
    it("reports each ecosystem setpoint and mode write as its §4.2 command", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(5, Role.thermostat)], false);
            const endpoint = only(h);

            // Ecosystems drive a thermostat by writing the attributes, so one
            // watcher per attribute covers the whole command family.
            ecosystemWrite(endpoint, "thermostat", "occupiedHeatingSetpoint", 2150, 2000);
            ecosystemWrite(endpoint, "thermostat", "occupiedCoolingSetpoint", 2400, 2600);
            ecosystemWrite(endpoint, "thermostat", "systemMode", 3, 4);

            assert.deepEqual(h.commands, [
                { indigoDeviceId: 5, command: "setHeatingSetpoint", args: { valueC: 21.5 } },
                { indigoDeviceId: 5, command: "setCoolingSetpoint", args: { valueC: 24 } },
                { indigoDeviceId: 5, command: "setSystemMode", args: { mode: "cool" } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("says nothing about a mode outside §4.2's four values", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(5, Role.thermostat)], false);
            // EmergencyHeat. Legal Matter, no §4.2 spelling — reporting the
            // nearest guess would have Indigo act on something nobody asked for.
            ecosystemWrite(only(h), "thermostat", "systemMode", 5, 4);
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });

    it("does not echo its own set_state back as a command", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(5, Role.thermostat)], false);
            await h.registry.setState(5, { localTemperatureC: 19, heatingSetpointC: 21, systemMode: "auto" });
            assert.deepEqual(h.commands, [], "a local thermostat write echoed back");
        } finally {
            await h.close();
        }
    });

    it("accepts the setpoints a real house uses, which matter.js's defaults reject", async () => {
        // PR #125 C2. Un-seeded, matter.js enforces its own 7-30 °C heating and
        // 16-32 °C cooling limits and answers CONSTRAINT_ERROR (status 135) to
        // both of these — 5 °C frost protection, and the literal 0 Indigo
        // reports for `coolSetpoint` on a heat-only thermostat. The write threw,
        // Indigo did not change, so the next deviceUpdated computed the same
        // diff and pushed the same rejected value again, forever.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(5, Role.thermostat)], false);
            await h.registry.setState(5, { heatingSetpointC: 5 });
            assert.equal((only(h).stateOf("thermostat") as Record<string, unknown>).occupiedHeatingSetpoint, 500);

            await h.registry.setState(5, { coolingSetpointC: 0 });
            assert.equal((only(h).stateOf("thermostat") as Record<string, unknown>).occupiedCoolingSetpoint, 0);
        } finally {
            await h.close();
        }
    });

    it("holds both setpoints exactly as pushed — no deadband rewrite", async () => {
        // PR #125 C3. `minSetpointDeadBand` defaults to 2 °C and the spec says
        // the server *adjusts the other setpoint* rather than refusing: heat 21
        // / cool 21 left the node holding heat 19. That is an offline write, so
        // it raises no `command` event, so the plugin never learned and never
        // corrected — the ecosystem showed a setpoint two degrees out, silently
        // and permanently.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(5, Role.thermostat)], false);
            await h.registry.setState(5, { heatingSetpointC: 21, coolingSetpointC: 21 });
            const state = only(h).stateOf("thermostat") as Record<string, unknown>;
            assert.equal(state.occupiedHeatingSetpoint, 2100, "the deadband rewrote the heating setpoint");
            assert.equal(state.occupiedCoolingSetpoint, 2100, "the deadband rewrote the cooling setpoint");
            assert.deepEqual(h.commands, [], "a deadband adjustment must not look like an ecosystem write");
        } finally {
            await h.close();
        }
    });
});

describe("door lock commands (§4.2, §7, E4)", () => {
    it("reports lock and unlock invocations", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(9, Role.doorLock)], false);
            const endpoint = only(h);
            await endpoint.act(agent => agent.get(DoorLockServer).unlockDoor({}));
            await endpoint.act(agent => agent.get(DoorLockServer).lockDoor({}));
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 9, command: "unlock", args: {} },
                { indigoDeviceId: 9, command: "lock", args: {} },
            ]);
        } finally {
            await h.close();
        }
    });

    it("NEVER auto-confirms: lockState does not move on the ecosystem's say-so", async () => {
        // PRD §7. matter.js's stock lockDoor/unlockDoor set lockState themselves,
        // which for a bridge means the ecosystem shows the door locked the
        // instant it asks — before Indigo, the mesh or the bolt agree. The
        // overrides exist for this assertion.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(9, Role.doorLock, { states: { locked: false } })], false);
            const endpoint = only(h);
            assert.equal((endpoint.stateOf("doorLock") as Record<string, unknown>).lockState, 2, "Unlocked");

            await endpoint.act(agent => agent.get(DoorLockServer).lockDoor({}));
            assert.equal(
                (endpoint.stateOf("doorLock") as Record<string, unknown>).lockState,
                2,
                "the ecosystem's lockDoor moved lockState by itself",
            );

            // It moves when — and only when — Indigo says the bolt actually did.
            await h.registry.setState(9, { locked: true });
            assert.equal((endpoint.stateOf("doorLock") as Record<string, unknown>).lockState, 1, "Locked");
        } finally {
            await h.close();
        }
    });

    it("does not echo the confirming set_state back as a lock command", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(9, Role.doorLock)], false);
            await h.registry.setState(9, { locked: true });
            await h.registry.setState(9, { locked: false });
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });

    it("stops reporting once the registry lets the endpoint go, and FAILS the invocation", async () => {
        // The invocation roles have no observables, so the COMMAND_SINKS entry
        // is the only thing between a forgotten lock and a command for a device
        // the plugin no longer exports. `close()` rather than `remove()`
        // because a removed endpoint is *closed* and cannot be invoked at all —
        // which is its own guarantee, but tests matter.js, not the teardown.
        //
        // PR #125 H2: a lock whose command reaches nothing must *fail*. It used
        // to warn and return success, so Home showed "Unlocked", the user
        // walked away, and the bolt had never moved. There is no protocol frame
        // for "your command was dropped", so the invocation itself is the only
        // channel back to the person at the door.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(9, Role.doorLock)], false);
            const endpoint = only(h);
            await endpoint.act(agent => agent.get(DoorLockServer).lockDoor({}));
            assert.equal(h.commands.length, 1, "the sink should be live beforehand");

            h.registry.close();
            await assert.rejects(
                async () => endpoint.act(agent => agent.get(DoorLockServer).unlockDoor({})),
                /not connected/,
                "an undeliverable unlock reported success",
            );
            assert.equal(h.commands.length, 1, "a released lock still reported");
        } finally {
            await h.close();
        }
    });

    it("leaves the other roles warning-and-returning, not failing", async () => {
        // PR #125 H2 is doorLock-only, deliberately. A lamp whose command
        // reaches nothing is recovered by the next attach pushing the truth
        // back, and nobody was misled about anything that matters — so an
        // ecosystem error there would be noise. This is the control case for
        // the assertion above.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            h.registry.close();
            await endpoint.act(agent => agent.get(OnOffServer).on());
            assert.deepEqual(h.commands, [], "a released light still reported");
        } finally {
            await h.close();
        }
    });

    it("emits LockOperation when — and only when — a push moves the bolt", async () => {
        // PR #125 H3. The event is spec-mandatory and is what an ecosystem's
        // activity history and lock notifications are built on.
        // IndigoDoorLockServer removed matter.js's emission along with its
        // auto-confirmation, which was right (the stock server fires it when the
        // *command* arrives, before Indigo or the bolt agree) but left the
        // bridge silent. It is raised on the plugin's set_state instead, which
        // is when the bolt is known to have actually moved.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(9, Role.doorLock, { states: { locked: false } })], false);
            const events: Record<string, unknown>[] = [];
            const observable = (only(h).eventsOf("doorLock") as unknown as Record<string, EventObservable>)
                .lockOperation;
            assert.ok(observable !== undefined, "doorLock exposes no lockOperation event");
            observable.on(payload => events.push(payload));

            await h.registry.setState(9, { locked: true });
            assert.equal(events.length, 1, "locking the bolt raised no LockOperation");
            assert.equal(events[0]?.lockOperationType, DoorLock.LockOperationType.Lock);
            // Unspecified, not Manual or Remote: by the time Indigo reports a
            // lock state change, what caused it has been flattened to a boolean.
            assert.equal(events[0]?.operationSource, DoorLock.OperationSource.Unspecified);
            assert.equal(events[0]?.fabricIndex, null);
            assert.equal(events[0]?.sourceNode, null);

            // A push that repeats what the bolt already says is not an operation.
            await h.registry.setState(9, { locked: true });
            assert.equal(events.length, 1, "an unchanged lockState raised a LockOperation");

            await h.registry.setState(9, { locked: false });
            assert.equal(events.length, 2);
            assert.equal(events[1]?.lockOperationType, DoorLock.LockOperationType.Unlock);
        } finally {
            await h.close();
        }
    });

    it("refuses an invocation on a removed endpoint outright (PRD §7 race row)", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(9, Role.doorLock)], false);
            const endpoint = only(h);
            await h.registry.remove(9);
            await assert.rejects(async () => endpoint.act(agent => agent.get(DoorLockServer).lockDoor({})));
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });
});

describe("onOff commands (§4.2, §7, #143, #201)", () => {
    it("an invocation matching current state still emits, exactly once", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await h.registry.setState(1, { onOff: true });
            h.commands.length = 0;

            await endpoint.act(agent => agent.get(OnOffLighting).on());
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "onOff", args: { value: true } }]);
        } finally {
            await h.close();
        }
    });

    it("an invocation differing from current state emits exactly once, not twice", async () => {
        // The fence against a watcher+sink double-emit: onOff is watched
        // (WATCH_ON_OFF, for LevelControl's coupling) AND invocation-driven
        // now, and applyIndigoOnOff/the overrides must not trip both.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { states: { onOff: false } })], false);
            const endpoint = only(h);
            await endpoint.act(agent => agent.get(OnOffLighting).on());
            assert.equal(h.commands.length, 1, "the invocation must not also echo through the attribute watcher");
        } finally {
            await h.close();
        }
    });

    it("NEVER auto-confirms: onOff does not move on the ecosystem's say-so (PRD §7)", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { states: { onOff: false } })], false);
            const endpoint = only(h);
            await endpoint.act(agent => agent.get(OnOffLighting).on());
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onOff,
                false,
                "the ecosystem's on() moved onOff by itself",
            );

            // It moves when — and only when — Indigo says the device actually did.
            await h.registry.setState(1, { onOff: true });
            assert.equal((endpoint.stateOf("onOff") as Record<string, unknown>).onOff, true);
        } finally {
            await h.close();
        }
    });

    it("toggle and offWithEffect funnel through the same override, unmoved and single-emit", async () => {
        // matter.js's own class doc: "it is enough to override on() and off()
        // with custom control logic" — toggle/offWithEffect are deliberately
        // left stock, and both dispatch to our off() virtually.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await h.registry.setState(1, { onOff: true });
            h.commands.length = 0;

            await endpoint.act(agent => agent.get(OnOffLighting).toggle());
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "onOff", args: { value: false } }]);

            h.commands.length = 0;
            await endpoint.act(agent =>
                agent.get(OnOffLighting).offWithEffect({ effectIdentifier: 0, effectVariant: 0 }),
            );
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "onOff", args: { value: false } }]);
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onOff,
                true,
                "offWithEffect must not move the attribute either",
            );
        } finally {
            await h.close();
        }
    });

    it("a running countdown's own expiry emits off exactly once (#201)", async () => {
        // #timedOnTick's own `await this.off()` runs with the timer's bare
        // callback context (OnOffServer.js:163, no trailing context argument),
        // which is why on()/off() cannot be guarded on isEcosystemChange —
        // see the IndigoOnOffServer class doc.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await h.registry.setState(1, { onOff: true });
            h.commands.length = 0;

            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: false }, onTime: 2, offWaitTime: 0 }),
            );
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "onOff", args: { value: true } }]);

            await waitFor(() => h.commands.length >= 2);
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "onOff", args: { value: true } },
                { indigoDeviceId: 1, command: "onOff", args: { value: false } },
            ]);
            // No auto-confirm: the attribute only ever moves from Indigo's own
            // push, never from the countdown's own expiry.
            assert.equal((endpoint.stateOf("onOff") as Record<string, unknown>).onOff, true);
            assert.equal((endpoint.stateOf("onOff") as Record<string, unknown>).onTime, 0);
        } finally {
            await h.close();
        }
    });

    it("Indigo turning the device off during a countdown cancels it (#201)", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await h.registry.setState(1, { onOff: true });

            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: false }, onTime: 5, offWaitTime: 0 }),
            );
            h.commands.length = 0;

            // Indigo reports the device off mid-countdown. super.off() stops
            // timedOnTimer unconditionally (OnOffServer.js:85-87) — from
            // matter.js's own code, not a special case here.
            await h.registry.setState(1, { onOff: false });
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onTime,
                0,
                "the countdown must be cancelled",
            );

            // Push it back on: super.on()'s own cleanup (OnOffServer.js:67-72)
            // leaves no stray delayedOffTimer/offWaitTime behind either.
            await h.registry.setState(1, { onOff: true });

            // If the cancellation had not taken (a #201 regression), the
            // original countdown would still fire an off around its ~500ms mark.
            await new Promise(resolve => setTimeout(resolve, 700));
            assert.deepEqual(h.commands, [], "a cancelled countdown fired anyway, past its original deadline");
        } finally {
            await h.close();
        }
    });

    it("a redundant push does not kill a live countdown (§7.3)", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: false }, onTime: 2, offWaitTime: 0 }),
            );
            h.commands.length = 0;

            // The attribute never moved — our on() only emits, it never writes
            // state.onOff — so this "off" push targets an already-false
            // attribute. Without the §7.3 guard, applyIndigoOnOff would call
            // super.off(), which stops timedOnTimer unconditionally and would
            // kill a countdown a genuine race (a plugin restart landing before
            // its own confirming push) must not be able to cancel.
            await h.registry.setState(1, { onOff: false });
            assert.deepEqual(h.commands, [], "the redundant push itself must not emit");

            await waitFor(() => h.commands.length >= 1);
            assert.deepEqual(
                h.commands,
                [{ indigoDeviceId: 1, command: "onOff", args: { value: false } }],
                "the countdown must still have expired and reported off",
            );
        } finally {
            await h.close();
        }
    });

    it("every OnOff-bearing role reports invocations through the sink (five-role fence)", async () => {
        // Dropping IndigoOnOffServer from any role's deviceType().with() is a
        // SILENT regression everywhere else: agent.get resolves by behaviour
        // id, so the stock server answers, auto-confirms the attribute, and
        // emits nothing — and no other test in this file exercises an
        // invocation on the plug or the two colour roles.
        const roles = [
            Role.onOffPlugInUnit,
            Role.onOffLight,
            Role.dimmableLight,
            Role.colorTemperatureLight,
            Role.extendedColorLight,
        ];
        for (const role of roles) {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, role)], false);
                const endpoint = only(h);
                await endpoint.act(agent => agent.get(OnOffLighting).on());
                assert.deepEqual(
                    h.commands,
                    [{ indigoDeviceId: 1, command: "onOff", args: { value: true } }],
                    `role ${role}: the invocation must emit through the sink, exactly once`,
                );
                assert.equal(
                    (endpoint.stateOf("onOff") as Record<string, unknown>).onOff,
                    false,
                    `role ${role}: the invocation must not move the attribute`,
                );
            } finally {
                await h.close();
            }
        }
    });

    it("acceptOnlyWhenOn discards the command while the attribute is unconfirmed (accepted stock quirk)", async () => {
        // Stock onWithTimedOff answers acceptOnlyWhenOn from the ATTRIBUTE
        // (OnOffServer.js:138-140), which under no-auto-confirm stays false
        // for the whole tap-to-Indigo-confirmation window — so an "on, then
        // timed-off-if-on" pair sent quickly loses its countdown, silently,
        // with success reported to the controller. Accepted (see the
        // IndigoOnOffServer class doc's stock-quirks block); this test exists
        // so any change to that behaviour is noticed, not to endorse it.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: true }, onTime: 100, offWaitTime: 0 }),
            );
            assert.deepEqual(h.commands, [], "the discarded command must not emit");
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onTime,
                0,
                "the countdown must never have started",
            );
        } finally {
            await h.close();
        }
    });

    it("a second onWithTimedOff during a live countdown extends it and still emits", async () => {
        // Stock extension path: onTime = Math.max(requested, remaining)
        // (OnOffServer.js:148), then this.on() — which must still reach our
        // override and the sink.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: false }, onTime: 50, offWaitTime: 0 }),
            );
            await h.registry.setState(1, { onOff: true });
            h.commands.length = 0;

            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: false }, onTime: 100, offWaitTime: 0 }),
            );
            assert.deepEqual(
                h.commands,
                [{ indigoDeviceId: 1, command: "onOff", args: { value: true } }],
                "the extending command must still be forwarded",
            );
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onTime,
                100,
                "the countdown must have extended to the larger onTime",
            );
        } finally {
            await h.close();
        }
    });

    it("onWithRecallGlobalScene funnels through on() once, then no-ops once the flag is set", async () => {
        // Stock (OnOffServer.js:119-131): early-return only when
        // globalSceneControl is already true. In this composition it starts
        // false, so the first recall skips the fabric-scene lookup (offline
        // context, no fabric), flips the flag true, and funnels into
        // this.on() — one emission through our override, attribute unmoved.
        // The second recall then hits the early return: nothing at all.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const endpoint = only(h);
            await endpoint.act(agent => agent.get(OnOffLighting).onWithRecallGlobalScene());
            assert.deepEqual(
                h.commands,
                [{ indigoDeviceId: 1, command: "onOff", args: { value: true } }],
                "the first recall must forward exactly one on",
            );
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onOff,
                false,
                "the recall must not move the attribute",
            );

            h.commands.length = 0;
            await endpoint.act(agent => agent.get(OnOffLighting).onWithRecallGlobalScene());
            assert.deepEqual(h.commands, [], "with globalSceneControl now true, the recall must no-op");
        } finally {
            await h.close();
        }
    });

    it("a combined push still applies the level half when the onOff half is a no-op", async () => {
        // The §7.3 same-value guard lives INSIDE applyIndigoOnOff, per
        // endpoint — fencing the refactor that hoists it into applyStates and
        // early-returns the whole push, which would eat the level half of the
        // full-state replay Indigo sends on every attach.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            const endpoint = only(h);
            await h.registry.setState(1, { onOff: true, level: 60 });
            await h.registry.setState(1, { onOff: true, level: 80 });
            const level = (endpoint.stateOf("levelControl") as Record<string, unknown>).currentLevel;
            assert.equal(level, percentToCurrentLevel(80), "the level half of a no-op-onOff push must still apply");
        } finally {
            await h.close();
        }
    });

    it("a failed onOff half does not stop the residual set, and the error names the half (half-apply)", async () => {
        // applyStates runs the onOff act and the residual set as two
        // transactions; a failure in the first must not silently strand the
        // second (stale brightness until the next push, indefinitely for an
        // untouched lamp). Stub the endpoint's act to fail — the cleanest
        // seam the harness offers, since a real act failure needs a closing
        // endpoint mid-push.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            const endpoint = only(h);
            // Shadow act with an instance property that fails the FIRST call
            // only: applyStates' onOff act must fail, but Endpoint.set()
            // itself routes through act too (Endpoint.js:229-230), so a
            // blanket stub would take the residual half down with it and turn
            // this into the both-halves AggregateError case. Restore before
            // close, which needs the real thing.
            const endpointWithAct = endpoint as unknown as { act: (...args: unknown[]) => Promise<unknown> };
            const originalAct = endpointWithAct.act;
            let firstCall = true;
            endpointWithAct.act = function (...args: unknown[]) {
                if (firstCall) {
                    firstCall = false;
                    return Promise.reject(new Error("boom"));
                }
                return originalAct.apply(endpoint, args);
            };
            try {
                await assert.rejects(
                    async () => applyStates(endpoint, Role.dimmableLight, { onOff: true, level: 80 }, false, 1),
                    (error: Error) => /onOff half failed/.test(error.message),
                    "the error must name the failed half",
                );
            } finally {
                endpointWithAct.act = originalAct;
            }
            const level = (endpoint.stateOf("levelControl") as Record<string, unknown>).currentLevel;
            assert.equal(
                level,
                percentToCurrentLevel(80),
                "the residual set must still have applied despite the onOff failure",
            );
        } finally {
            await h.close();
        }
    });
});

describe("LevelControl command conversion (§4.2, #143)", () => {
    /**
     * One row per LevelControl command variant. `run` performs the real
     * invocation (never a synthetic `ecosystemWrite` — the whole point of
     * #143 is that these commands no longer write an attribute at all, so
     * only a genuine `agent.get().command()` call proves it), and `expect`
     * is the exact `command` frame array the invocation must produce.
     *
     * The `WithOnOff` boundary (both orders) is deliberately in the same
     * table as the plain commands (T1+T2 combined): every row is the same
     * assertion — one frame array, one unmoved attribute — so splitting the
     * boundary cases into their own test would only duplicate the harness
     * setup.
     */
    it("converts every level command to setLevel/onOff, clamped, in the measured order", async () => {
        type Run = (lc: InstanceType<typeof LevelLighting>) => unknown;
        const CASES: [string, Run, CommandEventData[]][] = [
            [
                "moveToLevel(152, tt=40)",
                lc => lc.moveToLevel({ level: 152, transitionTime: 40, optionsMask: {}, optionsOverride: {} }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 60 } }],
            ],
            [
                "moveToLevel(254)",
                lc => lc.moveToLevel({ level: 254, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 100 } }],
            ],
            [
                "move(Up, null) — the +Infinity target clamps to maxLevel",
                lc => lc.move({ moveMode: LevelControl.MoveMode.Up, rate: null, optionsMask: {}, optionsOverride: {} }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 100 } }],
            ],
            [
                "move(Down, 20) — the -Infinity target clamps to minLevel",
                lc => lc.move({ moveMode: LevelControl.MoveMode.Down, rate: 20, optionsMask: {}, optionsOverride: {} }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 0 } }],
            ],
            [
                "moveToLevelWithOnOff(1) — the minLevel boundary emits level THEN off",
                lc => lc.moveToLevelWithOnOff({ level: 1, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
                [
                    { indigoDeviceId: 1, command: "setLevel", args: { level: 0 } },
                    { indigoDeviceId: 1, command: "onOff", args: { value: false } },
                ],
            ],
            [
                "moveToLevelWithOnOff(152) — any other level emits setLevel ONLY (#239)",
                // No onOff frame at all: Indigo's turn-on-to-level semantics
                // do the turn-on, and a leading turnOn's stale Z-Wave report
                // (stored level 0 after a slider-to-zero) was live-observed
                // landing Indigo's brightness state at 0 — then mirrored back
                // out to every ecosystem. Simon's ruling on #239.
                lc => lc.moveToLevelWithOnOff({ level: 152, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 60 } }],
            ],
            [
                "moveWithOnOff(Down, 10) — clamped-target boundary, not stock's -Infinity!==minLevel bug",
                lc => lc.moveWithOnOff({ moveMode: LevelControl.MoveMode.Down, rate: 10, optionsMask: {}, optionsOverride: {} }),
                [
                    { indigoDeviceId: 1, command: "setLevel", args: { level: 0 } },
                    { indigoDeviceId: 1, command: "onOff", args: { value: false } },
                ],
            ],
            [
                "stepWithOnOff(Down, 254) — a step past minLevel still clamps to the boundary",
                lc => lc.stepWithOnOff({
                    stepMode: LevelControl.StepMode.Down,
                    stepSize: 254,
                    transitionTime: null,
                    optionsMask: {},
                    optionsOverride: {},
                }),
                [
                    { indigoDeviceId: 1, command: "setLevel", args: { level: 0 } },
                    { indigoDeviceId: 1, command: "onOff", args: { value: false } },
                ],
            ],
            [
                "stop() — never reaches transition(), stays stock, emits nothing",
                lc => lc.stop({ optionsMask: {}, optionsOverride: {} }),
                [],
            ],
            [
                "stopWithOnOff() — same as stop(), never reaches transition(), emits nothing",
                lc => lc.stopWithOnOff({ optionsMask: {}, optionsOverride: {} }),
                [],
            ],
            [
                "step(Up, 300) — a step past maxLevel still clamps to the boundary",
                lc =>
                    lc.step({
                        stepMode: LevelControl.StepMode.Up,
                        stepSize: 300,
                        transitionTime: null,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 100 } }],
            ],
            [
                "moveToLevel(0) — below minLevel clamps UP to the boundary (stock's own cropValueRange)",
                lc => lc.moveToLevel({ level: 0, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
                [{ indigoDeviceId: 1, command: "setLevel", args: { level: 0 } }],
            ],
        ];

        for (const [label, run, expected] of CASES) {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
                const endpoint = only(h);
                await endpoint.act(agent => run(agent.get(LevelLighting)));
                assert.deepEqual(h.commands, expected, label);
                assert.equal(
                    (endpoint.stateOf("levelControl") as Record<string, unknown>).currentLevel,
                    1,
                    `${label}: currentLevel must not have moved`,
                );
            } finally {
                await h.close();
            }
        }
    });

    it("step(Up, 10, tt=10) clamps against the endpoint's actual current level", async () => {
        // The one row the table above cannot express without seeding state:
        // stepLogic's target is `currentLevel + stepSize`, so this proves the
        // override reads the REAL current level, not a hard-coded one.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            const endpoint = only(h);
            // A plain attribute write, not a command invocation — seeds state,
            // does not exercise the override under test.
            await endpoint.set({ levelControl: { currentLevel: 128 } } as never);

            await endpoint.act(agent =>
                agent.get(LevelLighting).step({
                    stepMode: LevelControl.StepMode.Up,
                    stepSize: 10,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "setLevel", args: { level: 54 } }]);
        } finally {
            await h.close();
        }
    });

    it("the onOff half emits even when the attribute already agrees with the target", async () => {
        // A frequency change is still a report — the same reasoning
        // IndigoOnOffServer's own class doc and BRIDGE_PROTOCOL §4.2 make for
        // the plain onOff commands. Starting the attribute at `false` and
        // then hitting the minLevel boundary (which ALSO means "off") must
        // still emit the onOff half, not skip it as a no-op.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight, { states: { onOff: false } })], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent
                    .get(LevelLighting)
                    .moveToLevelWithOnOff({ level: 1, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
            );
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setLevel", args: { level: 0 } },
                { indigoDeviceId: 1, command: "onOff", args: { value: false } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("executeIfOff lets a plain moveToLevel reach the override while the attribute reads off", async () => {
        // LevelControlServer.js:443-445's #optionsAllowExecution gate answers
        // from the OnOff ATTRIBUTE, which under no-auto-confirm is Indigo's
        // last confirmation, not the device's real state (#230). Without the
        // executeIfOff seed on LEVEL_CONTROL_INITIAL, this command would be
        // dropped before ever reaching transition() and h.commands would stay
        // empty — the assertion IS the seed.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight, { states: { onOff: false } })], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent
                    .get(LevelLighting)
                    .moveToLevel({ level: 152, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
            );
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "setLevel", args: { level: 60 } }]);
        } finally {
            await h.close();
        }
    });

    it("executeIfOff is itself writable, and a plain moveToLevel is gated once it is written false", async () => {
        // KNOWN LIMITS in LEVEL_CONTROL_INITIAL's own doc: `options` is a
        // writable attribute, so an ecosystem could in principle turn the
        // seed back off. This is a genuine attribute write (not
        // ecosystemWrite(), which only fires the change observable and would
        // never actually flip what #optionsAllowExecution reads) — the gate
        // reads `this.state.options` directly (LevelControlServer.js:437),
        // so only a real write can move it.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight, { states: { onOff: false } })], false);
            const endpoint = only(h);
            await endpoint.set({
                levelControl: { options: { executeIfOff: false, coupleColorTempToLevel: false } },
            } as never);

            await endpoint.act(agent =>
                agent
                    .get(LevelLighting)
                    .moveToLevel({ level: 152, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
            );
            assert.deepEqual(h.commands, [], "a plain moveToLevel must be dropped once executeIfOff reads false");

            // WithOnOff never depended on the gate in the first place — see
            // the class doc's "KNOWN LIMITS" paragraph.
            await endpoint.act(agent =>
                agent.get(LevelLighting).moveToLevelWithOnOff({
                    level: 152,
                    transitionTime: null,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setLevel", args: { level: 60 } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("closes the ghost-timer chain: a WithOnOff at min no longer moves onOff (PR#230 review)", async () => {
        // The exact scenario PR#230's review described: a dimmer command
        // carrying WithOnOff while a timed-on countdown is running. Stock
        // `couple()` would have written `onOff` to `false` directly here,
        // stranding the timer between IndigoOnOffServer's "replay" and
        // "ghost" cases (see that class's own doc). #143 never calls
        // `couple()` at all — the override replaces `transition()` outright
        // — so nothing about the countdown is touched by this command.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            const endpoint = only(h);
            await h.registry.setState(1, { onOff: true });

            await endpoint.act(agent =>
                agent
                    .get(OnOffLighting)
                    .onWithTimedOff({ onOffControl: { acceptOnlyWhenOn: false }, onTime: 6000, offWaitTime: 0 }),
            );
            h.commands.length = 0;

            await endpoint.act(agent =>
                agent
                    .get(LevelLighting)
                    .moveToLevelWithOnOff({ level: 1, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
            );
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setLevel", args: { level: 0 } },
                { indigoDeviceId: 1, command: "onOff", args: { value: false } },
            ]);
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onOff,
                true,
                "the dimmer command must not have moved onOff by itself — the countdown must not be strandable",
            );
            assert.ok(
                ((endpoint.stateOf("onOff") as Record<string, unknown>).onTime as number) > 0,
                "the countdown must still be running, untouched by the dimmer command",
            );

            // Indigo's own confirmation is what closes the countdown for real.
            await h.registry.setState(1, { onOff: false });
            assert.equal((endpoint.stateOf("onOff") as Record<string, unknown>).onOff, false);
            assert.equal(
                (endpoint.stateOf("onOff") as Record<string, unknown>).onTime,
                0,
                "super.off() must have stopped the real countdown",
            );
        } finally {
            await h.close();
        }
    });

    it("every level-bearing role reports LevelControl invocations through the sink (level fence)", async () => {
        // Dropping IndigoLevelControlServer from any role's deviceType().with()
        // is a SILENT regression on that role alone: agent.get() resolves by
        // behaviour id, so the stock server would answer, write the attribute
        // itself, and emit nothing — and no other test exercises a level
        // invocation on colorTemperatureLight. Mirrors the OnOff five-role
        // fence.
        const roles = [Role.dimmableLight, Role.colorTemperatureLight, Role.extendedColorLight];
        for (const role of roles) {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, role)], false);
                const endpoint = only(h);
                await endpoint.act(agent =>
                    agent
                        .get(LevelLighting)
                        .moveToLevel({ level: 152, transitionTime: null, optionsMask: {}, optionsOverride: {} }),
                );
                assert.deepEqual(
                    h.commands,
                    [{ indigoDeviceId: 1, command: "setLevel", args: { level: 60 } }],
                    `role ${role}: the invocation must emit through the sink`,
                );
                assert.equal(
                    (endpoint.stateOf("levelControl") as Record<string, unknown>).currentLevel,
                    1,
                    `role ${role}: the invocation must not move the attribute`,
                );
            } finally {
                await h.close();
            }
        }
    });

    it("a level invocation does not also trigger WATCH_LEVEL (no double emission)", async () => {
        // The override never writes `currentLevel`, so the backstop watcher
        // must see nothing — a regression here would be a second, spurious
        // setLevel frame for every real command.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.dimmableLight)], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent.get(LevelLighting).moveToLevel({
                    level: 152,
                    transitionTime: null,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.equal(h.commands.length, 1, "a level invocation emitted more than one frame");
        } finally {
            await h.close();
        }
    });
});

describe("ColorControl command conversion (§4.2, #143)", () => {
    /**
     * One row per hue/saturation/colour-temperature command on
     * `extendedColorLight`. `seed` is a plain attribute write (not a command
     * invocation) that puts the endpoint in the state the row's math assumes;
     * `run` is the real invocation under test.
     */
    it("converts every hue/saturation/CT command to setColor/setColorTemp, clamped", async () => {
        type Seed = (endpoint: Endpoint) => Promise<void>;
        type Run = (cc: InstanceType<typeof ColorFull>) => unknown;
        const CASES: [string, Seed, Run, CommandEventData][] = [
            [
                "moveToHue(148, Shortest)",
                async endpoint => endpoint.set({ colorControl: { currentSaturation: 203 } } as never),
                cc =>
                    cc.moveToHue({
                        hue: 148,
                        direction: ColorControl.Direction.Shortest,
                        transitionTime: 10,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColor", args: { hue: 210, saturation: 80 } },
            ],
            [
                "stepHue(Up, 100) — cyclic wraparound past 254",
                async endpoint =>
                    endpoint.set({ colorControl: { currentHue: 200, currentSaturation: 203 } } as never),
                cc =>
                    cc.stepHue({
                        stepMode: ColorControl.StepMode.Up,
                        stepSize: 100,
                        transitionTime: 10,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColor", args: { hue: 65, saturation: 80 } },
            ],
            [
                "moveToSaturation(100)",
                async endpoint => endpoint.set({ colorControl: { currentHue: 148 } } as never),
                cc => cc.moveToSaturation({ saturation: 100, transitionTime: 10, optionsMask: {}, optionsOverride: {} }),
                { indigoDeviceId: 1, command: "setColor", args: { hue: 210, saturation: 39 } },
            ],
            [
                "stepSaturation(Down, 50)",
                async endpoint =>
                    endpoint.set({ colorControl: { currentHue: 148, currentSaturation: 203 } } as never),
                cc =>
                    cc.stepSaturation({
                        stepMode: ColorControl.StepMode.Down,
                        stepSize: 50,
                        transitionTime: 10,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColor", args: { hue: 210, saturation: 60 } },
            ],
            [
                "moveToColorTemperature(320)",
                async () => {},
                cc =>
                    cc.moveToColorTemperature({
                        colorTemperatureMireds: 320,
                        transitionTime: 10,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 320 } },
            ],
            [
                "moveColorTemperature(Up, min=200, max=400)",
                async () => {},
                cc =>
                    cc.moveColorTemperature({
                        moveMode: ColorControl.MoveMode.Up,
                        rate: 10,
                        colorTemperatureMinimumMireds: 200,
                        colorTemperatureMaximumMireds: 400,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 400 } },
            ],
            [
                "stepColorTemperature(Down, 50) — from a seeded 300 mireds",
                async endpoint => endpoint.set({ colorControl: { colorTemperatureMireds: 300 } } as never),
                cc =>
                    cc.stepColorTemperature({
                        stepMode: ColorControl.StepMode.Down,
                        stepSize: 50,
                        transitionTime: 10,
                        colorTemperatureMinimumMireds: 0,
                        colorTemperatureMaximumMireds: 0,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 250 } },
            ],
        ];

        for (const [label, seed, run, expected] of CASES) {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
                // The on-state is incidental (#235: colour forwards while off
                // too — colorControlDefaults seeds executeIfOff, like level).
                // Confirmed on anyway so these rows read as the common case;
                // the while-off forward has its own dedicated pin below.
                await h.registry.setState(1, { onOff: true });
                const endpoint = only(h);
                await seed(endpoint);
                h.commands.length = 0;
                const before = endpoint.stateOf("colorControl") as Record<string, unknown>;
                const beforeSnapshot = {
                    currentHue: before.currentHue,
                    currentSaturation: before.currentSaturation,
                    colorTemperatureMireds: before.colorTemperatureMireds,
                };
                await endpoint.act(agent => run(agent.get(ColorFull)));
                assert.deepEqual(h.commands, [expected], label);
                // The core #143 invariant: the override reports the command,
                // it never writes the attribute.
                const after = endpoint.stateOf("colorControl") as Record<string, unknown>;
                assert.deepEqual(
                    {
                        currentHue: after.currentHue,
                        currentSaturation: after.currentSaturation,
                        colorTemperatureMireds: after.colorTemperatureMireds,
                    },
                    beforeSnapshot,
                    `${label}: colour attributes must not have moved`,
                );
            } finally {
                await h.close();
            }
        }
    });

    it("carries the CT commands on colorTemperatureLight too, via the CT-only twin class", async () => {
        // Same math as the matrix above's three CT rows, proving
        // IndigoColorTemperatureControlServer (not just the full class) is
        // wired in and overrides its own three *Logic methods.
        type Run = (cc: InstanceType<typeof ColorCt>) => unknown;
        const CASES: [string, Run, CommandEventData][] = [
            [
                "moveToColorTemperature(320)",
                cc =>
                    cc.moveToColorTemperature({
                        colorTemperatureMireds: 320,
                        transitionTime: 10,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 320 } },
            ],
            [
                "moveColorTemperature(Up, min=200, max=400)",
                cc =>
                    cc.moveColorTemperature({
                        moveMode: ColorControl.MoveMode.Up,
                        rate: 10,
                        colorTemperatureMinimumMireds: 200,
                        colorTemperatureMaximumMireds: 400,
                        optionsMask: {},
                        optionsOverride: {},
                    }),
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 400 } },
            ],
        ];
        for (const [label, run, expected] of CASES) {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, Role.colorTemperatureLight)], false);
                // See the matrix above: colour commands are gated while
                // unconfirmed-off (commit 1).
                await h.registry.setState(1, { onOff: true });
                const endpoint = only(h);
                const before = (endpoint.stateOf("colorControl") as Record<string, unknown>).colorTemperatureMireds;
                await endpoint.act(agent => run(agent.get(ColorCt)));
                assert.deepEqual(h.commands, [expected], label);
                assert.equal(
                    (endpoint.stateOf("colorControl") as Record<string, unknown>).colorTemperatureMireds,
                    before,
                    `${label}: colorTemperatureMireds must not have moved`,
                );
            } finally {
                await h.close();
            }
        }
    });

    it("a plain moveToHue emits while off — colour forwards exactly like level (#235)", async () => {
        // Simon's #235 ruling (see colorControlDefaults' doc): the bridge
        // sends, Indigo decides — withholding the command loses the colour
        // for nothing, and whatever an RGB driver does with a colour write on
        // an off lamp is Indigo/driver semantics, not the bridge's to
        // second-guess. colorControlDefaults() seeds executeIfOff: true, so
        // Matter's #optionsAllowExecution gate (ColorControlServer.js:
        // 1444-1446) — whose premise broke under no-auto-confirm, where the
        // attribute means "Indigo's last confirmation" — lets the command
        // through to moveToHueLogic, same as the level test of this shape.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight, { states: { onOff: false } })], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent.get(ColorFull).moveToHue({
                    hue: 148,
                    direction: ColorControl.Direction.Shortest,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setColor", args: { hue: 210, saturation: 0 } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("moveToColorTemperature emits while off on the CT-only role — the twin gets the seed too (#235)", async () => {
        // colorControlDefaults(false) — the hueSaturation-free path
        // IndigoColorTemperatureControlServer's role uses — must carry the
        // same executeIfOff seed, or CT-while-off silently reverts to the
        // stock drop on colorTemperatureLight alone while the docs (§4.2,
        // CHANGELOG 2026.20.0) promise the forward for both colour roles.
        const h = await harness();
        try {
            await h.registry.reconcile(
                [spec(1, Role.colorTemperatureLight, { states: { onOff: false } })],
                false,
            );
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent.get(ColorCt).moveToColorTemperature({
                    colorTemperatureMireds: 320,
                    transitionTime: 0,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 320 } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("moveToHueAndSaturation emits ONE frame carrying BOTH requested values", async () => {
        // Regression fence: stock composes moveToHueLogic + moveToSaturationLogic
        // (ColorControlServer.js:577-582), which — now that neither writes an
        // attribute — would emit the new hue paired with the STALE saturation
        // still sitting in state. The override reports both requested values
        // in one frame instead.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            // The on-state is incidental here (#235: colour forwards while off too).
            await h.registry.setState(1, { onOff: true });
            const endpoint = only(h);
            // A stale saturation the composed-stock bug would have leaked.
            await endpoint.set({ colorControl: { currentSaturation: 10 } } as never);

            await endpoint.act(agent =>
                agent.get(ColorFull).moveToHueAndSaturation({
                    hue: 100,
                    saturation: 200,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "setColor", args: { hue: 142, saturation: 79 } }]);
            const after = endpoint.stateOf("colorControl") as Record<string, unknown>;
            assert.equal(after.currentHue, 0, "the invocation must not have moved currentHue");
            assert.equal(after.currentSaturation, 10, "the invocation must not have moved currentSaturation");
        } finally {
            await h.close();
        }
    });

    it("switchColorMode is a no-op — the 0.17.8 hsvToXy/xyToHsv mixup never corrupts currentSaturation", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            const endpoint = only(h);
            // The on-state is incidental here (#235: colour forwards while off too).
            await h.registry.setState(1, { onOff: true, hue: 210, saturation: 80 });
            const before = (endpoint.stateOf("colorControl") as Record<string, unknown>).currentSaturation;
            h.commands.length = 0;

            // HS -> XY. moveToColorLogic is converted (commit 6) and emits
            // its own setColor from the xy target — it never reads or writes
            // currentHue/currentSaturation, so it cannot be the mechanism
            // under test either way.
            await endpoint.act(agent =>
                agent
                    .get(ColorFull)
                    .moveToColor({ colorX: 20000, colorY: 20000, transitionTime: 10, optionsMask: {}, optionsOverride: {} }),
            );
            // XY -> HS. This is the buggy direction in stock 0.17.8 (calls
            // hsvToXy where xyToHsv is needed) — measured to move
            // currentSaturation from 200 to 82 in the design's own probe.
            await endpoint.act(agent =>
                agent.get(ColorFull).moveToHue({
                    hue: 148,
                    direction: ColorControl.Direction.Shortest,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );

            assert.equal(
                (endpoint.stateOf("colorControl") as Record<string, unknown>).currentSaturation,
                before,
                "switchColorMode must not have touched currentSaturation in either direction",
            );
            // Both calls now emit: moveToColorLogic reports the xy target
            // converted through xyToHsv, and moveToHueLogic reports its own
            // target paired with currentSaturation — which is still the
            // SEEDED value (203 → 80%), proving switchColorMode never
            // touched it in between.
            assert.deepEqual(h.commands, [
                { indigoDeviceId: 1, command: "setColor", args: { hue: 235, saturation: 9 } },
                { indigoDeviceId: 1, command: "setColor", args: { hue: 210, saturation: 80 } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("stepColor writes nothing — probe-confirmed to write silently in stock, so it is a debug-logged no-op", async () => {
        // HR-6: stepColorLogic is the one continuous colour command that
        // contradicts the "move*/step* with no target write nothing" pattern
        // (moveColorLogic genuinely writes nothing and is left stock). A
        // silent write nothing here watches is not acceptable, so it is
        // overridden to no-op + debug log instead of left stock.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            // The on-state is incidental here (#235: colour forwards while off) — the
            // lamp must be on for this to reach the override under test rather
            // than being dropped at the gate first.
            await h.registry.setState(1, { onOff: true });
            const endpoint = only(h);
            const before = endpoint.stateOf("colorControl") as { currentX: number; currentY: number };

            await endpoint.act(agent =>
                agent
                    .get(ColorFull)
                    .stepColor({ stepX: 500, stepY: 500, transitionTime: 10, optionsMask: {}, optionsOverride: {} }),
            );

            assert.deepEqual(h.commands, []);
            const after = endpoint.stateOf("colorControl") as { currentX: number; currentY: number };
            assert.equal(after.currentX, before.currentX, "stepColor must not move currentX");
            assert.equal(after.currentY, before.currentY, "stepColor must not move currentY");
        } finally {
            await h.close();
        }
    });

    it("moveToColor converts CIE xy to setColor via the stock xyToHsv conversion", async () => {
        // #143 commit 6. 0.35/0.35 is the design's own probe value: xyToHsv
        // resolves it to ~39.4° hue and ~12.6% saturation, rounding to the
        // exact figures pinned below — matter.js's own conversion, not a
        // second one this file maintains.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            // The on-state is incidental here (#235: colour forwards while off too).
            await h.registry.setState(1, { onOff: true });
            const endpoint = only(h);
            const before = endpoint.stateOf("colorControl") as Record<string, unknown>;
            await endpoint.act(agent =>
                agent.get(ColorFull).moveToColor({
                    colorX: Math.round(0.35 * 65536),
                    colorY: Math.round(0.35 * 65536),
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h.commands, [{ indigoDeviceId: 1, command: "setColor", args: { hue: 39, saturation: 13 } }]);
            const after = endpoint.stateOf("colorControl") as Record<string, unknown>;
            assert.equal(after.currentHue, before.currentHue, "the invocation must not have moved currentHue");
            assert.equal(
                after.currentSaturation,
                before.currentSaturation,
                "the invocation must not have moved currentSaturation",
            );
            assert.equal(
                after.colorTemperatureMireds,
                before.colorTemperatureMireds,
                "the invocation must not have moved colorTemperatureMireds",
            );
        } finally {
            await h.close();
        }
    });

    it("moveColor still writes and emits nothing — stock's own no-target-value transition", async () => {
        // Confirms the class doc's claim for the one continuous xy command
        // that is NOT stepColor: no targetValue means Transitions.start
        // returns before writing anything (probe-confirmed, unlike stepColor).
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            // The on-state is incidental here (#235: colour forwards while off) — the
            // lamp must be on for this to reach stock's own transition logic
            // rather than being dropped at the gate first.
            await h.registry.setState(1, { onOff: true });
            const endpoint = only(h);
            const before = endpoint.stateOf("colorControl") as { currentX: number; currentY: number };
            await endpoint.act(agent =>
                agent.get(ColorFull).moveColor({ rateX: 500, rateY: 500, optionsMask: {}, optionsOverride: {} }),
            );
            assert.deepEqual(h.commands, []);
            const after = endpoint.stateOf("colorControl") as { currentX: number; currentY: number };
            assert.equal(after.currentX, before.currentX, "moveColor must not move currentX");
            assert.equal(after.currentY, before.currentY, "moveColor must not move currentY");
        } finally {
            await h.close();
        }
    });

    it("every colour-bearing role reports ColorControl invocations through the sink (colour fence)", async () => {
        // Mirrors the LevelControl fence: dropping IndigoColorControlServer/
        // IndigoColorTemperatureControlServer from a role's deviceType() is a
        // silent regression on that role alone.
        const h1 = await harness();
        try {
            await h1.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            // The on-state is incidental here (#235: colour forwards while off too).
            await h1.registry.setState(1, { onOff: true });
            const endpoint = only(h1);
            await endpoint.act(agent =>
                agent.get(ColorFull).moveToColorTemperature({
                    colorTemperatureMireds: 320,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h1.commands, [
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 320 } },
            ]);
        } finally {
            await h1.close();
        }

        const h2 = await harness();
        try {
            await h2.registry.reconcile([spec(1, Role.colorTemperatureLight)], false);
            await h2.registry.setState(1, { onOff: true });
            const endpoint = only(h2);
            await endpoint.act(agent =>
                agent.get(ColorCt).moveToColorTemperature({
                    colorTemperatureMireds: 320,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.deepEqual(h2.commands, [
                { indigoDeviceId: 1, command: "setColorTemp", args: { colorTempMireds: 320 } },
            ]);
        } finally {
            await h2.close();
        }
    });

    it("a colour invocation does not also trigger WATCH_COLOR_TEMP/WATCH_HUE/WATCH_SATURATION (no double emission)", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.extendedColorLight)], false);
            // The on-state is incidental here (#235: colour forwards while off too).
            await h.registry.setState(1, { onOff: true });
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent.get(ColorFull).moveToColorTemperature({
                    colorTemperatureMireds: 320,
                    transitionTime: 10,
                    optionsMask: {},
                    optionsOverride: {},
                }),
            );
            assert.equal(h.commands.length, 1, "a colour invocation emitted more than one frame");
        } finally {
            await h.close();
        }
    });
});

describe("cluster composition is unchanged from stock (HR-1)", () => {
    /**
     * Per converting role: build the Indigo endpoint via the ordinary
     * registry path, then build a bare STOCK endpoint — same `.with()`
     * feature set, no `Indigo*Server` overrides — seeded with the Indigo
     * endpoint's own already-valid state (sidesteps re-deriving matter.js's
     * mandatory-conformance seed values by hand, and can never disagree with
     * what {@link createEndpoint} actually produces). Then diff the two on
     * exactly what a controller can see: `attributeList`/
     * `acceptedCommandList`/`featureMap` per behaviour, and the descriptor's
     * `serverList`/`deviceTypeList`. Equal here is HR-1 made a test: nothing
     * about a converting role's `.with()` feature composition changed, only
     * its command HANDLING did — no accessory recreation, no migration.
     */
    const CASES: [string, RoleValue, () => EndpointType, string[]][] = [
        [
            "dimmableLight",
            Role.dimmableLight,
            () => DimmableLightDevice.with(BridgedDeviceBasicInformationServer),
            ["onOff", "levelControl"],
        ],
        [
            "colorTemperatureLight",
            Role.colorTemperatureLight,
            () => ColorTemperatureLightDevice.with(BridgedDeviceBasicInformationServer),
            ["onOff", "levelControl", "colorControl"],
        ],
        [
            "extendedColorLight",
            Role.extendedColorLight,
            // The baseline used to be the SAME expression as production
            // (self-referential — it could never catch production drifting
            // from stock). It is rebuilt from a genuinely bare stock
            // ColorControlServer, `.alter()`-ed identically to
            // IndigoColorControlServer's own composition, so the fence's
            // real claim holds: the Indigo* override class is
            // composition-neutral against an EQUIVALENTLY-CONFIGURED stock
            // server, not against a copy of itself. The `HueSaturation`
            // deviation from bare stock is deliberate and already documented
            // at IndigoColorControlServer's own class doc.
            () =>
                ExtendedColorLightDevice.with(
                    BridgedDeviceBasicInformationServer,
                    ColorControlServer.with("HueSaturation", "Xy", "ColorTemperature").alter({
                        attributes: { remainingTime: { optional: false } },
                    }),
                ),
            ["onOff", "levelControl", "colorControl"],
        ],
    ];

    for (const [label, role, stockDeviceType, behaviors] of CASES) {
        it(`${label}: attributeList/acceptedCommandList/featureMap and serverList/deviceTypeList match stock`, async () => {
            const h = await harness();
            try {
                await h.registry.reconcile([spec(1, role)], false);
                const indigo = only(h);

                const initial: Record<string, unknown> = {
                    bridgedDeviceBasicInformation: {
                        ...(indigo.stateOf("bridgedDeviceBasicInformation") as Record<string, unknown>),
                        uniqueId: "stock-twin-unique-id",
                        serialNumber: "stock-twin-serial",
                    },
                };
                for (const behavior of behaviors) {
                    initial[behavior] = { ...(indigo.stateOf(behavior) as Record<string, unknown>) };
                }
                const stock = new Endpoint(stockDeviceType() as never, { id: "stock-twin", ...initial } as never);
                await h.aggregator.add(stock);

                for (const behavior of behaviors) {
                    const indigoState = indigo.stateOf(behavior) as {
                        attributeList: number[];
                        acceptedCommandList: number[];
                    };
                    const stockState = stock.stateOf(behavior) as {
                        attributeList: number[];
                        acceptedCommandList: number[];
                    };
                    assert.deepEqual(
                        [...indigoState.attributeList].sort(),
                        [...stockState.attributeList].sort(),
                        `${label}.${behavior}: attributeList differs from stock`,
                    );
                    assert.deepEqual(
                        [...indigoState.acceptedCommandList].sort(),
                        [...stockState.acceptedCommandList].sort(),
                        `${label}.${behavior}: acceptedCommandList differs from stock`,
                    );
                    assert.deepEqual(
                        indigo.featuresOf(behavior),
                        stock.featuresOf(behavior),
                        `${label}.${behavior}: featureMap differs from stock`,
                    );
                }

                const indigoDescriptor = indigo.stateOf("descriptor") as {
                    serverList: unknown[];
                    deviceTypeList: unknown;
                };
                const stockDescriptor = stock.stateOf("descriptor") as {
                    serverList: unknown[];
                    deviceTypeList: unknown;
                };
                assert.deepEqual(
                    [...indigoDescriptor.serverList].map(Number).sort(),
                    [...stockDescriptor.serverList].map(Number).sort(),
                    `${label}: descriptor.serverList differs from stock`,
                );
                assert.deepEqual(
                    indigoDescriptor.deviceTypeList,
                    stockDescriptor.deviceTypeList,
                    `${label}: descriptor.deviceTypeList differs from stock`,
                );
            } finally {
                await h.close();
            }
        });
    }
});

describe("window covering commands (§4.2, E4)", () => {
    it("maps every movement command to a §4.2 position, inverted", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(6, Role.windowCovering, { states: { position: 100 } })], false);
            const endpoint = only(h);

            // 3000 percent100ths is 30 % CLOSED, i.e. §4.2 position 70.
            await endpoint.act(agent =>
                agent.get(PositionAwareCovering).goToLiftPercentage({ liftPercent100thsValue: 3000 }),
            );
            await endpoint.act(agent => agent.get(PositionAwareCovering).downOrClose());
            await endpoint.act(agent => agent.get(PositionAwareCovering).upOrOpen());

            assert.deepEqual(h.commands, [
                { indigoDeviceId: 6, command: "goToPosition", args: { position: 70 } },
                { indigoDeviceId: 6, command: "goToPosition", args: { position: 0 } },
                { indigoDeviceId: 6, command: "goToPosition", args: { position: 100 } },
            ]);
        } finally {
            await h.close();
        }
    });

    it("reports a stop, and settles the ecosystem's in-motion rendering", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(6, Role.windowCovering, { states: { position: 100 } })], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent.get(PositionAwareCovering).goToLiftPercentage({ liftPercent100thsValue: 5000 }),
            );
            h.commands.length = 0;

            await endpoint.act(agent => agent.get(PositionAwareCovering).stopMotion());
            assert.deepEqual(h.commands, [{ indigoDeviceId: 6, command: "stopMotion", args: {} }]);
            // super.handleStopMovement() settles target onto current, so the
            // accessory stops animating. It is a statement about the request,
            // not a claim about where the blind ended up.
            const state = endpoint.stateOf("windowCovering") as Record<string, number>;
            assert.equal(state.targetPositionLiftPercent100ths, state.currentPositionLiftPercent100ths);
        } finally {
            await h.close();
        }
    });

    it("does not move the position itself — matter.js's instant snap is overridden", async () => {
        // PRD §5.2: "Must implement handleMovement() — matter.js's default snaps
        // to target instantly". A bridge that snapped would tell every ecosystem
        // the blind had arrived before Indigo had even been asked.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(6, Role.windowCovering, { states: { position: 100 } })], false);
            const endpoint = only(h);
            await endpoint.act(agent =>
                agent.get(PositionAwareCovering).goToLiftPercentage({ liftPercent100thsValue: 8000 }),
            );
            assert.equal(
                (endpoint.stateOf("windowCovering") as Record<string, unknown>).currentPositionLiftPercent100ths,
                0,
                "the covering snapped to target without Indigo",
            );

            // Indigo reports the blind arrived; only now does the position move.
            await h.registry.setState(6, { position: 20 });
            assert.equal(
                (endpoint.stateOf("windowCovering") as Record<string, unknown>).currentPositionLiftPercent100ths,
                8000,
            );
        } finally {
            await h.close();
        }
    });

    it("does not echo its own position push back as a goToPosition", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(6, Role.windowCovering)], false);
            await h.registry.setState(6, { position: 40 });
            await h.registry.setState(6, { position: 0 });
            assert.deepEqual(h.commands, []);
        } finally {
            await h.close();
        }
    });
});

// ---------------------------------------------------------------------------
// Issue #220: PowerSource / battery
// ---------------------------------------------------------------------------

/** The Descriptor cluster's own account of what an endpoint carries. */
function descriptorOf(endpoint: Endpoint): { deviceTypeList: Array<{ deviceType: unknown }>; serverList: unknown[] } {
    return endpoint.stateOf("descriptor") as {
        deviceTypeList: Array<{ deviceType: unknown }>;
        serverList: unknown[];
    };
}

describe("PowerSource / battery (issue #220)", () => {
    it("adds PowerSource (cluster 47) and device type 0x0011 to every role when battery: true", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile(
                [
                    spec(1, Role.contactSensor, { battery: true }),
                    spec(2, Role.onOffLight, { battery: true }),
                    spec(3, Role.thermostat, { battery: true, states: { systemMode: "off" } }),
                ],
                false,
            );
            for (const id of [1, 2, 3]) {
                const endpoint = [...h.aggregator.parts].find(part => part.id === endpointIdFor(id));
                assert.ok(endpoint !== undefined, `device ${id} missing`);
                const descriptor = descriptorOf(endpoint);
                assert.ok(
                    descriptor.serverList.map(Number).includes(47),
                    `device ${id} serverList ${JSON.stringify(descriptor.serverList)} missing PowerSource (47)`,
                );
                assert.ok(
                    descriptor.deviceTypeList.some(dt => Number(dt.deviceType) === 17),
                    `device ${id} deviceTypeList missing PowerSource device type (17/0x0011)`,
                );
                assert.equal(Object.keys(endpoint.state).includes("powerSource"), true);
            }
        } finally {
            await h.close();
        }
    });

    it("carries no PowerSource at all when battery: false", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: false })], false);
            const endpoint = only(h);
            const descriptor = descriptorOf(endpoint);
            assert.ok(!descriptor.serverList.map(Number).includes(47));
            assert.equal(Object.keys(endpoint.state).includes("powerSource"), false);
        } finally {
            await h.close();
        }
    });

    it("REGRESSION (measured trap, §0): seeds batPercentRemaining null even with no batteryLevel in states", async () => {
        // The whole feature ships dead without this: an endpoint built without
        // `batPercentRemaining` in its initial state never gets attribute 12
        // into `attributeList` at all, so a LATER write succeeds silently into
        // an attribute no controller can ever read.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.contactSensor, { battery: true })], false);
            const endpoint = only(h);
            const power = endpoint.stateOf("powerSource") as { batPercentRemaining: unknown; attributeList?: unknown[] };
            assert.equal(power.batPercentRemaining, null);
            // Unconditional, not an if-guard: an if-guard here would pass
            // vacuously the moment a matter.js upgrade stopped exposing
            // `attributeList` at all, which is exactly the kind of silent
            // regression this REGRESSION test exists to catch.
            assert.ok(power.attributeList !== undefined, "attributeList must exist to check for batPercentRemaining");
            assert.ok(
                power.attributeList.map(Number).includes(12),
                `attributeList ${JSON.stringify(power.attributeList)} missing batPercentRemaining (12)`,
            );
        } finally {
            await h.close();
        }
    });

    it("TRAP (measured, §0): an endpoint built WITHOUT batPercentRemaining never gets attribute 12", async () => {
        // The negative direction of the REGRESSION test above, pinned against
        // matter.js directly rather than through `createEndpoint` (which
        // always seeds `BATTERY_INITIAL`) — this is the mechanism itself, so
        // a future matter.js upgrade that changes it fails loudly here rather
        // than silently stop mattering.
        const h = await harness();
        try {
            const trap = new Endpoint(
                OnOffLightDevice.with(BridgedDeviceBasicInformationServer, PowerSourceServer.with("Battery")),
                { id: "trap-test", bridgedDeviceBasicInformation: { reachable: true } } as never,
            );
            await h.aggregator.add(trap);
            const power = trap.stateOf("powerSource") as { attributeList?: unknown[] };
            assert.ok(power.attributeList !== undefined, "attributeList must exist to check for batPercentRemaining");
            assert.ok(
                !power.attributeList.map(Number).includes(12),
                `attributeList ${JSON.stringify(power.attributeList)} unexpectedly HAS batPercentRemaining ` +
                    "without ever seeding it — the trap this test pins has changed",
            );
            // The other half of the trap: a later write still succeeds
            // silently, with no thrown error to say the attribute was never
            // declared.
            await trap.set({ powerSource: { batPercentRemaining: 48 } } as never);
            assert.equal((trap.stateOf("powerSource") as { batPercentRemaining: unknown }).batPercentRemaining, 48);
        } finally {
            await h.close();
        }
    });

    it("converts batteryLevel via set_state at the documented boundaries", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.contactSensor, { battery: true })], false);
            const endpoint = only(h);
            const read = (): unknown => (endpoint.stateOf("powerSource") as { batPercentRemaining: unknown }).batPercentRemaining;
            for (const [percent, expected] of [[1, 2], [24, 48], [77, 154], [100, 200]] as const) {
                await h.registry.setState(1, { batteryLevel: percent });
                assert.equal(read(), expected, `${percent}% should be ${expected}`);
            }
        } finally {
            await h.close();
        }
    });

    it("clamps an out-of-range batteryLevel rather than throwing", async () => {
        assert.equal(percentToBatteryRemaining(150), 200);

        // The integration claim the name makes, not only the pure conversion:
        // a live battery endpoint must accept the same out-of-range push.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: true })], false);
            await h.registry.setState(1, { batteryLevel: 150 });
            const endpoint = only(h);
            assert.equal((endpoint.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining, 200);
        } finally {
            await h.close();
        }
    });

    it("applies a batteryLevel-only set_state on a battery endpoint of every role, no refusal", async () => {
        const h = await harness();
        try {
            const roles = Object.values(Role) as RoleValue[];
            await h.registry.reconcile(
                roles.map((role, index) => spec(920_000 + index, role, { battery: true })),
                false,
            );
            for (const [index, role] of roles.entries()) {
                const id = 920_000 + index;
                await h.registry.setState(id, { batteryLevel: 55 });
                const endpoint = [...h.aggregator.parts].find(part => part.id === endpointIdFor(id));
                assert.equal(
                    (endpoint?.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining,
                    110,
                    `${role} batteryLevel push`,
                );
            }
        } finally {
            await h.close();
        }
    });

    it("lands onOff AND batteryLevel from the same set_state on a battery onOffLight (no #201 regression)", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: true })], false);
            await h.registry.setState(1, { onOff: true, batteryLevel: 48 });
            const endpoint = only(h);
            assert.equal((endpoint.stateOf("onOff") as Record<string, unknown>).onOff, true);
            assert.equal((endpoint.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining, 96);
        } finally {
            await h.close();
        }
    });

    it("a failed onOff half still lets batteryLevel land via the residual set (half-apply, battery)", async () => {
        // The battery twin of the plain half-apply test in the onOff commands
        // describe block below: `applyStates` runs the onOff act and the
        // residual set (which now carries `powerSource`) as two transactions,
        // and a failure in the first must not strand the second.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: true })], false);
            const endpoint = only(h);
            const endpointWithAct = endpoint as unknown as { act: (...args: unknown[]) => Promise<unknown> };
            const originalAct = endpointWithAct.act;
            let firstCall = true;
            endpointWithAct.act = function (...args: unknown[]) {
                if (firstCall) {
                    firstCall = false;
                    return Promise.reject(new Error("boom"));
                }
                return originalAct.apply(endpoint, args);
            };
            try {
                await assert.rejects(
                    async () => applyStates(endpoint, Role.onOffLight, { onOff: true, batteryLevel: 48 }, true, 1),
                    (error: Error) => /onOff half failed/.test(error.message),
                    "the error must name the failed half",
                );
            } finally {
                endpointWithAct.act = originalAct;
            }
            const power = (endpoint.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining;
            assert.equal(power, 96, "the battery half must still have applied despite the onOff failure");
        } finally {
            await h.close();
        }
    });

    it("refuses batteryLevel on a non-battery endpoint, and writes nothing", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(123456789, Role.onOffLight, { battery: false })], false);
            const error = await rejects(
                () => h.registry.setState(123456789, { batteryLevel: 77 }),
                ErrorCode.malformedArgs,
            );
            assert.equal(
                error.message,
                "endpoint 123456789 was not created with a battery, so batteryLevel cannot be " +
                    "published; re-export the device (§4.1)",
            );
            const endpoint = only(h);
            assert.equal(Object.keys(endpoint.state).includes("powerSource"), false);
        } finally {
            await h.close();
        }
    });

    it("still refuses a key the role does not speak — the split did not widen acceptance", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: true })], false);
            await rejects(() => h.registry.setState(1, { level: 55 }), ErrorCode.malformedArgs);
        } finally {
            await h.close();
        }
    });

    it("a consumed batteryLevel keeps a stranger key from refusing the whole push (skew tolerance)", async () => {
        // The version-skew case `applyStates`' own doc describes: a battery
        // reading this bridge understood must not be thrown away just because
        // it arrived alongside a key this build has never heard of.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: true })], false);
            await h.registry.setState(1, { batteryLevel: 48, bogus: 1 });
            const endpoint = only(h);
            assert.equal((endpoint.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining, 96);
        } finally {
            await h.close();
        }
    });

    it("a stranger key ALONE (no consumable battery) is still refused wholesale", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight, { battery: true })], false);
            await rejects(() => h.registry.setState(1, { bogus: 1 }), ErrorCode.malformedArgs);
        } finally {
            await h.close();
        }
    });

    it("the same skew tolerance applies to createEndpoint's initial states", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile(
                [spec(1, Role.onOffLight, { battery: true, states: { batteryLevel: 48, bogus: 1 } })],
                false,
            );
            const endpoint = only(h);
            assert.equal((endpoint.stateOf("powerSource") as Record<string, unknown>).batPercentRemaining, 96);
        } finally {
            await h.close();
        }
    });

    it("upsert: a battery GAIN keeps the endpoint number, adds the cluster, and bumps ConfigurationVersion", async () => {
        const h = await harness();
        const version = (): number =>
            (h.node.state.basicInformation as { configurationVersion?: number }).configurationVersion ?? 0;
        try {
            const created = await h.registry.upsert(spec(1, Role.onOffLight, { battery: false }));
            const before = version();

            const upserted = await h.registry.upsert(spec(1, Role.onOffLight, { battery: true }));

            assert.equal(upserted.endpointNumber, created.endpointNumber, "the endpoint number must survive");
            assert.equal(version(), before + 1, "a battery gain is a configuration change");
            const endpoint = only(h);
            assert.ok(descriptorOf(endpoint).serverList.map(Number).includes(47));
        } finally {
            await h.close();
        }
    });

    it("upsert: bumps ConfigurationVersion even when the battery-gain recreate's create() fails", async () => {
        // `closeOne` succeeds (the old endpoint really is gone) but `create`
        // throws on a state key `onOffLight` cannot consume — the exact
        // half-applied shape `reconcileNow`'s own catch handles, now pinned on
        // the `upsert` path too.
        const h = await harness();
        const version = (): number =>
            (h.node.state.basicInformation as { configurationVersion?: number }).configurationVersion ?? 0;
        try {
            await h.registry.upsert(spec(1, Role.onOffLight, { battery: false }));
            const before = version();

            await rejects(
                () =>
                    h.registry.upsert(spec(1, Role.onOffLight, { battery: true, states: { level: 50 } })),
                ErrorCode.malformedArgs,
            );

            assert.equal(version(), before + 1, "the set changed even though create failed, so the bump must fire");
            assert.equal(h.registry.size, 0, "the old endpoint is gone and the new one never landed");
        } finally {
            await h.close();
        }
    });

    it("upsert: a role change over a battery gain is refused as role_change (check order)", async () => {
        // Pins the check ORDERING: the role refusal must sit above the
        // battery-gain branch, or a role change riding a battery gain would
        // recreate under the new role instead of being refused outright.
        const h = await harness();
        try {
            await h.registry.upsert(spec(1, Role.onOffLight, { battery: false }));
            await rejects(
                () => h.registry.upsert(spec(1, Role.dimmableLight, { battery: true })),
                ErrorCode.roleChange,
            );
            assert.equal(h.registry.summaries()[0]?.role, Role.onOffLight, "nothing moved under a refused upsert");
        } finally {
            await h.close();
        }
    });

    it("upsert: a battery LOSS does not recreate, keeps the cluster, and does not bump", async () => {
        const h = await harness();
        const version = (): number =>
            (h.node.state.basicInformation as { configurationVersion?: number }).configurationVersion ?? 0;
        try {
            const created = await h.registry.upsert(spec(1, Role.onOffLight, { battery: true }));
            const before = version();

            const upserted = await h.registry.upsert(spec(1, Role.onOffLight, { battery: false }));

            assert.equal(upserted.endpointNumber, created.endpointNumber);
            assert.equal(version(), before, "a battery loss alone is not a configuration change");
            const endpoint = only(h);
            // §4.1: the cluster set is monotonic — a battery LOSS is "no
            // evidence right now", never a removal.
            assert.ok(descriptorOf(endpoint).serverList.map(Number).includes(47));
        } finally {
            await h.close();
        }
    });
});
