"""The SHARED transport's failure behaviour — the PR #121 review batch.

``test_matter_client.py`` and ``test_bridge_client.py`` cover each peer's own
vocabulary. What lives here is the machinery underneath both of them, asserted
where possible through *both* concrete clients so a fix cannot quietly become
bridge-only: a mute peer, a garbage frame, a halt racing a ``resume``, a socket
that is never closed.

Same ``asyncio.run``-per-scenario style as its siblings (the workspace's
framework Python has no pytest-asyncio). ``§N`` refers to BRIDGE_PROTOCOL.md.
"""
from __future__ import annotations

import asyncio

import pytest

import bridge_protocol
from bridge_client import BridgeClient
from matter_client import MatterClient
from protocol import Protocol
from ws_json_client import WsJsonClient

from fakes import NO_HANDSHAKE, FakeWebSocket, returns


def run(coro):
    return asyncio.run(coro)


async def settle(predicate, tries: int = 200) -> None:
    """Yield to the loop until ``predicate`` holds (or give up)."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.005)


def logged(mock_logger, level: str) -> str:
    """Every argument of every call at ``level``, flattened for substring checks."""
    return " ".join(str(call) for call in getattr(mock_logger, level).call_args_list)


def bridge_client(mock_logger, fake, **kw) -> BridgeClient:
    kw.setdefault("connect", lambda uri: returns(fake))
    return BridgeClient(mock_logger, {}, **kw)


def matter_client(mock_logger, fake, **kw) -> MatterClient:
    kw.setdefault("connect", lambda uri: returns(fake))
    return MatterClient(Protocol(), mock_logger, {}, **kw)


#: Both concrete clients, for the behaviours that must be identical in each.
CLIENTS = {"bridge": bridge_client, "matter-server": matter_client}


class TestHelloTimeout:
    """A peer that opens the socket and then says nothing must not wedge us."""

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_a_mute_peer_times_out_and_retries(self, mock_logger, monkeypatch, peer):
        # Before this, the first recv() waited forever: `connected` stayed False,
        # no reconnect was ever scheduled, and the watchdog saw a client that was
        # neither up nor retrying. The realistic cause is another service already
        # holding the port, which never gets better on its own either.
        monkeypatch.setattr(WsJsonClient, "HELLO_TIMEOUT", 0.05)

        async def scenario():
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                if len(delays) >= 2:
                    await client.close()
                await asyncio.sleep(0)

            client = CLIENTS[peer](mock_logger, FakeWebSocket(handshake=NO_HANDSHAKE),
                                   sleep=fake_sleep)
            await asyncio.wait_for(client.run(), timeout=5)

            assert delays, "a mute peer must be retried, not waited on forever"
            assert not client.connected
            warnings = logged(mock_logger, "warning")
            assert "no handshake frame within" in warnings, warnings
            assert "is another service on this port?" in warnings, warnings
        run(scenario())

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_the_timeout_is_never_logged_as_an_empty_connection_loss(
            self, mock_logger, monkeypatch, peer):
        # From 3.11 asyncio.TimeoutError IS the builtin TimeoutError, which is an
        # OSError — so without an explicit earlier branch it lands in the socket
        # handler and logs "connection lost (...): " with nothing after the colon.
        monkeypatch.setattr(WsJsonClient, "HELLO_TIMEOUT", 0.05)

        async def scenario():
            async def fake_sleep(_delay):
                await client.close()
                await asyncio.sleep(0)

            client = CLIENTS[peer](mock_logger, FakeWebSocket(handshake=NO_HANDSHAKE),
                                   sleep=fake_sleep)
            await asyncio.wait_for(client.run(), timeout=5)
            assert "connection lost" not in logged(mock_logger, "warning")
        run(scenario())


class TestFrameContainment:
    """One bad frame is not a reason to drop a working connection."""

    async def _connected(self, mock_logger, **kw):
        fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                        "bridgeVersion": "1", "matterJsVersion": "1"},
                             responder=lambda frame: [{"message_id": frame["message_id"],
                                                       "result": {"commissioned": False,
                                                                  "fabrics": [],
                                                                  "endpointCount": 0,
                                                                  "endpoints": [],
                                                                  "drift": []}}])
        client = bridge_client(mock_logger, fake, **kw)
        task = asyncio.create_task(client.run())
        await client.wait_connected(timeout=2)
        return fake, client, task

    def test_undecodable_frame_is_dropped_and_the_listener_survives(self, mock_logger):
        async def scenario():
            seen = []
            fake, client, task = await self._connected(mock_logger, on_commissioned=lambda: seen.append(1))

            await fake.push_raw("{this is not json")
            await fake.push_event("commissioned", {})
            await settle(lambda: seen)

            assert seen == [1], "the good frame after the bad one must still arrive"
            assert client.connected, "a malformed frame must not tear down the connection"
            assert "undecodable frame dropped" in logged(mock_logger, "warning")
            assert "this is not json" in logged(mock_logger, "warning")

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_non_object_frame_is_dropped_and_the_listener_survives(self, mock_logger):
        async def scenario():
            seen = []
            fake, client, task = await self._connected(mock_logger, on_commissioned=lambda: seen.append(1))

            await fake.push_frame(["not", "an", "object"])
            await fake.push_frame("a bare string")
            await fake.push_event("commissioned", {})
            await settle(lambda: seen)

            assert seen == [1]
            assert client.connected
            assert "non-object frame dropped" in logged(mock_logger, "warning")

            await client.close()
            task.cancel()
        run(scenario())

    def test_the_logged_frame_is_truncated(self, mock_logger):
        # A peer spraying megabytes must not be able to fill the Indigo event log.
        async def scenario():
            fake, client, task = await self._connected(mock_logger)
            await fake.push_raw("x" * 20_000)
            await settle(lambda: mock_logger.warning.called)

            warning = logged(mock_logger, "warning")
            assert "…" in warning
            assert len(warning) < 2_000, f"log line was {len(warning)} chars"

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_frame_that_is_neither_event_nor_response_is_logged(self, mock_logger):
        # §1 has exactly three inbound shapes. A fourth means the peer is not
        # speaking this protocol — silence made that look like a healthy socket.
        async def scenario():
            fake, client, task = await self._connected(mock_logger)
            await fake.push_frame({"greeting": "hi there"})
            await settle(lambda: "greeting" in logged(mock_logger, "debug"))

            assert "neither event nor response" in logged(mock_logger, "debug")
            assert "greeting" in logged(mock_logger, "debug")
            assert client.connected

            await client.close()
            task.cancel()
        run(scenario())


class TestSocketHygiene:
    """Every way out of a connection attempt closes the socket behind it."""

    def _explodes_on_attach(self, mock_logger, **kw):
        """A client whose handshake raises something that is nobody's error type."""
        def boom():
            raise RuntimeError("endpoint provider exploded")

        fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                        "bridgeVersion": "1", "matterJsVersion": "1"})
        return fake, bridge_client(mock_logger, fake, endpoint_provider=boom, **kw)

    def test_an_unexpected_failure_closes_the_socket_and_says_so(self, mock_logger):
        # The broad-except path used to leave the socket open. With the node
        # holding that half-open socket as its one attached client, the 1s
        # backoff turned into a connect/attach hammer against a node that was
        # refusing the new connection because of the old one.
        async def scenario():
            async def fake_sleep(_delay):
                await client.close()
                await asyncio.sleep(0)

            fake, client = self._explodes_on_attach(mock_logger, sleep=fake_sleep)
            await asyncio.wait_for(client.run(), timeout=5)

            assert fake.closed, "the socket was left open after an unexpected failure"
            reported = logged(mock_logger, "exception")
            assert "bridge node" in reported and "127.0.0.1" in reported
            assert "tearing down the connection" in reported
        run(scenario())

    def test_a_handshake_failure_streak_fires_the_supervisor_diagnostic(self, mock_logger):
        # The diagnostic used to hang off the socket-error branch only, so a node
        # that accepted connections and then failed every handshake never
        # surfaced anything for the supervisor to explain.
        async def scenario():
            calls = []
            attempts = {"n": 0}

            async def fake_sleep(_delay):
                attempts["n"] += 1
                if attempts["n"] >= 4:
                    await client.close()
                await asyncio.sleep(0)

            def boom():
                raise RuntimeError("endpoint provider exploded")

            def connect(_uri):
                return returns(FakeWebSocket(
                    handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                               "bridgeVersion": "1", "matterJsVersion": "1"}))

            client = BridgeClient(mock_logger, {}, connect=connect, sleep=fake_sleep,
                                  endpoint_provider=boom, on_repeated_failure=calls.append)
            await asyncio.wait_for(client.run(), timeout=5)
            assert len(calls) == 1 and calls[0] >= 2, calls
        run(scenario())


class TestResumeRace:
    """``resume`` clears a latch; it must not resurrect a halted iteration."""

    def test_a_halt_seen_in_this_iteration_wins_over_a_racing_resume(self, mock_logger):
        # resume() from the plugin thread can land while run() is awaiting the
        # socket teardown. Re-reading self.halted after that await would see the
        # cleared flag and reconnect — turning a fail-closed halt into the retry
        # loop the halt exists to prevent.
        class ResumingSocket(FakeWebSocket):
            client = None

            async def close(self):
                if self.client is not None:
                    self.client.resume()
                await super().close()

        async def scenario():
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                if len(delays) >= 3:
                    await client.close()
                await asyncio.sleep(0)

            fake = ResumingSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION + 1,
                                             "bridgeVersion": "1", "matterJsVersion": "1"})
            client = bridge_client(mock_logger, fake, sleep=fake_sleep)
            fake.client = client
            await asyncio.wait_for(client.run(), timeout=5)

            assert delays == [], "a halted iteration must break the loop, race or no race"
        run(scenario())

    def test_resume_is_refused_while_the_run_loop_is_live(self, mock_logger):
        async def scenario():
            fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                            "bridgeVersion": "1", "matterJsVersion": "1"},
                                 responder=lambda frame: [{"message_id": frame["message_id"],
                                                           "result": {}}])
            client = bridge_client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            client.halted = True          # as if a halt had just been latched
            client.resume()
            assert client.halted, "resume() must not touch a live run loop's state"
            assert "resume() ignored" in logged(mock_logger, "warning")

            await client.close()
            task.cancel()
        run(scenario())

    def test_resume_on_a_stopped_client_clears_the_latch_and_says_so(self, mock_logger):
        client = bridge_client(mock_logger, FakeWebSocket())
        client.halted = True
        client.halted_reason = "version_skew"
        client.resume()
        assert not client.halted and client.halted_reason is None
        assert "halt cleared" in logged(mock_logger, "info")


class TestRequestTimeouts:
    """A request nobody answers ends, and says what it was waiting for."""

    def test_a_correlated_request_times_out_and_cleans_up_its_future(self, mock_logger):
        async def scenario():
            fake = FakeWebSocket(
                handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                           "bridgeVersion": "1", "matterJsVersion": "1"},
                # attach is answered; everything after it is met with silence.
                responder=lambda frame: ([{"message_id": frame["message_id"],
                                           "result": {"commissioned": False, "fabrics": [],
                                                      "endpointCount": 0, "endpoints": [],
                                                      "drift": []}}]
                                         if frame["command"] == bridge_protocol.CMD_ATTACH else []))
            client = bridge_client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            with pytest.raises(asyncio.TimeoutError):
                await client.get_status(timeout=0.05)
            assert client._pending == {}, "a timed-out request must not leak its future"

            await client.close()
            task.cancel()
        run(scenario())

    def test_an_unanswered_attach_names_itself_in_the_log(self, mock_logger, monkeypatch):
        # The handshake's attach is pumped inline, so its deadline is a bare
        # asyncio.TimeoutError with an empty str() — "connection lost: " was all
        # the log said about the single most diagnostic failure in §2.
        monkeypatch.setattr("bridge_client.ATTACH_TIMEOUT", 0.05)

        async def scenario():
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                await client.close()
                await asyncio.sleep(0)

            fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                            "bridgeVersion": "1", "matterJsVersion": "1"},
                                 responder=lambda _frame: [])   # never answers the attach
            client = bridge_client(mock_logger, fake, sleep=fake_sleep)
            await asyncio.wait_for(client.run(), timeout=5)

            warnings = logged(mock_logger, "warning")
            assert "attach not answered within 0.05s" in warnings, warnings
            assert "connection lost" not in warnings
            assert delays, "an unanswered attach is retryable — it must reconnect"
        run(scenario())


class TestResponseCorrelation:
    """The pending-future table survives what a peer can do to it."""

    def test_a_duplicate_response_is_absorbed_and_the_loop_survives(self, mock_logger):
        async def scenario():
            status = {"commissioned": False, "fabrics": [], "endpointCount": 0,
                      "endpoints": [], "drift": []}

            def responder(frame):
                body = {"message_id": frame["message_id"], "result": status}
                # Two answers to one request: the node double-sending, or a
                # message_id it reused. Neither may resolve a future twice.
                return [body, dict(body)]

            fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                            "bridgeVersion": "1", "matterJsVersion": "1"},
                                 responder=responder)
            client = bridge_client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            first = await client.get_status(timeout=1)
            assert first.endpoint_count == 0
            # The duplicate arrives unmatched; the connection has to still work.
            second = await client.get_status(timeout=1)
            assert second.endpoint_count == 0
            assert client.connected

            await client.close()
            task.cancel()
        run(scenario())


class TestUnmatchedContext:
    """An unmatched error has to name the request, not just a message_id."""

    def test_the_context_map_is_bounded(self, mock_logger):
        from ws_json_client import SEND_CONTEXT_LIMIT

        client = bridge_client(mock_logger, FakeWebSocket())
        for i in range(SEND_CONTEXT_LIMIT + 50):
            client._remember_send_context(str(i), f"set_state dev {i}")
        assert len(client._send_context) == SEND_CONTEXT_LIMIT
        # FIFO: the oldest notes are the ones dropped.
        assert "0" not in client._send_context
        assert str(SEND_CONTEXT_LIMIT + 49) in client._send_context
