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

import { Endpoint, Environment, Logger, ServerNode, VendorId } from "@matter/main";
import { BasicInformationServer } from "@matter/main/behaviors/basic-information";
import { AggregatorEndpoint } from "@matter/main/endpoints/aggregator";

import { endpointIdFor, uniqueIdFor, watchCommands } from "../src/endpoints.js";
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
        productName: PRODUCT_NAME,
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
        ...overrides,
    };
}

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

    it("refuses a lawful §4.2 role this bridge version cannot build", async () => {
        const h = await harness();
        try {
            const error = await rejects(
                () => h.registry.reconcile([spec(1, Role.doorLock)], false),
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
        // The dangerous shape: one supported role and one E4 role, against a
        // POPULATED live set. If the role check ever moved after planning — or
        // worse, into the per-spec loop — the removals would run first and the
        // user would lose accessories to a request that was refused anyway.
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight), spec(2, Role.dimmableLight)], false);
            const before = h.registry.summaries();

            await rejects(
                // 2 is dropped from the desired set, so it is a planned removal;
                // 3 is a lawful v1 role this bridge cannot build.
                () => h.registry.reconcile([spec(1, Role.onOffLight), spec(3, Role.doorLock)], false),
                ErrorCode.internal,
            );

            assert.deepEqual(h.registry.summaries(), before, "the live set moved under a refused reconcile");
            assert.equal([...h.aggregator.parts].length, 2, "the Matter tree moved under a refused reconcile");
        } finally {
            await h.close();
        }
    });

    it("refuses an upsert of an E4 role without touching the live set", async () => {
        const h = await harness();
        try {
            await h.registry.reconcile([spec(1, Role.onOffLight)], false);
            const before = h.registry.summaries();
            await rejects(() => h.registry.upsert(spec(2, Role.thermostat)), ErrorCode.internal);
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

    it("emits the §4.2 payload for each attribute an ecosystem changes", async () => {
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

            assert.deepEqual(h.commands, [
                { indigoDeviceId: 7, command: "onOff", args: { value: true } },
                { indigoDeviceId: 7, command: "setLevel", args: { level: 60 } },
                { indigoDeviceId: 7, command: "setColorTemp", args: { colorTempMireds: 320 } },
                // setColor carries both halves; the saturation comes from state.
                { indigoDeviceId: 7, command: "setColor", args: { hue: 210, saturation: 80 } },
            ]);
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
                () => watchCommands(endpoint, { indigoDeviceId: 1, role: Role.dimmableLight }, () => {}),
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
