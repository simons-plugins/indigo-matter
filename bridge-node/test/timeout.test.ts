/**
 * §2: a connection that handshakes but never attaches is closed by the node.
 * Runs on its own server so the short timeout does not affect other tests.
 */

import assert from "node:assert/strict";
import { after, before, it } from "node:test";

import { PROTOCOL_VERSION } from "../src/protocol.js";
import { BridgeWsServer } from "../src/ws-server.js";
import { TestClient } from "./client.js";
import { StubBridge } from "./stub-bridge.js";

const server = new BridgeWsServer({
    port: 0,
    bridge: new StubBridge(),
    bridgeVersion: "0.1.0-test",
    matterJsVersion: "0.17.8",
    log: () => {},
    unattachedTimeoutMs: 150,
});

before(async () => {
    await server.listen();
});

after(async () => {
    await server.close();
});

it("closes an unattached socket after the timeout", async () => {
    const client = await TestClient.connect(server.port);
    await client.next(); // handshake
    await client.waitForClose(2000);
    assert.equal(client.closed, true);
});

it("does not close a socket that attached in time", async () => {
    const client = await TestClient.connect(server.port);
    await client.next();
    const response = await client.request({
        message_id: "a",
        command: "attach",
        args: { protocolVersion: PROTOCOL_VERSION, pluginVersion: "test", endpoints: [] },
    });
    assert.ok("result" in response);
    await new Promise(resolve => setTimeout(resolve, 400));
    assert.equal(client.closed, false);
    client.close();
});
