"""M2: the WS client connects, correlates requests, streams events, reconnects.

Driven against the in-process FakeWebSocket — no Node process. Async tests run
via ``asyncio.run`` (the workspace's framework Python has no pytest-asyncio).
"""
from __future__ import annotations

import asyncio

import pytest

import protocol
from protocol import MatterCommand, Protocol
from matter_client import MatterClient

from fakes import FakeWebSocket, returns, scripted_responder


def run(coro):
    return asyncio.run(coro)


def _client(mock_logger, fake, **kw):
    return MatterClient(Protocol(), mock_logger, {}, connect=lambda uri: returns(fake), **kw)


def test_connects_and_captures_server_info(mock_logger):
    async def scenario():
        fake = FakeWebSocket(server_info={"sdk_version": "matter-server/0.6.2", "fabric_id": "0xABC"})
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        assert client.connected
        assert client.server_info["sdk_version"] == "matter-server/0.6.2"
        assert client.server_info["fabric_id"] == "0xABC"
        # start_listening sent on connect
        assert protocol.CMD_START_LISTENING in fake.sent_commands()
        await client.close()
        task.cancel()
    run(scenario())


def test_request_response_correlation(mock_logger):
    async def scenario():
        fake = FakeWebSocket(responder=scripted_responder({
            protocol.CMD_GET_NODES: [{"node_id": 1}, {"node_id": 2}],
            protocol.CMD_GET_NODE: lambda f: {"node_id": f["args"]["node_id"], "endpoints": {}},
        }))
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        nodes = await client.get_nodes()
        assert nodes == [{"node_id": 1}, {"node_id": 2}]

        node = await client.get_node(7)
        assert node["node_id"] == 7

        await client.close()
        task.cancel()
    run(scenario())


def test_send_command_invokes_device_command(mock_logger):
    async def scenario():
        seen = {}

        def responder(frame):
            if frame.get(protocol.KEY_COMMAND) == protocol.CMD_DEVICE:
                seen["args"] = frame["args"]
            return [{protocol.KEY_MESSAGE_ID: frame["message_id"], protocol.KEY_RESULT: None}]

        fake = FakeWebSocket(responder=responder)
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        result = await client.send_command(
            MatterCommand(node_id=42, endpoint=1, cluster=0x0006, command="On", args={})
        )
        assert result is None
        assert seen["args"][protocol.ARG_NODE_ID] == 42
        assert seen["args"][protocol.ARG_COMMAND] == "On"

        await client.close()
        task.cancel()
    run(scenario())


def test_events_are_dispatched_to_callback(mock_logger):
    async def scenario():
        received = []
        fake = FakeWebSocket()
        client = MatterClient(Protocol(), mock_logger, {},
                              connect=lambda uri: returns(fake),
                              on_event=received.append)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        # real shape: data is [node_id, "ep/cl/at", value]
        await fake.push_event("attribute_updated", [42, "1/6/0", True])
        # let the listen loop process
        for _ in range(20):
            if received:
                break
            await asyncio.sleep(0.01)

        assert received, "no event dispatched"
        evt = received[-1]
        assert evt.kind == protocol.EVT_ATTRIBUTE_UPDATED
        assert (evt.node_id, evt.endpoint, evt.cluster, evt.attribute) == (42, 1, 6, 0)
        assert evt.value is True
        assert client.last_event_ts is not None

        await client.close()
        task.cancel()
    run(scenario())


def test_reconnects_with_backoff_after_drop(mock_logger):
    async def scenario():
        delays = []

        async def fake_sleep(d):
            delays.append(d)
            await asyncio.sleep(0)  # yield without real delay

        attempts = {"n": 0}
        fakes = [FakeWebSocket(), FakeWebSocket()]

        def connect(uri):
            i = attempts["n"]
            attempts["n"] += 1
            if i == 0:
                # first attempt: fail
                async def boom():
                    raise ConnectionError("refused")
                return boom()
            return returns(fakes[1])

        client = MatterClient(Protocol(), mock_logger, {},
                              connect=connect, sleep=fake_sleep)
        task = asyncio.create_task(client.run())
        # should recover on the second attempt
        await client.wait_connected(timeout=2)
        assert client.connected
        assert attempts["n"] >= 2
        assert delays and delays[0] <= 30  # backoff applied, capped

        await client.close()
        task.cancel()
    run(scenario())


def test_in_flight_request_fails_on_disconnect_instead_of_hanging(mock_logger):
    async def scenario():
        # responder never answers get_nodes → the request stays pending
        fake = FakeWebSocket(responder=lambda f: [])
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        pending = asyncio.create_task(client.get_nodes())
        await asyncio.sleep(0.02)  # let it register as in-flight
        await fake.close()         # drop the socket mid-request

        with pytest.raises(ConnectionError):
            await asyncio.wait_for(pending, timeout=2)  # must fail, not hang

        await client.close()
        task.cancel()
    run(scenario())


def test_request_when_disconnected_raises(mock_logger):
    async def scenario():
        client = MatterClient(Protocol(), mock_logger, {},
                              connect=lambda uri: returns(FakeWebSocket()))
        # never started run(); not connected
        try:
            await client.get_nodes()
            assert False, "expected ConnectionError"
        except ConnectionError:
            pass
    run(scenario())
