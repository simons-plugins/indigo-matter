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


def test_on_connect_runs_after_each_connect(mock_logger):
    async def scenario():
        calls = []

        async def on_connect():
            calls.append(1)

        fake = FakeWebSocket()
        client = _client(mock_logger, fake, on_connect=on_connect)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        await asyncio.sleep(0.02)  # let the scheduled on_connect task run
        assert calls == [1]
        await client.close()
        task.cancel()
    run(scenario())


def test_on_connect_fires_again_after_reconnect(mock_logger):
    # the whole point of on_connect (vs the old one-shot _initial_sync): it must
    # re-run after a drop+reconnect, not just on the first connect.
    async def scenario():
        calls = []

        async def on_connect():
            calls.append(1)

        fakes = [FakeWebSocket(), FakeWebSocket()]
        n = {"i": 0}

        def connect(uri):
            i = n["i"]
            n["i"] += 1
            return returns(fakes[i] if i < len(fakes) else fakes[-1])

        async def fake_sleep(_d):
            await asyncio.sleep(0)  # no real backoff delay

        client = MatterClient(Protocol(), mock_logger, {}, connect=connect,
                              on_connect=on_connect, sleep=fake_sleep)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        await fakes[0].close()  # drop → backoff → reconnect to fakes[1]
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(calls) >= 2 and client.connected:
                break
        assert len(calls) >= 2  # fired on the first connect AND the reconnect
        await client.close()
        task.cancel()
    run(scenario())


def test_on_connect_failure_is_logged_not_swallowed(mock_logger):
    async def scenario():
        async def boom():
            raise RuntimeError("resync failed")

        fake = FakeWebSocket()
        client = _client(mock_logger, fake, on_connect=boom)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        await asyncio.sleep(0.05)  # let the on_connect task run + done-callback fire
        # the listen loop survives a failed reconcile, and the failure is surfaced
        assert client.connected
        mock_logger.exception.assert_called()
        await client.close()
        task.cancel()
    run(scenario())


def test_on_disconnect_fires_on_real_drop(mock_logger):
    async def scenario():
        events = []
        fake = FakeWebSocket()
        client = _client(mock_logger, fake, on_disconnect=lambda: events.append("down"))
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        await fake.close()              # genuine socket drop
        await asyncio.sleep(0.02)
        assert events == ["down"]
        await client.close()
        task.cancel()
    run(scenario())


def test_on_disconnect_not_called_on_intentional_close(mock_logger):
    async def scenario():
        events = []
        fake = FakeWebSocket()
        client = _client(mock_logger, fake, on_disconnect=lambda: events.append("down"))
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        await client.close()            # intentional shutdown — not a drop
        await asyncio.sleep(0.02)
        assert events == []
        task.cancel()
    run(scenario())


def test_commission_timeout_covers_observed_worst_case():
    # Live rehearsal 2026-06-09: commissioning took ~124s; the old 60s default
    # timed the job out while matter-server completed the join (#16). Only the
    # commission RPC gets the long deadline — the global default stays short.
    import inspect

    from matter_client import COMMISSION_TIMEOUT, MatterClient

    default = inspect.signature(MatterClient.commission_with_code).parameters["timeout"].default
    assert default == COMMISSION_TIMEOUT
    assert COMMISSION_TIMEOUT >= 300.0
    generic = inspect.signature(MatterClient.request).parameters["timeout"].default
    assert generic <= 30.0
