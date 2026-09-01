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
import threading

import pytest

import bridge_protocol
import protocol
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


class TestRetryNow:
    """The E7 post-install poke (issue #135) — cutting a grown backoff short.

    ``sleep`` stays the seam of record: every scenario here injects a
    ``gated_sleep``/``hanging_sleep`` that only ends when the TEST opens a gate
    it controls, or when ``retry_now()`` does — so a wait that completes is
    provably one or the other, never a race decided by real wall-clock timing.
    """

    def _boom_connect(self):
        def connect(_uri):
            async def boom():
                raise ConnectionError("node not there yet")
            return boom()
        return connect

    def test_retry_now_cuts_the_wait_short_and_resets_the_next_delay_to_1s(self, mock_logger):
        # Two waits are let through by hand (proving normal growth to [1, 2, 4]);
        # the THIRD is never opened — only retry_now() can end it — and the
        # fourth wait's delay must be back down to 1s, not climbing to 8.
        async def scenario():
            delays = []
            gate = asyncio.Event()

            async def gated_sleep(delay):
                delays.append(delay)
                await gate.wait()
                gate.clear()

            client = bridge_client(mock_logger, None, connect=self._boom_connect(),
                                   sleep=gated_sleep)
            task = asyncio.create_task(client.run())

            await settle(lambda: len(delays) >= 1)
            gate.set()
            await settle(lambda: len(delays) >= 2)
            gate.set()
            await settle(lambda: len(delays) >= 3)
            assert delays == [1, 2, 4], delays  # grown naturally; nothing poked yet

            client.retry_now()  # the gate for a 4th wait is never opened
            await settle(lambda: len(delays) >= 4)
            assert delays == [1, 2, 4, 1], (
                f"retry_now() must both cut the wait short and reset the backoff "
                f"(mutation check: a version without `attempt = 0` in the wake path "
                f"would show 8 here, not 1) — got {delays}")

            await client.close()
            task.cancel()
        run(asyncio.wait_for(scenario(), timeout=5))

    def test_poking_a_connected_not_sleeping_client_is_harmless(self, mock_logger):
        # FIX 3 regression: a poke while connected arms `_retry_event` even
        # though nothing is waiting on it yet (harmless in the moment — the
        # original point of this test). Left armed, it used to silently skip
        # the FIRST backoff after a LATER, wholly unrelated drop — potentially
        # hours after anyone thought about the poke. `_mark_connected` clears
        # it, so this continues past the poke through two drops: the first
        # (the same connected session the poke armed) still gets the one skip
        # that is a poke's whole point; the reconnect that follows it clears
        # the stale arm, so the SECOND drop's backoff is a real wait — proven
        # by requiring the gate before it resolves, not just by the delay it
        # logged (a skipped wait logs the same delay a real one does).
        async def scenario():
            delays = []
            gate = asyncio.Event()
            fakes: list[FakeWebSocket] = []

            async def gated_sleep(delay):
                delays.append(delay)
                await gate.wait()
                gate.clear()

            def connect(_uri):
                fake = FakeWebSocket(
                    handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                               "bridgeVersion": "1", "matterJsVersion": "1"},
                    responder=lambda frame: [{"message_id": frame["message_id"],
                                              "result": {"commissioned": False, "fabrics": [],
                                                         "endpointCount": 0, "endpoints": [],
                                                         "drift": []}}])
                fakes.append(fake)
                return returns(fake)

            client = bridge_client(mock_logger, None, connect=connect, sleep=gated_sleep)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            client.retry_now()  # nothing is waiting on the backoff event; must not raise
            assert client.connected

            await fakes[-1].close()  # an unrelated drop, well after the poke above
            await settle(lambda: len(delays) >= 1)
            assert delays == [1], "the (skipped) first backoff still logs its intended delay"

            # The reconnect that follows must succeed WITHOUT the gate — the
            # stale event, not yet cleared by any _mark_connected, wakes it.
            await client.wait_connected(timeout=2)
            assert not client._retry_event.is_set(), \
                "_mark_connected must clear a poke armed while previously connected"

            await fakes[-1].close()  # a second, genuinely unrelated drop
            await settle(lambda: len(delays) >= 2)
            assert delays == [1, 1], "the second backoff's delay must still be logged"
            # And this one must NOT resolve on its own — mutation check: remove
            # the clear-on-wake in _wait_for_retry and the stale arm survives
            # the FIRST wait, so this second wait is skipped too and this
            # wait_connected succeeds instead of timing out. (_mark_connected's
            # own clear is belt-and-braces for the close()-then-new-run() path,
            # which no backoff wait ever consumes.)
            with pytest.raises(asyncio.TimeoutError):
                await client.wait_connected(timeout=0.1)

            gate.set()
            await client.wait_connected(timeout=2)
            assert client.connected

            await client.close()
            task.cancel()
        run(asyncio.wait_for(scenario(), timeout=5))

    def test_poking_a_halted_client_does_nothing(self, mock_logger):
        async def scenario():
            fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                            "bridgeVersion": "1", "matterJsVersion": "1"},
                                 responder=lambda frame: [{"message_id": frame["message_id"],
                                                           "result": {"commissioned": False,
                                                                      "fabrics": [],
                                                                      "endpointCount": 0,
                                                                      "endpoints": [],
                                                                      "drift": []}}])
            client = bridge_client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            client.halted = True  # as if a halt had just been latched
            client.retry_now()

            assert client.halted, "a halted client must stay halted after a poke"
            assert not client._retry_event.is_set(), \
                "a halted poke must not even arm the wake-up event"

            await client.close()
            task.cancel()
        run(scenario())

    def test_retry_now_from_a_foreign_thread_reaches_the_loop(self, mock_logger):
        # The npm install runs on a plain threading.Thread (E7) — retry_now()
        # has to reach a loop it does not own.
        async def scenario():
            delays = []

            async def hanging_sleep(delay):
                delays.append(delay)
                await asyncio.Future()  # only cancellation (via retry_now) ends this

            client = bridge_client(mock_logger, None, connect=self._boom_connect(),
                                   sleep=hanging_sleep)
            task = asyncio.create_task(client.run())
            await settle(lambda: delays)  # the first backoff wait is underway

            thread = threading.Thread(target=client.retry_now, daemon=True)
            thread.start()
            thread.join(timeout=2)

            await settle(lambda: len(delays) >= 2)
            assert len(delays) >= 2, "a poke from a foreign thread must reach the run loop"

            await client.close()
            task.cancel()
        run(asyncio.wait_for(scenario(), timeout=5))

    def test_retry_now_returns_false_when_the_loop_closes_out_from_under_it(self, mock_logger):
        # FIX 4: the loop can close between retry_now() reading self._loop and
        # calling call_soon_threadsafe on it — plugin shutdown racing an
        # in-flight install poke. That must decline, not raise, on the install
        # thread (a raised RuntimeError there would print a wrong install
        # failure over a poke that simply lost a race).
        async def scenario():
            fake = FakeWebSocket(handshake={"protocolVersion": bridge_protocol.PROTOCOL_VERSION,
                                            "bridgeVersion": "1", "matterJsVersion": "1"},
                                 responder=lambda frame: [{"message_id": frame["message_id"],
                                                           "result": {"commissioned": False,
                                                                      "fabrics": [],
                                                                      "endpointCount": 0,
                                                                      "endpoints": [],
                                                                      "drift": []}}])
            client = bridge_client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            closed_loop = asyncio.new_event_loop()
            closed_loop.close()
            client._loop = closed_loop  # simulate the shutdown race directly

            assert client.retry_now() is False, "a closed loop must decline, not raise"

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_wake_rearms_the_failure_diagnostic_so_continued_failures_refire_it(
            self, mock_logger):
        # FIX 6: `attempt = 0` on the wake path rewinds the backoff, but
        # `_diag_fired` used to stay latched — so "the install succeeded, we
        # poked, and it STILL won't connect" never surfaced the reason again
        # for the rest of the (new) streak. This drives two full streaks: the
        # first fires the diagnostic normally, a poke mid-second-wait cuts the
        # backoff short (and must rearm), and continued failures after that
        # must fire it a SECOND time rather than staying silent.
        async def scenario():
            calls = []
            delays = []
            gate = asyncio.Event()

            async def gated_sleep(delay):
                delays.append(delay)
                await gate.wait()
                gate.clear()

            client = bridge_client(mock_logger, None, connect=self._boom_connect(),
                                   sleep=gated_sleep, on_repeated_failure=calls.append)
            task = asyncio.create_task(client.run())

            await settle(lambda: len(delays) >= 1)      # wait #1 (delay=1)
            gate.set()
            await settle(lambda: len(delays) >= 2)      # wait #2 (delay=2)
            assert len(calls) == 1 and calls[0] >= 2, calls

            client.retry_now()                          # cuts wait #2 short; must rearm
            await settle(lambda: len(delays) >= 3)       # wait #3 (delay=1, backoff reset)
            gate.set()
            await settle(lambda: len(delays) >= 4)       # wait #4 (delay=2)
            assert len(calls) == 2 and calls[1] >= 2, (
                "the diagnostic must re-fire for the streak after the poke "
                f"(mutation check: dropping rearm_failure_diagnostic() leaves this at 1) — {calls}")

            await client.close()
            task.cancel()
        run(asyncio.wait_for(scenario(), timeout=5))


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
        # The deadline is derived from the endpoint count (E3b), so pin the
        # formula rather than its floor constant.
        monkeypatch.setattr("bridge_client.attach_timeout_for", lambda _count: 0.05)

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


class TestLateResponse:
    """A response that arrives after its own request gave up (#23).

    The transport stays commissioning-agnostic here: ``context`` is an opaque
    str-able object, and these tests use plain strings — matter_client and
    commission_jobs cover what a real caller's context object looks like.
    """

    def _withheld(self, mock_logger, **kw):
        """A matter-server client that never answers a ``slow_thing`` request
        (or the fire-and-forget handshake ``start_listening``, whose own
        unmatched reply would otherwise land as a spurious late response and
        confuse these scenarios) but answers everything else immediately — so
        a scenario can prove the dispatcher survived a late response by
        sending a normal one after."""
        def responder(frame):
            if frame.get(protocol.KEY_COMMAND) in ("slow_thing", protocol.CMD_START_LISTENING):
                return []
            return [{protocol.KEY_MESSAGE_ID: frame[protocol.KEY_MESSAGE_ID], protocol.KEY_RESULT: None}]

        fake = FakeWebSocket(responder=responder)
        client = matter_client(mock_logger, fake, **kw)
        return fake, client

    def test_a_late_result_reaches_the_hook_with_its_context(self, mock_logger):
        async def scenario():
            seen = []
            fake, client = self._withheld(mock_logger, on_late_response=seen.append)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            frame = client.proto.build_request("slow_thing")
            mid = frame["message_id"]
            with pytest.raises(asyncio.TimeoutError):
                await client._request_frame(frame, 0.02, context="my request")

            await fake.push_frame({"message_id": mid, "result": {"node_id": 7}})
            await settle(lambda: seen)

            assert len(seen) == 1
            late = seen[0]
            assert late.message_id == mid
            assert late.context == "my request"
            assert late.error is None
            assert late.result == {"node_id": 7}

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_late_error_reaches_the_hook_with_its_context(self, mock_logger):
        async def scenario():
            seen = []
            fake, client = self._withheld(mock_logger, on_late_response=seen.append)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            frame = client.proto.build_request("slow_thing")
            mid = frame["message_id"]
            with pytest.raises(asyncio.TimeoutError):
                await client._request_frame(frame, 0.02, context="my request")

            await fake.push_frame({"message_id": mid, "error_code": 50, "details": "boom"})
            await settle(lambda: seen)

            assert len(seen) == 1
            late = seen[0]
            assert late.error == (50, "boom")
            assert late.result is None
            assert late.context == "my request"

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_raising_hook_does_not_kill_the_dispatcher(self, mock_logger):
        async def scenario():
            def boom(_late):
                raise RuntimeError("hook exploded")

            fake, client = self._withheld(mock_logger, on_late_response=boom)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            frame = client.proto.build_request("slow_thing")
            mid = frame["message_id"]
            with pytest.raises(asyncio.TimeoutError):
                await client._request_frame(frame, 0.02, context="my request")

            await fake.push_frame({"message_id": mid, "result": {}})
            await settle(lambda: mock_logger.exception.called)

            assert client.connected
            assert "on_late_response hook raised" in str(mock_logger.exception.call_args)

            # the listen loop survives: a fresh, correlated request still
            # round-trips normally afterwards
            nodes = await client.request(protocol.CMD_GET_NODES)
            assert nodes is None  # withheld responder answers unknown commands with None...

            await client.close()
            task.cancel()
        run(scenario())

    def test_an_abandoned_request_keeps_its_context_note(self, mock_logger):
        async def scenario():
            fake, client = self._withheld(mock_logger)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            frame = client.proto.build_request("slow_thing")
            mid = frame["message_id"]
            with pytest.raises(asyncio.TimeoutError):
                await client._request_frame(frame, 0.02, context="my request")

            assert client._pending == {}, "the pending future is still cleaned up"
            assert client._send_context.get(mid) == "my request", (
                "the note must survive the timeout — it is the whole point: a "
                "later answer still has something to describe itself with")

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_request_without_context_notes_nothing_but_the_hook_still_fires(self, mock_logger):
        async def scenario():
            seen = []
            fake, client = self._withheld(mock_logger, on_late_response=seen.append)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            frame = client.proto.build_request("slow_thing")
            mid = frame["message_id"]
            with pytest.raises(asyncio.TimeoutError):
                await client._request_frame(frame, 0.02)  # no context passed

            assert mid not in client._send_context, "nothing was noted to forget"

            await fake.push_frame({"message_id": mid, "result": {"ok": True}})
            await settle(lambda: seen)

            assert len(seen) == 1
            assert seen[0].context is None
            assert seen[0].result == {"ok": True}

            await client.close()
            task.cancel()
        run(scenario())


class TestExpectedOutage:
    """#340: a peer WE took down must not be reported as a peer that fell over.

    The risk this class exists to pin is not the quiet — it is what the quiet
    could swallow. Every test below asks "when could this go silent and be
    wrong?" rather than "does it go silent?".
    """

    def _boom_connect(self):
        def connect(_uri):
            async def boom():
                raise ConnectionError("node not there yet")
            return boom()
        return connect

    async def _two_failures(self, mock_logger, peer, **kw):
        """Drive two failed connection attempts, then stop. Returns the client."""
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)
            if len(delays) >= 2:
                await client.close()
            await asyncio.sleep(0)

        client = CLIENTS[peer](mock_logger, None, connect=self._boom_connect(),
                               sleep=fake_sleep, **kw)
        await asyncio.wait_for(client.run(), timeout=5)
        assert len(delays) >= 2, delays
        return client

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_reconnect_failures_are_warnings_when_nobody_ordered_the_outage(
            self, mock_logger, peer):
        # The baseline, and the half that must NOT change: with no supervisor
        # window (remote mode, or a peer that simply died), a refused connect is
        # still news at WARNING.
        run(self._two_failures(mock_logger, peer))
        assert "connection lost" in logged(mock_logger, "warning")

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_reconnect_failures_drop_to_debug_inside_the_window(self, mock_logger, peer):
        run(self._two_failures(mock_logger, peer, outage_expected=lambda: True))
        assert "connection lost" not in logged(mock_logger, "warning"), (
            "an outage the supervisor ordered must not be reported as a failure")
        assert "connection lost" in logged(mock_logger, "debug"), (
            "quieted is not deleted — the attempts must still be traceable at debug")

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_the_failure_is_still_COUNTED_inside_the_window(self, mock_logger, peer):
        # The one that matters. Demoting the per-attempt line must not also
        # disarm the supervisor's diagnostic: the supervisor is the side that
        # knows when its window closes, and it can only report a restart that
        # never came back if this hook keeps firing underneath the quiet.
        calls = []
        run(self._two_failures(mock_logger, peer,
                               outage_expected=lambda: True,
                               on_repeated_failure=calls.append))
        assert calls, ("the repeated-failure hook must fire inside the window — without it "
                       "a node that never returns is silent for the whole streak")

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_the_window_closing_makes_the_next_attempt_loud_again(self, mock_logger, peer):
        # The window is a live question, not a snapshot taken when the client
        # was built: a supervisor that disarms it (its restart failed) must get
        # the warnings back on the very next attempt, with no reconnection of
        # any kind in between.
        open_window = [True]

        async def scenario():
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                open_window[0] = False        # the supervisor gives up mid-streak
                if len(delays) >= 2:
                    await client.close()
                await asyncio.sleep(0)

            client = CLIENTS[peer](mock_logger, None, connect=self._boom_connect(),
                                   sleep=fake_sleep,
                                   outage_expected=lambda: open_window[0])
            await asyncio.wait_for(client.run(), timeout=5)
        run(scenario())
        assert "connection lost" in logged(mock_logger, "warning")

    @pytest.mark.parametrize("peer", sorted(CLIENTS))
    def test_a_raising_window_check_is_treated_as_no_window(self, mock_logger, peer):
        # Fail loud, not quiet. A predicate that raises is a broken supervisor,
        # and the wrong way to fail is the one where the log goes silent —
        # nothing would ever surface the broken predicate either.
        def boom():
            raise RuntimeError("supervisor predicate is broken")

        run(self._two_failures(mock_logger, peer, outage_expected=boom))
        assert "connection lost" in logged(mock_logger, "warning")

    def test_an_unexpected_teardown_still_gets_its_traceback_inside_the_window(
            self, mock_logger):
        # The `logger.exception` branch is deliberately NOT routed through the
        # demotion: it means this client hit something it does not understand,
        # which a restart in progress is no reason to hide. Pinned by handing it
        # a failure the WS_ERRORS branch cannot catch.
        async def scenario():
            delays = []

            def connect(_uri):
                async def boom():
                    raise ValueError("not a socket error at all")
                return boom()

            async def fake_sleep(delay):
                delays.append(delay)
                await client.close()
                await asyncio.sleep(0)

            client = bridge_client(mock_logger, None, connect=connect, sleep=fake_sleep,
                                   outage_expected=lambda: True)
            await asyncio.wait_for(client.run(), timeout=5)
        run(scenario())
        assert mock_logger.exception.call_args_list, (
            "an unexpected teardown must keep its traceback even mid-restart")
