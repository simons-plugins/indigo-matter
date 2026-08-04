"""E1: the bridge-node WS client — handshake, commands, events, reconnect.

Driven against the in-process FakeWebSocket (no Node process), in the same
``asyncio.run``-per-scenario style as ``test_matter_client.py``: the workspace's
framework Python has no pytest-asyncio.

Request/response shapes are asserted against the golden frames shared with the
bridge node's TypeScript suite (BRIDGE_PROTOCOL §7), not against restated dicts.
References to ``§N`` are BRIDGE_PROTOCOL.md sections.
"""
from __future__ import annotations

import asyncio

import pytest

import bridge_protocol
from bridge_client import BridgeClient
from bridge_protocol import EndpointSpec
from matter_client import MatterClient
from protocol import Protocol

from conftest import load_bridge_frames
from fakes import FakeWebSocket, returns

FRAMES = load_bridge_frames()
PENDING = FRAMES["pending"]

HELLO = FRAMES["handshake"]
SKEWED_HELLO = {**HELLO, "protocolVersion": bridge_protocol.PROTOCOL_VERSION + 1}

#: Golden responses keyed by the command that provokes them.
RESPONSES = {
    bridge_protocol.CMD_ATTACH: FRAMES["attach"]["response"],
    bridge_protocol.CMD_GET_STATUS: FRAMES["get_status"]["response"],
    bridge_protocol.CMD_GET_PAIRING: FRAMES["get_pairing_commissioned"]["response"],
    bridge_protocol.CMD_OPEN_WINDOW: FRAMES["open_commissioning_window"]["response"],
    bridge_protocol.CMD_UPSERT_ENDPOINT: PENDING["upsert_endpoint"]["response"],
    bridge_protocol.CMD_REMOVE_ENDPOINT: PENDING["remove_endpoint"]["response"],
    bridge_protocol.CMD_SET_STATE: PENDING["set_state"]["response"],
    bridge_protocol.CMD_SET_REACHABLE: PENDING["set_reachable"]["response"],
    bridge_protocol.CMD_REMOVE_FABRIC: PENDING["remove_fabric"]["response"],
    bridge_protocol.CMD_FACTORY_RESET: PENDING["factory_reset"]["response"],
    bridge_protocol.CMD_REBUILD_ENDPOINT_MAP: PENDING["rebuild_endpoint_map"]["response"],
}

KITCHEN_LAMP = EndpointSpec(
    indigo_device_id=123456789, role="onOffLight", label="Kitchen Lamp",
    reachable=True, states={"onOff": True}, options={},
)


def run(coro):
    return asyncio.run(coro)


def golden_responder(overrides=None, silent=()):
    """Answer each command with its golden response (message_id substituted)."""
    table = {**RESPONSES, **(overrides or {})}

    def _respond(frame: dict) -> list:
        command = frame["command"]
        if command in silent:
            return []
        body = table.get(command)
        if body is None:
            return [{"message_id": frame["message_id"], "result": None}]
        return [{**body, "message_id": frame["message_id"]}]

    return _respond


def _fake(**kw) -> FakeWebSocket:
    kw.setdefault("responder", golden_responder())
    kw.setdefault("handshake", HELLO)
    return FakeWebSocket(**kw)


def _client(mock_logger, fake, **kw) -> BridgeClient:
    kw.setdefault("plugin_version", "2026.8.1")
    kw.setdefault("connect", lambda uri: returns(fake))
    return BridgeClient(mock_logger, {}, **kw)


def sent(fake: FakeWebSocket, command: str) -> dict:
    frames = [f for f in fake.sent if f.get("command") == command]
    assert frames, f"no {command} frame sent (saw {fake.sent_commands()})"
    return frames[-1]


async def settle(predicate, tries: int = 100) -> None:
    """Yield to the loop until ``predicate`` holds (or give up)."""
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.01)


class TestHandshake:
    """§2 — hello, then attach, or nothing at all."""

    def test_attaches_and_delivers_the_status_report(self, mock_logger):
        async def scenario():
            attached = []
            fake = _fake()
            client = _client(mock_logger, fake,
                             endpoint_provider=lambda: [KITCHEN_LAMP],
                             on_attached=attached.append)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            await settle(lambda: attached)

            frame = sent(fake, bridge_protocol.CMD_ATTACH)
            assert frame["args"]["protocolVersion"] == bridge_protocol.PROTOCOL_VERSION
            assert frame["args"]["pluginVersion"] == "2026.8.1"
            assert frame["args"]["endpoints"] == [KITCHEN_LAMP.to_wire()]
            # §3.1: no intent flag unless the caller deliberately empties the set.
            assert bridge_protocol.ARG_INTENT not in frame["args"]

            assert attached, "on_attached never fired"
            # The golden attach answers the golden attach REQUEST, which carries
            # `endpoints: []` — so the lawful §3.1 answer is an empty live set.
            assert attached[0].endpoint_count == 0
            assert client.status is attached[0]
            assert client.attached, "a successful attach is what `attached` means"
            assert client.hello.bridge_version == HELLO["bridgeVersion"]

            await client.close()
            task.cancel()
        run(scenario())

    def test_version_skew_refuses_to_attach_and_stops_reconnecting(self, mock_logger):
        # §2 + invariant 6.5: skew fails closed — no attach, no retry, pairings
        # untouched. The user is told to restart the agent; the plugin does not
        # sit in a reconnect loop pretending it might get better.
        async def scenario():
            skews = []
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                await asyncio.sleep(0)

            fake = _fake(handshake=SKEWED_HELLO)
            client = _client(mock_logger, fake, on_version_skew=skews.append, sleep=fake_sleep)
            await asyncio.wait_for(client.run(), timeout=2)  # returns; does not loop

            assert client.halted
            assert not client.connected
            assert delays == [], "a skewed node must not be retried"
            assert bridge_protocol.CMD_ATTACH not in fake.sent_commands()
            assert skews and skews[0].protocol_version == SKEWED_HELLO["protocolVersion"]
            assert skews[0].bridge_version == HELLO["bridgeVersion"]
            mock_logger.error.assert_called()

            client.resume()          # the agent was restarted; retries allowed again
            assert not client.halted
        run(scenario())

    def test_non_hello_first_frame_is_refused(self, mock_logger):
        async def scenario():
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                await asyncio.sleep(0)

            fake = _fake(handshake={"greeting": "hi"})
            client = _client(mock_logger, fake, sleep=fake_sleep)
            task = asyncio.create_task(client.run())
            await settle(lambda: delays)
            assert bridge_protocol.CMD_ATTACH not in fake.sent_commands()
            assert not client.connected
            await client.close()
            task.cancel()
        run(scenario())

    def test_reconnect_reattaches_with_a_fresh_endpoint_list(self, mock_logger):
        # PRD §5.4: every connection is a full reconcile, so the list must be read
        # again — the allow-list may have changed while the socket was down.
        async def scenario():
            allow_list = [KITCHEN_LAMP]
            fakes = [_fake(), _fake()]
            index = {"i": 0}

            def connect(_uri):
                i = index["i"]
                index["i"] += 1
                return returns(fakes[min(i, len(fakes) - 1)])

            async def fake_sleep(_delay):
                await asyncio.sleep(0)

            client = _client(mock_logger, fakes[0], connect=connect,
                             endpoint_provider=lambda: list(allow_list), sleep=fake_sleep)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            allow_list.append(EndpointSpec(indigo_device_id=123456790, role="doorLock",
                                           label="Front Door", states={"locked": True}))
            await fakes[0].close()   # drop → backoff → reconnect
            await settle(lambda: bridge_protocol.CMD_ATTACH in fakes[1].sent_commands())

            second = sent(fakes[1], bridge_protocol.CMD_ATTACH)
            assert [ep["indigoDeviceId"] for ep in second["args"]["endpoints"]] == [123456789, 123456790]

            await client.close()
            task.cancel()
        run(scenario())

    def test_attach_replace_all_is_opt_in(self, mock_logger):
        async def scenario():
            fake = _fake()
            client = _client(mock_logger, fake, endpoint_provider=lambda: [KITCHEN_LAMP])
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            await client.attach([], replace_all=True)   # the §7 allow-list-emptied path
            frame = sent(fake, bridge_protocol.CMD_ATTACH)
            assert frame["args"]["endpoints"] == []
            assert frame["args"][bridge_protocol.ARG_INTENT] == bridge_protocol.INTENT_REPLACE_ALL

            await client.close()
            task.cancel()
        run(scenario())


class TestCommands:
    """§3 — each method's request and parsed result against the golden frames."""

    def _exchange(self, mock_logger, call, key, section):
        """Run ``call`` on a connected client and check it against fixture ``key``."""
        captured = {}

        async def scenario():
            fake = _fake()
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            captured["result"] = await call(client)
            expected = section[key]["request"]
            frame = sent(fake, expected["command"])
            assert frame["args"] == expected["args"]
            await client.close()
            task.cancel()
        run(scenario())
        return captured["result"]

    def test_upsert_endpoint(self, mock_logger):
        result = self._exchange(
            mock_logger, lambda c: c.upsert_endpoint(KITCHEN_LAMP), "upsert_endpoint", PENDING)
        assert result == 2

    def test_upsert_endpoint_accepts_a_wire_dict(self, mock_logger):
        wire = PENDING["upsert_endpoint"]["request"]["args"]["endpoint"]
        assert self._exchange(mock_logger, lambda c: c.upsert_endpoint(wire),
                              "upsert_endpoint", PENDING) == 2

    def test_remove_endpoint(self, mock_logger):
        assert self._exchange(mock_logger, lambda c: c.remove_endpoint(123456789),
                              "remove_endpoint", PENDING) is True

    def test_remove_endpoint_absent_is_not_an_error(self, mock_logger):
        # §3.3: idempotent — removing what is not there succeeds with removed=false.
        async def scenario():
            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_REMOVE_ENDPOINT: PENDING["remove_endpoint_absent"]["response"]}))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            assert await client.remove_endpoint(123456791) is False
            await client.close()
            task.cancel()
        run(scenario())

    def test_set_reachable(self, mock_logger):
        self._exchange(mock_logger, lambda c: c.set_reachable(123456789, False),
                       "set_reachable", PENDING)

    def test_get_status(self, mock_logger):
        status = self._exchange(mock_logger, lambda c: c.get_status(), "get_status", FRAMES)
        assert status.endpoint_count == 1
        assert status.endpoints[0].role == "onOffPlugInUnit"

    def test_get_pairing(self, mock_logger):
        pairing = self._exchange(mock_logger, lambda c: c.get_pairing(),
                                 "get_pairing_commissioned", FRAMES)
        assert pairing.commissioned is True
        assert pairing.manual_pairing_code is None
        assert pairing.fabrics[0].label == "Apple Home"

    def test_open_commissioning_window(self, mock_logger):
        window = self._exchange(mock_logger, lambda c: c.open_commissioning_window(900),
                                "open_commissioning_window", FRAMES)
        assert window.qr_pairing_code.startswith("MT:")

    def test_open_commissioning_window_defaults_to_the_matter_maximum(self, mock_logger):
        self._exchange(mock_logger, lambda c: c.open_commissioning_window(),
                       "open_commissioning_window", FRAMES)

    def test_remove_fabric(self, mock_logger):
        self._exchange(mock_logger, lambda c: c.remove_fabric(2), "remove_fabric", PENDING)

    def test_factory_reset_preserves_endpoint_numbers_by_default(self, mock_logger):
        self._exchange(mock_logger, lambda c: c.factory_reset(), "factory_reset", PENDING)

    def test_factory_reset_can_discard_the_map(self, mock_logger):
        self._exchange(mock_logger, lambda c: c.factory_reset(False),
                       "factory_reset_discard_map", PENDING)

    def test_rebuild_endpoint_map(self, mock_logger):
        status = self._exchange(mock_logger, lambda c: c.rebuild_endpoint_map(),
                                "rebuild_endpoint_map", PENDING)
        assert status.endpoint_count == 2
        # §3.11 REallocates — the numbers differ from the ones attach reported.
        assert status.endpoints[1].endpoint_number == 5

    def test_error_response_raises(self, mock_logger):
        async def scenario():
            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_UPSERT_ENDPOINT: PENDING["upsert_endpoint_role_change"]["response"]}))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            with pytest.raises(bridge_protocol.BridgeProtocolError) as excinfo:
                await client.upsert_endpoint(KITCHEN_LAMP)
            assert excinfo.value.code == bridge_protocol.ERR_ROLE_CHANGE
            await client.close()
            task.cancel()
        run(scenario())


class TestSetState:
    """§3.4 — fire-and-forget, because Indigo's device thread is on the line."""

    def test_returns_without_awaiting_a_response(self, mock_logger):
        async def scenario():
            # The node never answers set_state here: an awaiting implementation
            # would hang until its timeout, and the whole point is that it doesn't.
            fake = _fake(responder=golden_responder(silent=(bridge_protocol.CMD_SET_STATE,)))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            await asyncio.wait_for(client.set_state(123456789, {"onOff": True}), timeout=0.5)
            frame = sent(fake, bridge_protocol.CMD_SET_STATE)
            assert frame["args"] == PENDING["set_state"]["request"]["args"]
            assert client._pending == {}, "set_state must not register a pending future"

            await client.close()
            task.cancel()
        run(scenario())

    def test_unmatched_error_response_is_logged(self, mock_logger):
        # §3.4: the response to a fire-and-forget request still arrives, and an
        # unnoticed failure looks exactly like "the ecosystem shows stale state".
        async def scenario():
            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_SET_STATE: PENDING["set_state_unknown_device"]["response"]}))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            await client.set_state(123456791, {"onOff": True})
            await settle(lambda: mock_logger.warning.called)

            warnings = [str(call.args) for call in mock_logger.warning.call_args_list]
            assert any(bridge_protocol.ERR_UNKNOWN_DEVICE in text for text in warnings), warnings

            await client.close()
            task.cancel()
        run(scenario())

    def test_unmatched_success_response_is_not_a_warning(self, mock_logger):
        async def scenario():
            fake = _fake()
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            mock_logger.warning.reset_mock()

            await client.set_state(123456789, {"onOff": True})
            await settle(lambda: mock_logger.debug.called)
            assert not mock_logger.warning.called

            await client.close()
            task.cancel()
        run(scenario())

    def test_push_while_disconnected_is_dropped_not_raised(self, mock_logger):
        # §6.2: the node caches nothing across reconnects and attach re-delivers
        # the full state, so there is nothing to recover — and the device thread
        # must not eat an exception for a bridge that happens to be restarting.
        async def scenario():
            client = _client(mock_logger, _fake())
            await client.set_state(123456789, {"onOff": True})   # never ran run()
            mock_logger.debug.assert_called()
        run(scenario())


class TestEvents:
    """§5 — events reach the injected callbacks; unknown ones are logged."""

    def _push(self, mock_logger, frames, **callbacks):
        async def scenario():
            fake = _fake()
            client = _client(mock_logger, fake, **callbacks)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            for frame in frames:
                await fake.push_event(frame["event"], frame["data"])
            await settle(lambda: client.last_event_ts is not None)
            await asyncio.sleep(0.02)
            await client.close()
            task.cancel()
        run(scenario())

    def test_command_events_reach_on_command(self, mock_logger):
        received = []
        self._push(mock_logger,
                   [FRAMES["command_on_off"], FRAMES["command_set_level"], FRAMES["command_lock"]],
                   on_command=received.append)
        assert [(c.indigo_device_id, c.command, c.args) for c in received] == [
            (123456789, "onOff", {"value": True}),
            (123456789, "setLevel", {"level": 60}),
            (123456790, "lock", {}),
        ]

    def test_fabric_events(self, mock_logger):
        changes, commissioned, decommissioned = [], [], []
        self._push(mock_logger,
                   [FRAMES["fabrics_changed_added"], FRAMES["commissioned"], FRAMES["decommissioned"]],
                   on_fabrics_changed=lambda fabrics, change: changes.append((fabrics, change)),
                   on_commissioned=lambda: commissioned.append(1),
                   on_decommissioned=lambda: decommissioned.append(1))
        assert changes and changes[0][1] == "added"
        assert changes[0][0][0].vendor_id == 4937
        assert commissioned == [1] and decommissioned == [1]

    def test_window_closed_carries_the_reason(self, mock_logger):
        reasons = []
        self._push(mock_logger,
                   [FRAMES["window_closed_expired"], FRAMES["window_closed_commissioned"]],
                   on_window_closed=reasons.append)
        assert reasons == ["expired", "commissioned"]

    def test_drift_detected(self, mock_logger):
        drifts = []
        self._push(mock_logger, [FRAMES["drift_detected"]], on_drift_detected=drifts.append)
        assert drifts and drifts[0][0].unique_id == "indigo-123456789"

    def test_unknown_event_is_logged_and_dropped(self, mock_logger):
        # §1: unknown events are logged and dropped, never fatal.
        self._push(mock_logger, [{"event": "teleported", "data": {}}])
        assert any("teleported" in str(call.args) for call in mock_logger.warning.call_args_list)

    def test_a_raising_callback_does_not_kill_the_loop(self, mock_logger):
        def boom(_command):
            raise RuntimeError("handler exploded")

        async def scenario():
            fake = _fake()
            client = _client(mock_logger, fake, on_command=boom)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            await fake.push_event("command", FRAMES["command_on_off"]["data"])
            await settle(lambda: mock_logger.exception.called)
            assert client.connected            # still listening
            mock_logger.exception.assert_called()
            await client.close()
            task.cancel()
        run(scenario())


class TestTransport:
    """Behaviour inherited from WsJsonClient, asserted for this peer too."""

    def test_in_flight_request_fails_on_disconnect_instead_of_hanging(self, mock_logger):
        async def scenario():
            fake = _fake(responder=golden_responder(silent=(bridge_protocol.CMD_GET_STATUS,)))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            pending = asyncio.create_task(client.get_status())
            await asyncio.sleep(0.02)
            await fake.close()

            with pytest.raises(ConnectionError):
                await asyncio.wait_for(pending, timeout=2)

            await client.close()
            task.cancel()
        run(scenario())

    def test_request_when_disconnected_raises(self, mock_logger):
        async def scenario():
            client = _client(mock_logger, _fake())
            with pytest.raises(ConnectionError):
                await client.get_status()
        run(scenario())

    def test_on_disconnect_fires_only_on_a_real_drop(self, mock_logger):
        async def scenario():
            events = []
            fake = _fake()
            client = _client(mock_logger, fake, on_disconnect=lambda: events.append("down"),
                             sleep=lambda _d: asyncio.sleep(0))
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            await fake.close()
            await settle(lambda: events)
            assert events == ["down"]
            await client.close()
            task.cancel()
        run(scenario())

    def test_backoff_matches_the_controller_client(self, mock_logger):
        # One run loop, one backoff policy: min(2**attempt, 30). If the shared
        # base ever grows a peer-specific delay, this is what catches it.
        def _delays(factory):
            async def scenario():
                delays = []
                attempts = {"n": 0}

                async def fake_sleep(delay):
                    delays.append(delay)
                    if len(delays) >= 8:
                        await client.close()
                    await asyncio.sleep(0)

                def connect(_uri):
                    attempts["n"] += 1

                    async def boom():
                        raise ConnectionError("refused")
                    return boom()

                client = factory(connect, fake_sleep)
                await client.run()
                return delays
            return run(scenario())

        bridge = _delays(lambda connect, sleep: BridgeClient(
            mock_logger, {}, connect=connect, sleep=sleep))
        controller = _delays(lambda connect, sleep: MatterClient(
            Protocol(), mock_logger, {}, connect=connect, sleep=sleep))
        assert bridge == controller == [1, 2, 4, 8, 16, 30, 30, 30]

    def test_repeated_failure_diagnostic_fires_once_per_streak(self, mock_logger):
        async def scenario():
            calls = []
            attempts = {"n": 0}

            async def fake_sleep(_delay):
                if attempts["n"] >= 4:
                    await client.close()
                await asyncio.sleep(0)

            def connect(_uri):
                attempts["n"] += 1

                async def boom():
                    raise ConnectionError("refused")
                return boom()

            client = BridgeClient(mock_logger, {}, connect=connect, sleep=fake_sleep,
                                  on_repeated_failure=calls.append)
            await client.run()
            assert len(calls) == 1 and calls[0] >= 2
        run(scenario())


class TestUri:
    """Loopback only (§6.1), on the pref-configurable port (default 5581)."""

    def _uri(self, prefs):
        return BridgeClient(None, prefs, connect=lambda uri: None).uri

    def test_default_port(self):
        assert self._uri({}) == "ws://127.0.0.1:5581/"

    def test_configured_port(self):
        assert self._uri({"bridgeWsPort": "6000"}) == "ws://127.0.0.1:6000/"

    def test_blank_port_falls_back_to_the_default(self):
        # The controller client's field bug (blank pref → port 80) must not repeat.
        assert self._uri({"bridgeWsPort": ""}) == "ws://127.0.0.1:5581/"
        assert self._uri({"bridgeWsPort": " 5599 "}) == "ws://127.0.0.1:5599/"


def error_response(code: str, details: str = "because") -> dict:
    """An error frame body for ``golden_responder``'s override table (§1)."""
    return {"error_code": code, "details": details}


def logged(mock_logger, level: str) -> str:
    """Every argument of every call at ``level``, flattened for substring checks."""
    return " ".join(str(call) for call in getattr(mock_logger, level).call_args_list)


class TestAttachRefused:
    """§1.1 — the node can refuse an attach, and "retry in 1s" answers none of it.

    Before this, every refusal took the same path: the ``BridgeProtocolError``
    escaped the handshake, the run loop logged a bare traceback, and one second
    later the client attached again to be refused again — forever, for reasons a
    reconnect cannot change.
    """

    def _refuse(self, mock_logger, code, **kw):
        """Run a client against a node that refuses the handshake attach."""
        state = {}

        async def scenario():
            delays = []

            async def fake_sleep(delay):
                delays.append(delay)
                await client.close()
                await asyncio.sleep(0)

            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_ATTACH: error_response(code, f"{code} details")}))
            client = _client(mock_logger, fake, sleep=fake_sleep, **kw)
            task = asyncio.create_task(client.run())
            # A halting refusal returns from run(); a recovery one keeps running.
            for _ in range(200):
                if task.done() or client.recovery:
                    break
                await asyncio.sleep(0.005)
            state.update(client=client, fake=fake, delays=delays, task=task)
            if not task.done():
                await client.close()
                task.cancel()
        run(scenario())
        return state

    def test_mass_removal_refused_halts_with_its_own_reason(self, mock_logger):
        # §3.1's guard fired: the node still serves endpoints and we asked it to
        # serve none. Attaching again with the same set gets the same answer.
        refusals = []
        state = self._refuse(mock_logger, bridge_protocol.ERR_MASS_REMOVAL_REFUSED,
                             on_attach_refused=lambda code, details: refusals.append((code, details)))
        client = state["client"]

        assert client.halted
        assert client.halted_reason == bridge_protocol.ERR_MASS_REMOVAL_REFUSED
        assert not client.attached
        assert state["delays"] == [], "a refusal that will repeat must not be retried"
        assert refusals == [(bridge_protocol.ERR_MASS_REMOVAL_REFUSED,
                             f"{bridge_protocol.ERR_MASS_REMOVAL_REFUSED} details")]
        errors = logged(mock_logger, "error")
        assert bridge_protocol.ERR_MASS_REMOVAL_REFUSED in errors
        assert "allow-list" in errors, "the log must carry the remedy, not just the code"

    def test_version_mismatch_on_the_attach_also_halts(self, mock_logger):
        # Distinct from the hello-frame skew: the node accepted the connection
        # and rejected the attach args. Same conclusion, different route in.
        state = self._refuse(mock_logger, bridge_protocol.ERR_VERSION_MISMATCH)
        assert state["client"].halted
        assert state["client"].halted_reason == bridge_protocol.ERR_VERSION_MISMATCH
        assert state["delays"] == []
        assert "restart the bridge agent" in logged(mock_logger, "error")

    def test_a_transient_refusal_reconnects_and_names_the_code(self, mock_logger):
        # `internal` says the node fell over on its own; that genuinely may work
        # next time. What must not survive is the bare traceback it used to log.
        state = self._refuse(mock_logger, bridge_protocol.ERR_INTERNAL)
        assert not state["client"].halted
        assert state["delays"], "a transient refusal must be retried"
        warnings = logged(mock_logger, "warning")
        assert bridge_protocol.ERR_INTERNAL in warnings
        assert "handshake failed" in warnings
        assert not mock_logger.exception.called, "a known error_code is not a traceback"

    def test_malformed_args_is_treated_as_transient(self, mock_logger):
        state = self._refuse(mock_logger, bridge_protocol.ERR_MALFORMED_ARGS)
        assert not state["client"].halted
        assert state["delays"]


class TestEndpointMapInvalid:
    """§1.1 — the one refusal where dropping the socket destroys the way out.

    In this state the node serves nothing and accepts exactly ``get_status``,
    ``get_pairing`` and ``rebuild_endpoint_map``. That rebuild is PRD §7's
    user-confirmed recovery, so the connection is held open, un-attached.
    """

    def _recovering(self, mock_logger, overrides=None):
        table = {bridge_protocol.CMD_ATTACH: error_response(
            bridge_protocol.ERR_ENDPOINT_MAP_INVALID,
            PENDING["endpoint_map_invalid"]["response"]["details"])}
        table.update(overrides or {})
        fake = _fake(responder=golden_responder(table))
        return fake, _client(mock_logger, fake)

    def test_the_connection_is_kept_open_un_attached(self, mock_logger):
        async def scenario():
            refusals = []
            fake, client = self._recovering(mock_logger)
            client._on_attach_refused = lambda code, details: refusals.append(code)
            task = asyncio.create_task(client.run())
            await settle(lambda: client.recovery)

            assert client.connected, "the recovery commands need this socket"
            assert not client.attached
            assert client.recovery
            assert not client.halted
            assert refusals == [bridge_protocol.ERR_ENDPOINT_MAP_INVALID]
            assert "confirm the rebuild" in logged(mock_logger, "error")

            await client.close()
            task.cancel()
        run(scenario())

    def test_the_three_recovery_commands_still_work(self, mock_logger):
        # §1.1 names exactly these three. If the client cannot reach them, the
        # refuse-to-start state is a dead end rather than a recoverable one.
        async def scenario():
            fake, client = self._recovering(mock_logger)
            task = asyncio.create_task(client.run())
            await settle(lambda: client.recovery)

            assert (await client.get_status()).endpoint_count == 1
            assert (await client.get_pairing()).commissioned is True

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_successful_rebuild_re_attaches(self, mock_logger):
        # The map is valid again, so the connection stops being a three-command
        # lifeline: without this the export stays dark until the next reconnect.
        async def scenario():
            attached = []
            # Once the map is rebuilt the node accepts the attach it refused.
            answers = {bridge_protocol.CMD_ATTACH: error_response(
                bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "map unreadable")}

            def responder(frame):
                command = frame["command"]
                if command == bridge_protocol.CMD_REBUILD_ENDPOINT_MAP:
                    answers[bridge_protocol.CMD_ATTACH] = FRAMES["attach"]["response"]
                body = {**RESPONSES, **answers}.get(command)
                return [{**body, "message_id": frame["message_id"]}]

            fake = _fake(responder=responder)
            client = _client(mock_logger, fake, on_attached=attached.append)
            task = asyncio.create_task(client.run())
            await settle(lambda: client.recovery)
            assert not client.attached

            status = await client.rebuild_endpoint_map()

            assert client.attached, "the client must re-attach once the map is rebuilt"
            assert not client.recovery
            assert attached and attached[-1] is status
            assert bridge_protocol.CMD_ATTACH in fake.sent_commands()

            await client.close()
            task.cancel()
        run(scenario())

    def test_state_pushes_are_dropped_while_un_attached(self, mock_logger):
        # `connected` is true here, so a transport-level check would have posted
        # state into a node that is serving no endpoints at all.
        async def scenario():
            fake, client = self._recovering(mock_logger)
            task = asyncio.create_task(client.run())
            await settle(lambda: client.recovery)

            await client.set_state(123456789, {"onOff": True})
            assert bridge_protocol.CMD_SET_STATE not in fake.sent_commands()

            await client.close()
            task.cancel()
        run(scenario())


class TestSetStateFailurePaths:
    """§3.4 — a push must never raise on Indigo's device thread, and must never lie."""

    def test_a_send_that_raises_is_dropped_not_propagated(self, mock_logger):
        # The listen loop has not noticed the socket died, so `connected` is
        # still True and the guard passes — then send() raises straight into
        # whatever Indigo callback triggered the state change.
        class DyingSocket(FakeWebSocket):
            async def send(self, raw: str) -> None:
                import json as _json
                if _json.loads(raw).get("command") == bridge_protocol.CMD_SET_STATE:
                    raise ConnectionResetError("broken pipe")
                await super().send(raw)

        async def scenario():
            fake = DyingSocket(responder=golden_responder(), handshake=HELLO)
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            mock_logger.debug.reset_mock()

            await client.set_state(123456789, {"onOff": True})   # must not raise

            assert "send failed" in logged(mock_logger, "debug")
            assert "123456789" in logged(mock_logger, "debug")

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_push_while_halted_warns_that_nothing_is_being_delivered(self, mock_logger):
        # "attach will reconcile" is true while reconnecting and false while
        # halted — there is no reconnect coming. Logging it at debug meant the
        # one state where the export is silently dead was the quietest of all.
        async def scenario():
            client = _client(mock_logger, _fake())
            client.halted = True
            client.halted_reason = "version_skew"

            await client.set_state(123456789, {"onOff": True})

            warnings = logged(mock_logger, "warning")
            assert "halted" in warnings
            assert "NOT being delivered" in warnings
            assert "version_skew" in warnings
        run(scenario())

    def test_an_unmatched_error_names_the_device(self, mock_logger):
        # §3.4 responses are not awaited, so the warning is all anyone gets. A
        # bare message_id is not something a user can act on.
        async def scenario():
            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_SET_STATE: PENDING["set_state_unknown_device"]["response"]}))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            await client.set_state(123456791, {"onOff": True})
            await settle(lambda: mock_logger.warning.called)

            warnings = logged(mock_logger, "warning")
            assert "set_state dev 123456791" in warnings, warnings
            assert bridge_protocol.ERR_UNKNOWN_DEVICE in warnings
            # Matched or logged, the note is consumed either way.
            assert client._send_context == {}

            await client.close()
            task.cancel()
        run(scenario())


class TestHandshakeInterleaving:
    """The attach is pumped inline — everything else on the wire must survive it."""

    def test_an_event_arriving_during_the_handshake_is_not_lost(self, mock_logger):
        # The node may emit before it answers: it has an attached client the
        # instant it processes the attach. The inline pump has to dispatch those
        # normally, which means on_command CAN fire before on_attached.
        async def scenario():
            order = []

            def responder(frame):
                if frame["command"] != bridge_protocol.CMD_ATTACH:
                    return golden_responder()(frame)
                return [
                    {"event": bridge_protocol.EVT_COMMAND,
                     "data": FRAMES["command_on_off"]["data"]},
                    {**FRAMES["attach"]["response"], "message_id": frame["message_id"]},
                ]

            fake = _fake(responder=responder)
            client = _client(mock_logger, fake,
                             on_command=lambda cmd: order.append(("command", cmd.command)),
                             on_attached=lambda _s: order.append(("attached", None)))
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            await settle(lambda: len(order) == 2)

            assert order == [("command", "onOff"), ("attached", None)], order
            assert client.attached
            assert client.last_event_ts is not None

            await client.close()
            task.cancel()
        run(scenario())


class TestHaltAndResume:
    """The full skew → halt → fix → resume → attach cycle, behaviourally."""

    def test_a_resumed_client_attaches_to_a_fixed_node(self, mock_logger):
        async def scenario():
            sockets = [_fake(handshake=SKEWED_HELLO), _fake()]
            index = {"i": 0}

            def connect(_uri):
                i = min(index["i"], len(sockets) - 1)
                index["i"] += 1
                return returns(sockets[i])

            async def fake_sleep(_delay):
                await asyncio.sleep(0)

            attached = []
            client = _client(mock_logger, sockets[0], connect=connect, sleep=fake_sleep,
                             on_attached=attached.append)

            await asyncio.wait_for(client.run(), timeout=2)
            assert client.halted and client.halted_reason == "version_skew"
            assert not attached

            client.resume()                       # the agent was restarted
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            await settle(lambda: attached)

            assert client.attached
            assert attached, "the fixed node must actually be attached to"
            assert bridge_protocol.CMD_ATTACH in sockets[1].sent_commands()

            await client.close()
            task.cancel()
        run(scenario())


class TestReattach:
    """§3.1 on a live connection is a refresh, not just a frame on the wire."""

    def test_attach_on_a_live_connection_updates_the_status(self, mock_logger):
        async def scenario():
            answers = {bridge_protocol.CMD_ATTACH: FRAMES["attach"]["response"]}

            def responder(frame):
                body = {**RESPONSES, **answers}.get(frame["command"])
                return [{**body, "message_id": frame["message_id"]}]

            fake = _fake(responder=responder)
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            assert client.status.endpoint_count == 0

            # The node now serves the two-endpoint set.
            answers[bridge_protocol.CMD_ATTACH] = PENDING["attach_with_endpoints"]["response"]
            refreshed = await client.attach([KITCHEN_LAMP])

            assert client.status is refreshed
            assert refreshed.endpoint_count == 2
            assert [ep.endpoint_number for ep in client.status.endpoints] == [2, 3]

            await client.close()
            task.cancel()
        run(scenario())


class TestEventDiagnostics:
    """§5 — a callback that blows up must say WHICH event blew it up."""

    def test_the_failure_log_names_the_event_and_its_data(self, mock_logger):
        def boom(_command):
            raise RuntimeError("handler exploded")

        async def scenario():
            fake = _fake()
            client = _client(mock_logger, fake, on_command=boom)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)
            await fake.push_event(bridge_protocol.EVT_COMMAND, FRAMES["command_on_off"]["data"])
            await settle(lambda: mock_logger.exception.called)

            reported = logged(mock_logger, "exception")
            assert bridge_protocol.EVT_COMMAND in reported
            assert "123456789" in reported, "the payload is the lead; log it"
            assert client.connected

            await client.close()
            task.cancel()
        run(scenario())


class TestResultValidation:
    """A missing field in a result is a protocol error, never a plausible default."""

    def test_upsert_endpoint_without_an_endpoint_number_raises(self, mock_logger):
        # Endpoint numbers ARE the exported accessory's identity (§6.3). A
        # defaulted 0 would be fed straight to the drift detector that exists to
        # catch exactly this kind of divergence.
        async def scenario():
            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_UPSERT_ENDPOINT: {"result": {}}}))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            with pytest.raises(bridge_protocol.BridgeProtocolError):
                await client.upsert_endpoint(KITCHEN_LAMP)

            await client.close()
            task.cancel()
        run(scenario())

    def test_a_window_without_pairing_codes_raises(self, mock_logger):
        # An empty code is handed to the user as the thing to type into their
        # ecosystem. A window that opened with no usable code is worse than one
        # that failed to open.
        async def scenario():
            fake = _fake(responder=golden_responder(
                {bridge_protocol.CMD_OPEN_WINDOW:
                    {"result": {"windowExpiresAt": "2026-08-04T12:15:00.000Z"}}}))
            client = _client(mock_logger, fake)
            task = asyncio.create_task(client.run())
            await client.wait_connected(timeout=2)

            with pytest.raises(bridge_protocol.BridgeProtocolError) as excinfo:
                await client.open_commissioning_window()
            assert "manualPairingCode" in str(excinfo.value)

            await client.close()
            task.cancel()
        run(scenario())
