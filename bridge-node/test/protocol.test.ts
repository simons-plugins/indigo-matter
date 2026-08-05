/**
 * BRIDGE_PROTOCOL.md conformance for the implemented command set, run against
 * the real ws-server with the Matter node stubbed out.
 */

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

import { ErrorCode, PROTOCOL_VERSION, ProtocolError } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";
import { golden, StubBridge } from "./stub-bridge.js";

const BRIDGE_VERSION = "0.1.0-test";
const MATTER_JS_VERSION = "0.17.8";

/** The paired ecosystem the populated golden statuses assume. */
const APPLE_HOME = { fabricIndex: 1, label: "Apple Home", vendorId: 4937 };

const bridge = new StubBridge();
const server = new BridgeWsServer({
    port: 0,
    bridge,
    bridgeVersion: BRIDGE_VERSION,
    matterJsVersion: MATTER_JS_VERSION,
    log: () => {},
});

/**
 * A second server on its own double, for the tests that need a *cold* endpoint
 * set. The shared one above is deliberately kept warm at the
 * `attach_with_endpoints` set — that is what makes `get_status` answer the
 * golden populated StatusReport — so a test about an empty or emptied bridge
 * cannot use it without wrecking the ones around it.
 */
async function withColdBridge<T>(
    run: (bridge: StubBridge, connect: () => Promise<TestClient>) => Promise<T>,
): Promise<T> {
    const cold = new StubBridge();
    const coldServer = new BridgeWsServer({
        port: 0,
        bridge: cold,
        bridgeVersion: BRIDGE_VERSION,
        matterJsVersion: MATTER_JS_VERSION,
        log: () => {},
    });
    await coldServer.listen();
    try {
        return await run(cold, async () => {
            const client = await TestClient.connect(coldServer.port);
            await client.next(); // handshake
            return client;
        });
    } finally {
        await coldServer.close();
    }
}

async function connect(): Promise<TestClient> {
    const client = await TestClient.connect(server.port);
    await client.next(); // consume the handshake
    return client;
}

/**
 * The shared server's `attach` — the populated one, so the live endpoint set
 * matches `golden.get_status`. §3.1 makes attach a full reconcile, and
 * re-sending the same set is all-updates, so this is safe to call repeatedly.
 */
async function attach(client: TestClient): Promise<Record<string, unknown>> {
    return client.request(golden.attach_with_endpoints.request);
}

before(async () => {
    bridge.statusCommissioned = true;
    bridge.statusFabrics = [APPLE_HOME];
    await server.listen();
});

after(async () => {
    await server.close();
});

describe("handshake (§2)", () => {
    it("sends the bare handshake frame first, with no message_id", async () => {
        const client = await TestClient.connect(server.port);
        const frame = await client.next();
        assert.deepEqual(frame, {
            protocolVersion: PROTOCOL_VERSION,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
        });
        assert.equal(Object.keys(golden.handshake).sort().join(","), Object.keys(frame).sort().join(","));
        client.close();
    });
});

describe("attach (§3.1)", () => {
    it("accepts a matching protocol version and reconciles the endpoint set", async () => {
        const client = await connect();
        const response = await attach(client);
        assert.deepEqual(response, golden.attach_with_endpoints.response);
        client.close();
    });

    it("answers an empty desired set with an empty live set", async () => {
        // The golden `attach` pair: nothing live, nothing desired. The
        // mass-removal guard cannot fire — there is nothing to remove.
        await withColdBridge(async (_cold, connectCold) => {
            const client = await connectCold();
            assert.deepEqual(await client.request(golden.attach.request), golden.attach.response);
            client.close();
        });
    });

    it("refuses an attach that would remove every live endpoint", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            assert.deepEqual(
                await client.request(golden.attach_mass_removal_refused.request),
                golden.attach_mass_removal_refused.response,
            );
            // §3.1 is a gate, not a rollback: the live set is untouched, and the
            // client is still attached so it can retry with the intent.
            assert.deepEqual(
                await client.request(golden.get_status.request),
                golden.get_status.response,
            );
            client.close();
        });
    });

    it("empties the live set when the client says intent: replace_all", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            assert.deepEqual(
                await client.request(golden.attach_replace_all.request),
                golden.attach_replace_all.response,
            );
            client.close();
        });
    });

    it("keeps an endpoint's number across a removal and a role change", async () => {
        // The double models this because the real registry does: matter.js keys
        // its persisted number on `Endpoint.id`, so a device that goes away and
        // comes back — or changes role, which §3.1 implements as remove+re-add —
        // comes back as the same accessory number. A double that renumbered
        // would have made the golden StatusReports true only on a cold bridge.
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            // Empty it, then bring the same two devices back — with the first
            // one's role changed, which is the recreate path.
            await client.request(golden.attach_replace_all.request);
            const specs = golden.attach_with_endpoints.request.args as { endpoints: Record<string, unknown>[] };
            const rerun = await client.request({
                message_id: "renumber",
                command: "attach",
                args: {
                    protocolVersion: PROTOCOL_VERSION,
                    pluginVersion: "t",
                    endpoints: [{ ...specs.endpoints[0], role: "dimmableLight", states: {} }, specs.endpoints[1]],
                },
            });
            const endpoints = (rerun.result as { endpoints: { indigoDeviceId: number; endpointNumber: number }[] })
                .endpoints;
            assert.deepEqual(
                endpoints.map(endpoint => [endpoint.indigoDeviceId, endpoint.endpointNumber]),
                [
                    [123456789, 2],
                    [123456790, 3],
                ],
            );
            client.close();
        });
    });

    it("rejects a malformed endpoint set without disturbing the live one", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            for (const endpoints of [
                "not-an-array",
                [{ role: "onOffLight", label: "x" }], // no indigoDeviceId
                [{ indigoDeviceId: 1, role: "onOffLight" }], // no label
                [{ indigoDeviceId: 1, role: 7, label: "x" }],
                [
                    { indigoDeviceId: 1, role: "onOffLight", label: "a" },
                    { indigoDeviceId: 1, role: "onOffLight", label: "b" },
                ],
            ]) {
                const response = await client.request({
                    message_id: "bad-set",
                    command: "attach",
                    args: { protocolVersion: PROTOCOL_VERSION, pluginVersion: "t", endpoints },
                });
                assert.equal(response.error_code, ErrorCode.malformedArgs, JSON.stringify(endpoints));
            }

            // §1.1: a lawful shape carrying a role outside §4.2 is its own code.
            const unknownRole = await client.request({
                message_id: "bad-role",
                command: "attach",
                args: {
                    protocolVersion: PROTOCOL_VERSION,
                    pluginVersion: "t",
                    endpoints: [{ indigoDeviceId: 1, role: "airPurifier", label: "x" }],
                },
            });
            assert.equal(unknownRole.error_code, ErrorCode.unknownRole);

            assert.deepEqual(await client.request(golden.get_status.request), golden.get_status.response);
            client.close();
        });
    });

    it("rejects a mismatched protocolVersion and closes the socket", async () => {
        const client = await connect();
        const response = await client.request(golden.attach_version_mismatch.request);
        assert.deepEqual(response, golden.attach_version_mismatch.response);
        await client.waitForClose();
    });

    it("rejects a non-numeric protocolVersion as malformed_args", async () => {
        const client = await connect();
        const response = await client.request({
            message_id: "bad",
            command: "attach",
            args: { protocolVersion: "1" },
        });
        assert.equal(response.error_code, "malformed_args");
        assert.equal(response.message_id, "bad");
        client.close();
    });

    it("supersedes the incumbent attached client", async () => {
        const first = await connect();
        await attach(first);

        const second = await connect();
        await attach(second);

        await first.waitForClose();

        // The superseding client is still usable.
        const status = await second.request(golden.get_status.request);
        assert.deepEqual(status, golden.get_status.response);
        second.close();
    });

    it("keeps the incumbent when a second socket's attach is refused", async () => {
        // §3.1 parses the endpoint set *before* attaching state changes hands.
        // If that order ever inverts, a plugin sending one bad spec would hang
        // up on the healthy connection currently carrying every command event
        // and then fail to attach itself, leaving the bridge with no client at
        // all — worse than the malformed attach it was rejecting.
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            const incumbent = await connectCold();
            await incumbent.request(golden.attach_with_endpoints.request);

            const usurper = await connectCold();
            for (const endpoints of [
                [{ indigoDeviceId: 1, role: "airPurifier", label: "x" }], // unknown_role
                "not-an-array", // malformed_args
            ]) {
                const refusal = await usurper.request({
                    message_id: "usurp",
                    command: "attach",
                    args: { protocolVersion: PROTOCOL_VERSION, pluginVersion: "t", endpoints },
                });
                assert.ok(refusal.error_code !== undefined, JSON.stringify(endpoints));
                assert.equal(incumbent.closed, false, "the incumbent was hung up on by a refused attach");
            }

            // Still *the* attached client: events go to it, and commands work.
            cold.emitCommand(golden.command_on_off.data as never);
            assert.deepEqual(await incumbent.next(), golden.command_on_off);
            assert.deepEqual(await incumbent.request(golden.get_status.request), golden.get_status.response);

            // And the refused socket never became attached in passing.
            const gated = await usurper.request({ message_id: "g", command: "get_status", args: {} });
            assert.equal(gated.error_code, ErrorCode.notAttached);

            usurper.close();
            incumbent.close();
        });
    });

    it("re-attaches the same socket without closing it", async () => {
        // §2 supersession must not fire on the incumbent when it *is* the
        // socket attaching: a plugin that re-attaches to refresh its endpoint
        // set would otherwise hang up on itself.
        const client = await connect();
        await attach(client);
        const again = await attach(client);
        assert.deepEqual(again, golden.attach_with_endpoints.response);
        assert.equal(client.closed, false);

        const status = await client.request(golden.get_status.request);
        assert.deepEqual(status, golden.get_status.response);
        assert.equal(client.closed, false);
        client.close();
    });
});

describe("gating (§1.1)", () => {
    it("refuses non-attach commands before attach with not_attached", async () => {
        const client = await connect();
        const response = await client.request(golden.not_attached.request);
        assert.deepEqual(response, golden.not_attached.response);
        client.close();
    });

    it("returns unknown_command for a name outside §3", async () => {
        const client = await connect();
        await attach(client);
        const response = await client.request(golden.unknown_command.request);
        assert.deepEqual(response, golden.unknown_command.response);
        client.close();
    });

    it("prefers not_attached over unknown_command before attach", async () => {
        const client = await connect();
        const response = await client.request(golden.unknown_command.request);
        assert.equal(response.error_code, "not_attached");
        client.close();
    });

    it("refuses every §3 command before attach, not just the one with a golden frame", async () => {
        // Enumerated rather than spot-checked: `not_attached` is a per-command
        // gate in `onMessage`, and a command added to the handler map without a
        // thought for the gate would otherwise be reachable on an un-attached
        // socket. Every §3 command the node implements, minus `attach` itself.
        const gated: [string, Record<string, unknown>][] = [
            ["get_status", {}],
            ["get_pairing", {}],
            ["open_commissioning_window", { durationSeconds: 900 }],
            ["upsert_endpoint", golden.upsert_endpoint.request.args as Record<string, unknown>],
            ["remove_endpoint", { indigoDeviceId: 123456789 }],
            ["set_state", { indigoDeviceId: 123456789, states: { onOff: true } }],
            ["set_reachable", { indigoDeviceId: 123456789, reachable: false }],
            ["remove_fabric", { fabricIndex: 2 }],
            ["factory_reset", { preserveEndpointNumbers: true }],
            // Exempt from this gate only while the node is REFUSING (§1.1); with
            // a healthy node it is an ordinary command and needs an attach.
            ["rebuild_endpoint_map", {}],
        ];
        const client = await connect();
        for (const [command, args] of gated) {
            const response = await client.request({ message_id: `gate-${command}`, command, args });
            assert.equal(response.error_code, ErrorCode.notAttached, `${command} answered before attach`);
            assert.equal(response.message_id, `gate-${command}`);
        }
        client.close();
    });

    it("returns malformed_args when command is missing", async () => {
        const client = await connect();
        const response = await client.request({ message_id: "nocmd" });
        assert.equal(response.error_code, "malformed_args");
        client.close();
    });

    it("echoes message_id verbatim", async () => {
        const client = await connect();
        await attach(client);
        const response = await client.request({
            message_id: "an-opaque-🔑-id",
            command: "get_status",
            args: {},
        });
        assert.equal(response.message_id, "an-opaque-🔑-id");
        client.close();
    });
});

describe("endpoint CRUD (§3.2-§3.5)", () => {
    /**
     * One walk of the golden sequence on a cold bridge: attach the two-endpoint
     * set, then every CRUD frame in the order that makes its payload true. They
     * share a bridge because they *are* a sequence — `upsert_endpoint`'s
     * `{endpointNumber: 2}` is only correct for the endpoint `attach` created,
     * and `remove_endpoint` only returns `{removed: true}` once.
     */
    it("answers every golden endpoint exchange verbatim", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            // §3.2 idempotent update of a live endpoint, answering with its number.
            assert.deepEqual(await client.request(golden.upsert_endpoint.request), golden.upsert_endpoint.response);
            // §4.1: ecosystems cache device types, so a role change is refused.
            assert.deepEqual(
                await client.request(golden.upsert_endpoint_role_change.request),
                golden.upsert_endpoint_role_change.response,
            );
            // §1.1: a lawfully-shaped endpoint naming a role outside §4.2 gets
            // its own code, decided in `parseEndpointSpec` before the facade is
            // reached — which is why the live set is untouched below.
            assert.deepEqual(
                await client.request(golden.upsert_endpoint_unknown_role.request),
                golden.upsert_endpoint_unknown_role.response,
            );
            assert.equal(cold.model.has(900099), false, "an unknown role must not create an endpoint");
            // §3.4/§3.5 against a live device, and against one that is not.
            assert.deepEqual(await client.request(golden.set_state.request), golden.set_state.response);
            assert.deepEqual(
                await client.request(golden.set_state_unknown_device.request),
                golden.set_state_unknown_device.response,
            );
            assert.deepEqual(await client.request(golden.set_reachable.request), golden.set_reachable.response);
            assert.equal(cold.lastReachable, false);
            // §3.3, then the same removal again — idempotent both ways.
            assert.deepEqual(await client.request(golden.remove_endpoint.request), golden.remove_endpoint.response);
            assert.deepEqual(
                await client.request(golden.remove_endpoint_absent.request),
                golden.remove_endpoint_absent.response,
            );
            assert.deepEqual(
                await client.request({ ...golden.remove_endpoint.request, message_id: "again" }),
                { message_id: "again", result: { removed: false } },
            );
            client.close();
        });
    });

    it("creates an absent endpoint on upsert and reports its new number", async () => {
        await withColdBridge(async (_cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach.request); // empty live set
            const response = await client.request(golden.upsert_endpoint.request);
            assert.deepEqual(response.result, { endpointNumber: 2 });
            client.close();
        });
    });

    it("refuses malformed CRUD args (§1.1)", async () => {
        await withColdBridge(async (_cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach.request);

            const malformed: [string, Record<string, unknown>][] = [
                ["upsert_endpoint", {}],
                ["upsert_endpoint", { endpoint: 7 }],
                ["remove_endpoint", {}],
                ["remove_endpoint", { indigoDeviceId: "123" }],
                ["set_state", { indigoDeviceId: 1 }],
                ["set_state", { indigoDeviceId: 1, states: [] }],
                ["set_reachable", { indigoDeviceId: 1 }],
                ["set_reachable", { indigoDeviceId: 1, reachable: "no" }],
            ];
            for (const [command, args] of malformed) {
                const response = await client.request({ message_id: `bad-${command}`, command, args });
                assert.equal(
                    response.error_code,
                    ErrorCode.malformedArgs,
                    `${command} ${JSON.stringify(args)} was accepted`,
                );
            }
            client.close();
        });
    });
});

describe("command event (§5)", () => {
    it("forwards every §4.2 command payload to the attached client", async () => {
        const client = await connect();
        await attach(client);

        for (const frame of [
            golden.command_on_off,
            golden.command_set_level,
            golden.command_set_color_temp,
            golden.command_set_color,
        ]) {
            bridge.emitCommand(frame.data as never);
            assert.deepEqual(await client.next(), frame);
        }
        client.close();
    });
});

describe("get_pairing (§3.7)", () => {
    it("reports the initial window as open with the persisted codes", async () => {
        bridge.commissioned = false;
        const client = await connect();
        await attach(client);
        const response = await client.request(golden.get_pairing_uncommissioned.request);
        assert.deepEqual(response, golden.get_pairing_uncommissioned.response);

        const result = response.result as Record<string, unknown>;
        assert.equal(result.windowOpen, true);
        assert.notEqual(result.manualPairingCode, null);
        assert.notEqual(result.qrPairingCode, null);
        client.close();
    });

    it("nulls the codes once commissioned with no window open", async () => {
        bridge.commissioned = true;
        const client = await connect();
        await attach(client);
        const response = await client.request(golden.get_pairing_commissioned.request);
        assert.deepEqual(response, golden.get_pairing_commissioned.response);

        const result = response.result as Record<string, unknown>;
        assert.equal(result.windowOpen, false);
        assert.equal(result.manualPairingCode, null);
        assert.equal(result.windowExpiresAt, null);
        bridge.commissioned = false;
        client.close();
    });

    it("reports codes and a non-null expiry while commissioned with a window open", async () => {
        bridge.commissioned = true;
        bridge.windowOpen = true;
        const client = await connect();
        await attach(client);
        const response = await client.request(golden.get_pairing_commissioned_window_open.request);
        assert.deepEqual(response, golden.get_pairing_commissioned_window_open.response);

        const result = response.result as Record<string, unknown>;
        assert.equal(result.commissioned, true);
        assert.equal(result.windowOpen, true);
        assert.notEqual(result.windowExpiresAt, null);
        assert.notEqual(result.manualPairingCode, null);
        bridge.commissioned = false;
        bridge.windowOpen = false;
        client.close();
    });
});

describe("open_commissioning_window (§3.8)", () => {
    it("passes durationSeconds through and returns the window result", async () => {
        const client = await connect();
        await attach(client);
        const response = await client.request(golden.open_commissioning_window.request);
        assert.deepEqual(response, golden.open_commissioning_window.response);
        assert.equal(bridge.openWindowCalls.at(-1), 900);
        client.close();
    });

    it("bounds durationSeconds to Matter's 180-900s window", async () => {
        const client = await connect();
        await attach(client);
        // 0 and 60 are below Matter's MinCommissioningTimeout; 901 is above its
        // maximum, and matter.js 0.17.8 would never time a longer window out.
        for (const durationSeconds of [0, -1, 60, 179, 901, 3600, 900.5]) {
            const response = await client.request({
                message_id: `dur-${durationSeconds}`,
                command: "open_commissioning_window",
                args: { durationSeconds },
            });
            assert.equal(response.error_code, "malformed_args", `durationSeconds ${durationSeconds} was accepted`);
        }
        // The golden frame for that refusal, asserted whole (§7).
        assert.deepEqual(
            await client.request(golden.open_window_malformed_args.request),
            golden.open_window_malformed_args.response,
        );
        for (const durationSeconds of [180, 900]) {
            const response = await client.request({
                message_id: `ok-${durationSeconds}`,
                command: "open_commissioning_window",
                args: { durationSeconds },
            });
            assert.ok("result" in response, `durationSeconds ${durationSeconds} was rejected`);
            assert.equal(bridge.openWindowCalls.at(-1), durationSeconds);
        }
        client.close();
    });

    it("reports an unexpected facade failure as internal, with the message as details", async () => {
        // The StubBridge hook exists precisely so this path is covered without
        // a Matter stack that can be made to fail on demand.
        bridge.openWindowError = new Error(golden.open_window_internal.response.details as string);
        try {
            const client = await connect();
            await attach(client);
            const response = await client.request(golden.open_window_internal.request);
            assert.deepEqual(response, golden.open_window_internal.response);
            client.close();
        } finally {
            bridge.openWindowError = undefined;
        }
    });

    it("passes a ProtocolError through with its own error_code", async () => {
        bridge.openWindowError = new ProtocolError(
            ErrorCode.commissioningWindowFailed,
            golden.open_window_failed.response.details as string,
        );
        try {
            const client = await connect();
            await attach(client);
            const response = await client.request(golden.open_window_failed.request);
            assert.deepEqual(response, golden.open_window_failed.response);
            client.close();
        } finally {
            bridge.openWindowError = undefined;
        }
    });
});

describe("frame hygiene (§1)", () => {
    it("drops every shape of garbage without answering or dying, and stays usable", async () => {
        const client = await connect();
        await attach(client);

        const garbage: (string | Buffer)[] = [
            "{ not json",
            "",
            '"hi"',
            "42",
            "null",
            "[1, 2, 3]",
            JSON.stringify({ command: "get_status", args: {} }), // no message_id
            JSON.stringify({ message_id: 7, command: "get_status" }), // non-string message_id
            JSON.stringify({ event: "window_closed", data: { reason: "expired" } }), // events are node→plugin
            Buffer.from([0x00, 0xff, 0x10, 0x80]), // binary frame
        ];
        for (const payload of garbage) {
            client.sendRaw(payload);
        }

        // A well-formed follow-up is still answered, and it is the *only* frame
        // that comes back — nothing above produced a response.
        const response = await client.request(golden.get_status.request);
        assert.deepEqual(response, golden.get_status.response);
        assert.equal(client.buffered, 0);
        assert.equal(client.closed, false);
        client.close();
    });
});

describe("exactly one response (§1)", () => {
    it("answers internal when the result itself cannot be put on the wire", async () => {
        // A handler can succeed and the response still fail — a value that will
        // not serialise, a socket that dies mid-write. §1 promises one response
        // per request either way, and the plugin correlates on message_id, so
        // silence here would strand a future until its timeout.
        const client = await connect();
        await attach(client);
        bridge.poisonStatus = true;
        try {
            const response = await client.request({ message_id: "poison", command: "get_status", args: {} });
            assert.deepEqual(response, {
                message_id: "poison",
                error_code: ErrorCode.internal,
                details: "Could not send the result of get_status",
            });
        } finally {
            bridge.poisonStatus = false;
        }
        // And the socket is still usable afterwards.
        assert.deepEqual(await client.request(golden.get_status.request), golden.get_status.response);
        client.close();
    });
});

describe("ordering (§1)", () => {
    it("answers pipelined frames in receipt order even when the first awaits", async () => {
        // open_commissioning_window genuinely awaits (crypto); a get_pairing
        // pipelined behind it resolves synchronously. Without a per-socket
        // handler chain the second response overtakes the first.
        bridge.delayOpenWindowMs = 50;
        try {
            const client = await connect();
            await attach(client);

            client.send({
                message_id: "first-slow",
                command: "open_commissioning_window",
                args: { durationSeconds: 900 },
            });
            client.send({ message_id: "second-fast", command: "get_pairing", args: {} });

            const first = await client.next();
            const second = await client.next();
            assert.equal(first.message_id, "first-slow");
            assert.equal(second.message_id, "second-fast");
            client.close();
        } finally {
            bridge.delayOpenWindowMs = 0;
        }
    });
});

describe("window_closed event (§3.8/§5)", () => {
    it("emits the golden frame to the attached client for each reason", async () => {
        const client = await connect();
        await attach(client);

        bridge.emitWindowClosed("expired");
        assert.deepEqual(await client.next(), golden.window_closed_expired);

        bridge.emitWindowClosed("commissioned");
        assert.deepEqual(await client.next(), golden.window_closed_commissioned);

        // Events carry no message_id (§1) — the plugin must not try to match one.
        assert.equal("message_id" in golden.window_closed_expired, false);
        client.close();
    });

    it("drops the event when nobody is attached", async () => {
        // Its own server and stub: `#attached` is server-wide state, and the
        // shared server has attached clients throughout this file.
        const lonelyBridge = new StubBridge();
        const logs: string[] = [];
        const lonely = new BridgeWsServer({
            port: 0,
            bridge: lonelyBridge,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
            log: message => logs.push(message),
        });
        await lonely.listen();
        try {
            // Nothing connected at all.
            lonelyBridge.emitWindowClosed("expired");

            // Connected but never attached: still no addressee.
            const client = await TestClient.connect(lonely.port);
            await client.next(); // handshake
            lonelyBridge.emitWindowClosed("expired");
            await assert.rejects(client.next(200));
            assert.equal(client.closed, false);
            client.close();
        } finally {
            await lonely.close();
        }

        // Dropped, but not in silence: a run of these is what "the ecosystem
        // does nothing" looks like from here, and it must be distinguishable
        // from a listener that was never wired up.
        assert.equal(logs.filter(line => line.includes("Dropping window_closed event")).length, 2, logs.join("\n"));
    });

    it("names the device when it drops a command event with nobody attached", async () => {
        const orphanBridge = new StubBridge();
        const logs: string[] = [];
        const orphan = new BridgeWsServer({
            port: 0,
            bridge: orphanBridge,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
            log: message => logs.push(message),
        });
        await orphan.listen();
        try {
            orphanBridge.emitCommand(golden.command_on_off.data as never);
        } finally {
            await orphan.close();
        }
        const dropped = logs.filter(line => line.includes("Dropping command event"));
        assert.equal(dropped.length, 1, logs.join("\n"));
        assert.match(dropped[0] ?? "", /for device 123456789/);
    });
});

describe("fabric and reset commands (§3.9-§3.11)", () => {
    it("answers every golden exchange verbatim", async () => {
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);
            for (const name of ["remove_fabric", "factory_reset", "factory_reset_discard_map"] as const) {
                const exchange = golden[name];
                assert.deepEqual(await client.request(exchange.request), exchange.response, name);
            }
            assert.deepEqual(cold.removedFabrics, [2]);
            // §3.10: the two flavours reach the facade as the booleans they are.
            assert.deepEqual(cold.factoryResets, [true, false]);
            client.close();
        });
    });

    it("defaults preserveEndpointNumbers to true when the flag is omitted", async () => {
        // §6.6: destructive operations cannot happen as a default, and
        // discarding endpoint identity is the destructive half of §3.10.
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);
            await client.request({ message_id: "fr-default", command: "factory_reset", args: {} });
            assert.deepEqual(cold.factoryResets, [true]);
            client.close();
        });
    });

    it("rejects malformed §3.9/§3.10 args", async () => {
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);
            const bad: [string, Record<string, unknown>][] = [
                ["remove_fabric", { fabricIndex: 0 }],
                ["remove_fabric", { fabricIndex: "2" }],
                ["remove_fabric", {}],
                ["factory_reset", { preserveEndpointNumbers: "yes" }],
            ];
            for (const [command, args] of bad) {
                const response = await client.request({ message_id: `bad-${command}`, command, args });
                assert.equal(response.error_code, ErrorCode.malformedArgs, `${command} ${JSON.stringify(args)}`);
            }
            assert.deepEqual(cold.removedFabrics, [], "a refused command must not reach the facade");
            assert.deepEqual(cold.factoryResets, []);
            client.close();
        });
    });

    it("answers rebuild_endpoint_map with the golden StatusReport", async () => {
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);
            const exchange = golden.rebuild_endpoint_map;
            assert.deepEqual(await client.request(exchange.request), exchange.response);
            assert.equal(cold.rebuilds, 1);
            client.close();
        });
    });
});

describe("the endpoint_map_invalid refuse-to-start state (§1.1, PRD §7)", () => {
    it("refuses everything outside the recovery trio, attach included", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.refusal = "endpoint map is unreadable";
            const client = await connectCold();

            // `attach` is the one that matters most: a node that cannot vouch
            // for its endpoint numbers must not CREATE endpoints, because
            // creating them is exactly how a lost map duplicates every
            // accessory in every paired ecosystem.
            const attached = await client.request(golden.attach_with_endpoints.request);
            assert.equal(attached.error_code, ErrorCode.endpointMapInvalid);
            assert.deepEqual(cold.model.summaries(), [], "nothing may be created while refusing");

            const refused = await client.request(golden.endpoint_map_invalid.request);
            assert.deepEqual(refused, golden.endpoint_map_invalid.response);
            client.close();
        });
    });

    it("accepts get_status, get_pairing and rebuild_endpoint_map WITHOUT an attach", async () => {
        // The client holding this socket open never got to attach — its attach
        // is what was refused — so requiring one would make the documented way
        // out unreachable.
        await withColdBridge(async (cold, connectCold) => {
            cold.refusal = "endpoint map is unreadable";
            const client = await connectCold();

            for (const command of ["get_status", "get_pairing"] as const) {
                const response = await client.request({ message_id: `rec-${command}`, command, args: {} });
                assert.ok(response.result !== undefined, `${command} was refused: ${JSON.stringify(response)}`);
            }
            const rebuilt = await client.request(golden.rebuild_endpoint_map.request);
            assert.deepEqual(rebuilt, golden.rebuild_endpoint_map.response);
            client.close();
        });
    });

    it("serves normally on the very next frame after a rebuild", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.statusCommissioned = true;
            cold.statusFabrics = [APPLE_HOME];
            cold.refusal = "endpoint map is unreadable";
            const client = await connectCold();
            await client.request(golden.rebuild_endpoint_map.request);

            // The gate reads the facade per frame rather than latching at
            // connect, so the command that fixes the state takes effect
            // immediately — no reconnect, no second refusal.
            const attached = await client.request(golden.attach_with_endpoints.request);
            assert.deepEqual(attached, golden.attach_with_endpoints.response);
            client.close();
        });
    });

    it("carries the refusal reason in details, not just a generic message", async () => {
        await withColdBridge(async (cold, connectCold) => {
            cold.refusal = "this bridge was commissioned but its Matter fabric storage is gone";
            const client = await connectCold();
            const response = await client.request(golden.attach_with_endpoints.request);
            // The two refusals need different remedies (§3.11 versus restoring a
            // backup), so a single canned string would send half the users the
            // wrong way.
            assert.match(String(response.details), /Matter fabric storage is gone/);
            assert.match(String(response.details), /only get_status, get_pairing and rebuild_endpoint_map/);
            client.close();
        });
    });

    it("refuses EVERY command outside the trio, by name", async () => {
        // Table-driven because the gate is a set-membership test: widening it
        // by one entry is a one-character change with no local symptom, and
        // three of these four would un-export or re-pair a whole house.
        //
        // `factory_reset` is on this list on purpose (see RECOVERY_COMMANDS):
        // it is arguably an exit, but it is the destructive one, and §3.11
        // already exits every refusal state without touching a pairing.
        const refusable = [
            { command: "attach", args: { protocolVersion: PROTOCOL_VERSION, endpoints: [] } },
            { command: "factory_reset", args: { preserveEndpointNumbers: false } },
            { command: "remove_fabric", args: { fabricIndex: 1 } },
            { command: "set_state", args: { indigoDeviceId: 123456789, states: { onOff: true } } },
            { command: "remove_endpoint", args: { indigoDeviceId: 123456789 } },
            { command: "upsert_endpoint", args: golden.upsert_endpoint.request.args },
            { command: "set_reachable", args: { indigoDeviceId: 123456789, reachable: false } },
            { command: "open_commissioning_window", args: { durationSeconds: 900 } },
        ];
        await withColdBridge(async (cold, connectCold) => {
            cold.refusal = "endpoint map is unreadable";
            const client = await connectCold();
            for (const { command, args } of refusable) {
                const response = await client.request({ message_id: `gate-${command}`, command, args });
                assert.equal(
                    response.error_code,
                    ErrorCode.endpointMapInvalid,
                    `${command} was NOT refused while the node is refusing to serve`,
                );
            }
            assert.deepEqual(cold.factoryResets, [], "no reset may run while refusing");
            assert.deepEqual(cold.removedFabrics, [], "no fabric may be dropped while refusing");
            client.close();
        });
    });

    it("does not close an un-attached socket while it is refusing", async () => {
        // The plugin deliberately HOLDS this socket open un-attached: §1.1 makes
        // it the only route to the rebuild. Closing it on the §2 timer put the
        // two in a fight — refuse, close, reconnect, refuse, every 10s — and a
        // rebuild the user had just confirmed could be cut off mid-flight by
        // our own timer.
        const cold = new StubBridge();
        cold.refusal = "endpoint map is unreadable";
        const coldServer = new BridgeWsServer({
            port: 0,
            bridge: cold,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
            log: () => {},
            unattachedTimeoutMs: 20,
        });
        await coldServer.listen();
        try {
            const client = await TestClient.connect(coldServer.port);
            await client.next(); // handshake
            await new Promise(resolve => setTimeout(resolve, 80));

            const rebuilt = await client.request(golden.rebuild_endpoint_map.request);
            assert.deepEqual(rebuilt, golden.rebuild_endpoint_map.response);
            client.close();
        } finally {
            await coldServer.close();
        }
    });

    it("still closes an un-attached socket when it is NOT refusing", async () => {
        const cold = new StubBridge();
        const coldServer = new BridgeWsServer({
            port: 0,
            bridge: cold,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
            log: () => {},
            unattachedTimeoutMs: 20,
        });
        await coldServer.listen();
        try {
            const client = await TestClient.connect(coldServer.port);
            await client.next(); // handshake
            await client.waitForClose(1000);
        } finally {
            await coldServer.close();
        }
    });

    it("reaps a socket that still never attaches once the refusal has cleared", async () => {
        // ⊗ The §2 timer fired once. Holding the socket open while refusing is
        // right, but returning bare left nothing armed — so after a
        // `rebuild_endpoint_map` cleared the refusal, a client that STILL never
        // attached was never reaped again for the life of the process. Every
        // half-open socket a crashed plugin left behind accumulated there, and
        // the one check that reclaims them had already been spent.
        const cold = new StubBridge();
        cold.refusal = "endpoint map is unreadable";
        const coldServer = new BridgeWsServer({
            port: 0,
            bridge: cold,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
            log: () => {},
            unattachedTimeoutMs: 20,
        });
        await coldServer.listen();
        try {
            const client = await TestClient.connect(coldServer.port);
            await client.next(); // handshake
            // Comfortably several periods, so the one-shot timer is long spent.
            await new Promise(resolve => setTimeout(resolve, 120));
            assert.equal(client.closed, false, "the socket must be held open while refusing");

            // The documented exit, which is exactly why the socket was held.
            await client.request(golden.rebuild_endpoint_map.request);

            await client.waitForClose(1000);
        } finally {
            await coldServer.close();
        }
    });
});

describe("drift_detected event (§5)", () => {
    it("forwards the detector's findings to the attached client", async () => {
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            cold.emitDrift(golden.drift_detected.data.drift as never);

            assert.deepEqual(await client.next(), golden.drift_detected);
            client.close();
        });
    });
});

describe("fabric events (§5 / §3.9 / §3.10)", () => {
    // All three were declared in the event enum, documented in §5, carried by
    // golden frames and consumed by the plugin's client — and emitted by
    // nothing. A user unpairing an ecosystem produced no event at all.
    it("emits fabrics_changed with the set AFTER the change", async () => {
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            cold.emitFabricsChanged([APPLE_HOME], "added");

            assert.deepEqual(await client.next(), {
                event: "fabrics_changed",
                data: { fabrics: [APPLE_HOME], change: "added" },
            });
            client.close();
        });
    });

    it("emits commissioned and decommissioned", async () => {
        await withColdBridge(async (cold, connectCold) => {
            const client = await connectCold();
            await client.request(golden.attach_with_endpoints.request);

            cold.emitCommissioned();
            assert.deepEqual(await client.next(), { event: "commissioned", data: {} });

            cold.emitDecommissioned();
            assert.deepEqual(await client.next(), { event: "decommissioned", data: {} });
            client.close();
        });
    });
});
