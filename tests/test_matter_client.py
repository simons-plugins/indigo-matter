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


def test_commission_timeout_value_reaches_the_wait(mock_logger, monkeypatch):
    # Behavioural counterpart of the signature check above: prove the long
    # deadline actually reaches the asyncio.wait_for guarding the commission
    # RPC's pending future, and that a plain request keeps its short default.
    from matter_client import COMMISSION_TIMEOUT

    captured: list[float] = []
    real_wait_for = asyncio.wait_for

    async def spying_wait_for(awaitable, timeout):
        captured.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr("matter_client.asyncio.wait_for", spying_wait_for)

    async def scenario():
        fake = FakeWebSocket(responder=scripted_responder({
            protocol.CMD_COMMISSION: {"node_id": 1},
            protocol.CMD_GET_NODES: [],
        }))
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        captured.clear()  # drop wait_connected's own wait_for
        result = await client.commission_with_code("MT:Y.TEST")
        assert result == {"node_id": 1}
        assert captured == [COMMISSION_TIMEOUT]  # the 300s deadline was used

        captured.clear()
        await client.get_nodes()
        assert captured == [10.0]  # plain requests keep the short default

        await client.close()
        task.cancel()
    run(scenario())


def _withheld_commission(mock_logger, **kw):
    """A connected matter-server client whose commission request never gets
    an answer (or the fire-and-forget handshake's, so its own unmatched reply
    doesn't land as a spurious late response first) — everything else is
    answered immediately."""
    def responder(frame):
        if frame.get(protocol.KEY_COMMAND) in (protocol.CMD_COMMISSION, protocol.CMD_START_LISTENING):
            return []
        return [{protocol.KEY_MESSAGE_ID: frame[protocol.KEY_MESSAGE_ID], protocol.KEY_RESULT: None}]

    fake = FakeWebSocket(responder=responder)
    return fake, _client(mock_logger, fake, **kw)


def test_late_commission_error_names_the_job_in_the_warning(mock_logger):
    # #23: the shared unmatched-error warning already fires; commission_with_code
    # must hand it a context that names the job, not just the bare message_id.
    async def scenario():
        fake, client = _withheld_commission(mock_logger)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        with pytest.raises(asyncio.TimeoutError):
            await client.commission_with_code("MT:Y.TEST", timeout=0.02,
                                              context="commission job abc-123")

        mid = fake.sent[-1][protocol.KEY_MESSAGE_ID]
        await fake.push_frame({"message_id": mid, "error_code": 50, "details": "PASE failed"})
        for _ in range(50):
            if mock_logger.warning.called:
                break
            await asyncio.sleep(0.01)

        warnings = " ".join(str(c) for c in mock_logger.warning.call_args_list)
        assert "commission job abc-123" in warnings
        assert "50" in warnings and "PASE failed" in warnings

        await client.close()
        task.cancel()
    run(scenario())


def test_commission_with_code_default_context_names_itself(mock_logger):
    # No caller-supplied context (the historical behaviour) still logs SOMETHING
    # more useful than a bare message_id.
    async def scenario():
        fake, client = _withheld_commission(mock_logger)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        with pytest.raises(asyncio.TimeoutError):
            await client.commission_with_code("MT:Y.TEST", timeout=0.02)

        mid = fake.sent[-1][protocol.KEY_MESSAGE_ID]
        await fake.push_frame({"message_id": mid, "error_code": 1, "details": "x"})
        for _ in range(50):
            if mock_logger.warning.called:
                break
            await asyncio.sleep(0.01)

        assert "commission_with_code" in str(mock_logger.warning.call_args)

        await client.close()
        task.cancel()
    run(scenario())


def test_on_late_response_hook_receives_a_late_response_naming_the_job(mock_logger):
    async def scenario():
        seen = []
        fake, client = _withheld_commission(mock_logger, on_late_response=seen.append)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)

        with pytest.raises(asyncio.TimeoutError):
            await client.commission_with_code("MT:Y.TEST", timeout=0.02,
                                              context="commission job xyz")

        mid = fake.sent[-1][protocol.KEY_MESSAGE_ID]
        await fake.push_frame({"message_id": mid, "result": {"node_id": 99}})
        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)

        assert len(seen) == 1
        late = seen[0]
        assert late.context == "commission job xyz"
        assert late.result == {"node_id": 99}
        assert late.error is None

        await client.close()
        task.cancel()
    run(scenario())


class TestUriBuilding:
    """Local mode forces loopback; remote mode uses the fields (blank-safe).

    Regression: a user told to set the host with no port left matterServerPort
    blank; prefs.get(..., "5580") returned the empty string (key present), so
    the URI became "ws://host:/ws" and websockets silently used port 80. Local
    mode now sidesteps this entirely by ignoring host/port.
    """

    def _uri(self, prefs):
        return MatterClient(Protocol(), None, prefs, connect=lambda uri: None).uri

    def test_local_is_default_and_forces_loopback(self):
        assert self._uri({}) == "ws://localhost:5580/ws"

    def test_local_ignores_stale_host_and_port(self):
        # the exact misconfiguration from the field: host + wrong port, local mode
        prefs = {"serverLocation": "local",
                 "matterServerHost": "jobs2.local", "matterServerPort": "8176"}
        assert self._uri(prefs) == "ws://localhost:5580/ws"

    def test_remote_uses_the_fields(self):
        prefs = {"serverLocation": "remote",
                 "matterServerHost": "192.168.1.20", "matterServerPort": "5580"}
        assert self._uri(prefs) == "ws://192.168.1.20:5580/ws"

    def test_remote_blank_port_falls_back_to_default(self):
        # the original bug, still guarded on the remote path
        prefs = {"serverLocation": "remote",
                 "matterServerHost": "jobs2.local", "matterServerPort": ""}
        assert self._uri(prefs) == "ws://jobs2.local:5580/ws"

    def test_remote_values_are_trimmed(self):
        prefs = {"serverLocation": "remote",
                 "matterServerHost": " host ", "matterServerPort": " 9000 ", "matterServerPath": " /x "}
        assert self._uri(prefs) == "ws://host:9000/x"


def test_on_repeated_failure_fires_once_per_streak(mock_logger):
    """Diagnostic hook fires once after >=2 consecutive failures, not every cycle (#90)."""
    async def scenario():
        calls = []
        attempts = {"n": 0}

        async def fake_sleep(d):
            if attempts["n"] >= 4:      # let a few failed cycles run, then stop
                await client.close()
            await asyncio.sleep(0)

        def connect(uri):
            attempts["n"] += 1
            async def boom():
                raise ConnectionError("refused")
            return boom()

        client = MatterClient(Protocol(), mock_logger, {},
                              connect=connect, sleep=fake_sleep,
                              on_repeated_failure=lambda n: calls.append(n))
        await client.run()              # returns once closed
        # exactly one diagnostic despite many consecutive failures (no spam)
        assert len(calls) == 1
        # and only from the second failure onward, not the first blip
        assert calls[0] >= 2
    run(scenario())


def test_connect_log_names_the_server_version(mock_logger):
    async def scenario():
        fake = FakeWebSocket(server_info={"sdk_version": "matter-server/1.2.2", "fabric_id": "0x1"})
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        connect = [c for c in mock_logger.info.call_args_list
                   if c.args and "connected to matter-server" in str(c.args[0])]
        assert connect and connect[0].args[1] == "matter-server/1.2.2"
        await client.close()
        task.cancel()
    run(scenario())


def test_connect_log_version_unknown_when_absent(mock_logger):
    async def scenario():
        fake = FakeWebSocket(server_info={"fabric_id": "0x1"})   # no sdk_version key
        client = _client(mock_logger, fake)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        connect = [c for c in mock_logger.info.call_args_list
                   if c.args and "connected to matter-server" in str(c.args[0])]
        assert connect and connect[0].args[1] == "version unknown"
        await client.close()
        task.cancel()
    run(scenario())


def test_rearm_failure_diagnostic_allows_refire(mock_logger):
    # after the hook fires once (latched), rearm lets it fire again this streak
    calls = []
    fake = FakeWebSocket()
    client = MatterClient(Protocol(), mock_logger, {}, connect=lambda uri: returns(fake),
                          on_repeated_failure=lambda n: calls.append(n))
    client._diag_fired = True         # simulate "already fired this streak"
    client._maybe_report_repeated_failure(3)
    assert calls == []                # latched → no fire
    client.rearm_failure_diagnostic()
    client._maybe_report_repeated_failure(3)
    assert calls == [4]               # re-armed → fires again
