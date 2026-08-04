/**
 * BRIDGE_PROTOCOL.md conformance for the E0 command subset, run against the
 * real ws-server with the Matter node stubbed out.
 */

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";

import { PROTOCOL_VERSION } from "../src/protocol.js";
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
        assert.deepEqual(response, golden.attach.response);
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
});

describe("gating (§1.1)", () => {
    it("refuses non-attach commands before attach with not_attached", async () => {
        const client = await connect();
        const response = await client.request(golden.not_attached.request);
        assert.deepEqual(response, golden.not_attached.response);
        client.close();
    });

    it("returns unknown_command for E1 endpoint CRUD", async () => {
        const client = await connect();
        await attach(client);
        const response = await client.request(golden.unknown_command.request);
        assert.deepEqual(response, golden.unknown_command.response);
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

    it("rejects a non-positive duration as malformed_args", async () => {
        const client = await connect();
        await attach(client);
        const response = await client.request({
            message_id: "dur",
            command: "open_commissioning_window",
            args: { durationSeconds: 0 },
        });
        assert.equal(response.error_code, "malformed_args");
        client.close();
    });
});
