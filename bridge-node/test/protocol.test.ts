/**
 * BRIDGE_PROTOCOL.md conformance for the E0 command subset, run against the
 * real ws-server with the Matter node stubbed out.
 */

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

import { ErrorCode, PROTOCOL_VERSION, ProtocolError } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";
import { golden, StubBridge } from "./stub-bridge.js";

const BRIDGE_VERSION = "0.1.0-test";
const MATTER_JS_VERSION = "0.17.8";

const bridge = new StubBridge();
const server = new BridgeWsServer({
    port: 0,
    bridge,
    bridgeVersion: BRIDGE_VERSION,
    matterJsVersion: MATTER_JS_VERSION,
    log: () => {},
});

async function connect(): Promise<TestClient> {
    const client = await TestClient.connect(server.port);
    await client.next(); // consume the handshake
    return client;
}

async function attach(client: TestClient): Promise<Record<string, unknown>> {
    return client.request(golden.attach.request);
}

/**
 * What the E0 node answers an `attach` with.
 *
 * NOT `golden.attach.response`: that frame is the lawful §3.1 pair for a request
 * carrying `endpoints: []` (empty desired set → empty live set), which is what
 * the plugin asserts against. E0 does not reconcile the requested set at all —
 * it serves one hard-coded endpoint and returns that status — so this is the
 * live truth until E2 makes attach reconcile. Deliberate, and the fixture's
 * _comment says so.
 */
function e0AttachResponse(messageId: string): Record<string, unknown> {
    return { message_id: messageId, result: golden.get_status.response.result };
}

before(async () => {
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
    it("accepts a matching protocol version and returns a StatusReport", async () => {
        const client = await connect();
        const response = await attach(client);
        assert.deepEqual(response, e0AttachResponse(golden.attach.request.message_id as string));
        client.close();
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

    it("re-attaches the same socket without closing it", async () => {
        // §2 supersession must not fire on the incumbent when it *is* the
        // socket attaching: a plugin that re-attaches to refresh its endpoint
        // set would otherwise hang up on itself.
        const client = await connect();
        await attach(client);
        const again = await attach(client);
        assert.deepEqual(again, e0AttachResponse(golden.attach.request.message_id as string));
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
        const lonely = new BridgeWsServer({
            port: 0,
            bridge: lonelyBridge,
            bridgeVersion: BRIDGE_VERSION,
            matterJsVersion: MATTER_JS_VERSION,
            log: () => {},
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
    });
});
