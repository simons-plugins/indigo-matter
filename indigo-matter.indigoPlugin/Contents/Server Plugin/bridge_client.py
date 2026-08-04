"""Persistent WebSocket client to the Matter **bridge node** (the export side).

The outbound twin of :mod:`matter_client`: same transport (:class:`ws_json_client.WsJsonClient`),
different peer. Wire shapes come from :mod:`bridge_protocol`; the contract is
`docs/BRIDGE_PROTOCOL.md`, and a bare ``§N`` below refers to it.

Two things differ from the controller client and both are deliberate:

* **The handshake fails closed on version skew (§2).** launchd keeps the old
  bridge node running across plugin reloads (PRD §4.2 PM-B), so plugin/node
  skew is the failure that will actually happen. On a mismatch we do NOT attach,
  we surface ``on_version_skew`` and stop reconnecting — pairings untouched,
  user told to restart the agent.
* **``set_state`` is fire-and-forget (§3.4).** It is called from Indigo's device
  thread and must never block it. The response still arrives and an error is
  logged by the shared unmatched-response path.

A successful ``attach`` carries the full desired endpoint set, so every
(re)connect is a complete reconcile (PRD §5.4 ``startup``) — the endpoint list
is pulled fresh from the injected provider each time, never cached here.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

import bridge_protocol
from bridge_protocol import (
    BridgeProtocol,
    BridgeProtocolError,
    CommissioningWindow,
    PairingReport,
    StatusReport,
)
from ws_json_client import WS_ERRORS, ClientHalted, HandshakeFailed, WsJsonClient, pref_str

#: What a send can raise when the socket died between the listen loop's last
#: read and this write: the stdlib connection errors plus, when the real library
#: is present, ``websockets``' own ConnectionClosed family.
_SEND_ERRORS = WS_ERRORS

#: Default deadline for a bridge RPC. The node's handlers are local and quick.
DEFAULT_TIMEOUT = 10.0

#: Longer deadline for the commissioning/reset RPCs: opening an enhanced window
#: derives a fresh passcode (crypto), and a factory reset rebuilds the node.
LONG_TIMEOUT = 30.0

#: The attach must complete inside the node's 10s unattached timeout (§2).
ATTACH_TIMEOUT = 8.0

#: Attach refusals that reconnecting cannot fix, with the remedy the user needs.
#: Everything NOT listed here (``internal``, ``malformed_args``, …) is treated as
#: transient and retried on the normal backoff — retrying these three instead
#: produces an endless connect/attach/refuse loop that also hammers the node.
TERMINAL_ATTACH_ERRORS = {
    bridge_protocol.ERR_VERSION_MISMATCH: (
        "the node speaks a different protocol version — restart the bridge agent so launchd "
        "picks up the node that ships with this plugin"
    ),
    bridge_protocol.ERR_MASS_REMOVAL_REFUSED: (
        "attaching would have un-exported every accessory and the request did not carry the "
        "explicit opt-in (§3.1) — check the export allow-list; nothing was removed"
    ),
    bridge_protocol.ERR_ENDPOINT_MAP_INVALID: (
        "the node's endpoint-number map is unreadable, so it is serving nothing until it is "
        "rebuilt — confirm the rebuild in the plugin (PRD §7); it WILL duplicate accessories "
        "in already-paired ecosystems"
    ),
}

#: The subset of :data:`TERMINAL_ATTACH_ERRORS` that halts the client outright.
#: ``endpoint_map_invalid`` is the exception: §1.1 keeps the connection usable
#: for exactly ``get_status``/``get_pairing``/``rebuild_endpoint_map``, and that
#: is the only route back, so we hold the socket open un-attached instead.
HALTING_ATTACH_ERRORS = frozenset({
    bridge_protocol.ERR_VERSION_MISMATCH,
    bridge_protocol.ERR_MASS_REMOVAL_REFUSED,
})


class BridgeClient(WsJsonClient):
    """A reconnecting client for the plugin ⇄ bridge-node protocol."""

    PEER = "bridge node"

    # The callbacks ARE the API of this class — the plugin injects every seam it
    # needs, exactly as MatterClient does. Grouping them into a struct would move
    # the arity, not remove it.
    # pylint: disable=too-many-arguments,too-many-locals
    def __init__(
        self,
        logger: Any,
        prefs: dict,
        *,
        plugin_version: str = "unknown",
        endpoint_provider: Optional[Callable[[], list]] = None,
        connect: Optional[Callable[[str], Awaitable]] = None,
        on_attached: Optional[Callable[[StatusReport], None]] = None,
        on_attach_refused: Optional[Callable[[str, str], None]] = None,
        on_version_skew: Optional[Callable[[bridge_protocol.Hello], None]] = None,
        on_command: Optional[Callable[[bridge_protocol.BridgeCommand], None]] = None,
        on_fabrics_changed: Optional[Callable[[list, str], None]] = None,
        on_commissioned: Optional[Callable[[], None]] = None,
        on_decommissioned: Optional[Callable[[], None]] = None,
        on_window_closed: Optional[Callable[[str], None]] = None,
        on_drift_detected: Optional[Callable[[list], None]] = None,
        on_connect: Optional[Callable[[], Awaitable]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_repeated_failure: Optional[Callable[[int], None]] = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ) -> None:
        self.proto = BridgeProtocol()
        port = pref_str(prefs, bridge_protocol.PREF_WS_PORT, bridge_protocol.DEFAULT_WS_PORT)
        # Loopback only (§6.1): there is no auth on this socket because there is
        # no remote surface, so there had better be no remote surface.
        super().__init__(
            f"ws://127.0.0.1:{port}/", logger,
            connect=connect, on_connect=on_connect, on_disconnect=on_disconnect,
            on_repeated_failure=on_repeated_failure, now=now, sleep=sleep,
        )
        self.plugin_version = plugin_version
        self._endpoint_provider = endpoint_provider or (lambda: [])
        self._on_attached = on_attached
        self._on_attach_refused = on_attach_refused
        self._on_version_skew = on_version_skew
        self._on_command = on_command
        self._on_fabrics_changed = on_fabrics_changed
        self._on_commissioned = on_commissioned
        self._on_decommissioned = on_decommissioned
        self._on_window_closed = on_window_closed
        self._on_drift_detected = on_drift_detected

        #: Populated by the handshake; what the node told us it is (§2).
        self.hello: Optional[bridge_protocol.Hello] = None
        #: The last StatusReport the node returned (attach / get_status).
        self.status: Optional[StatusReport] = None
        self._attached = False
        #: True while the node refused the attach with ``endpoint_map_invalid``
        #: and we are holding the connection open for the §1.1 recovery trio.
        self.recovery = False

    @property
    def attached(self) -> bool:
        """True only after an ``attach`` the node accepted (§3.1).

        Distinct from :attr:`connected`, which is transport-level: an
        ``endpoint_map_invalid`` refusal leaves a live socket that accepts
        exactly three commands, and treating that as "attached" would let state
        pushes disappear into a node that is serving nothing.
        """
        return self._attached and self.connected

    def _mark_disconnected(self) -> None:
        self._attached = False
        self.recovery = False
        super()._mark_disconnected()

    # ------------------------------------------------------------------
    # Handshake (§2) + frame vocabulary (the WsJsonClient hooks)
    # ------------------------------------------------------------------
    async def _handshake(self, first: Any) -> None:
        hello = bridge_protocol.parse_hello(first)
        self.hello = hello
        if hello.protocol_version != bridge_protocol.PROTOCOL_VERSION:
            # §2: skew fails closed — no attach, no retry loop, pairings untouched.
            self._notify(self._on_version_skew, hello)
            raise ClientHalted(
                f"node speaks protocol version {hello.protocol_version}, plugin speaks "
                f"{bridge_protocol.PROTOCOL_VERSION} (bridge {hello.bridge_version}); "
                "restart the bridge agent to pick up the matching node",
                reason="version_skew",
            )
        self.logger.info(
            "connected to bridge node (bridge %s, matter.js %s), attaching",
            hello.bridge_version, hello.matter_js_version,
        )
        # Requests are legal from here on: attach is one.
        self._mark_connected()
        try:
            status = await self._attach(None, replace_all=False, timeout=ATTACH_TIMEOUT, inline=True)
        except BridgeProtocolError as exc:
            self._handle_attach_refused(exc)
            return
        self._attached = True
        self.recovery = False
        self._notify(self._on_attached, status)

    def _handle_attach_refused(self, exc: BridgeProtocolError) -> None:
        """Decide what an ``attach`` the node refused means (§1.1).

        Three outcomes, because "retry in a second" is the wrong answer to all
        of the interesting refusals:

        * ``version_mismatch``/``mass_removal_refused`` — halt. The node has
          made a decision it will make again; only a human changes the outcome.
        * ``endpoint_map_invalid`` — hold the connection OPEN, un-attached. §1.1
          says the node still answers ``get_status``/``get_pairing``/
          ``rebuild_endpoint_map`` in this state, and that rebuild is the user-
          confirmed way out (PRD §7). Dropping the socket would throw away the
          only route to the fix.
        * anything else — genuinely might be transient; reconnect with backoff,
          but log *which* error it was rather than dumping a traceback.
        """
        code = str(exc.code)
        remedy = TERMINAL_ATTACH_ERRORS.get(code)
        if remedy is None:
            self._notify(self._on_attach_refused, code, exc.details)
            raise HandshakeFailed(f"bridge node refused attach: {code} ({exc.details})")
        self.logger.error("bridge node refused attach (%s): %s. %s", code, exc.details, remedy)
        self._notify(self._on_attach_refused, code, exc.details)
        if code in HALTING_ATTACH_ERRORS:
            raise ClientHalted(f"attach refused: {code} ({exc.details})", reason=code)
        # endpoint_map_invalid: stay connected, un-attached, in recovery.
        self.recovery = True

    def _is_event(self, frame: dict) -> bool:
        return self.proto.is_event(frame)

    def _is_response(self, frame: dict) -> bool:
        return self.proto.is_response(frame)

    def _message_id_of(self, frame: dict) -> Optional[str]:
        return self.proto.message_id_of(frame)

    def _parse_result(self, frame: dict) -> Any:
        return self.proto.parse_result(frame)

    def _error_of(self, frame: dict) -> Optional[tuple]:
        return self.proto.error_of(frame)

    # ------------------------------------------------------------------
    # Events (§5)
    # ------------------------------------------------------------------
    def _handle_event(self, frame: dict) -> None:
        name = self.proto.event_name(frame)
        data = self.proto.event_data(frame)
        handlers = {
            bridge_protocol.EVT_COMMAND: self._dispatch_command,
            bridge_protocol.EVT_FABRICS_CHANGED: self._dispatch_fabrics_changed,
            bridge_protocol.EVT_COMMISSIONED: lambda _data: self._deliver(self._on_commissioned),
            bridge_protocol.EVT_DECOMMISSIONED: lambda _data: self._deliver(self._on_decommissioned),
            bridge_protocol.EVT_WINDOW_CLOSED: self._dispatch_window_closed,
            bridge_protocol.EVT_DRIFT_DETECTED: self._dispatch_drift,
        }
        handler = handlers.get(name)
        if handler is None:
            # §1: unknown events are logged and dropped by the plugin.
            self.logger.warning("bridge node sent an unknown event %r; dropped", name)
            return
        try:
            handler(data)
        except Exception as exc:  # a bad frame must not kill the loop
            # Name the event and its payload: a bare traceback out of a callback
            # tells you a dict was wrong somewhere, which is not a lead.
            self.logger.exception("bridge node event %s failed on %r: %s", name, data, exc)

    def _dispatch_command(self, data: dict) -> None:
        self._deliver(self._on_command, bridge_protocol.parse_command(data))

    def _dispatch_fabrics_changed(self, data: dict) -> None:
        self._deliver(
            self._on_fabrics_changed,
            bridge_protocol.parse_fabrics(data.get("fabrics")),
            str(data.get("change", "")),
        )

    def _dispatch_window_closed(self, data: dict) -> None:
        self._deliver(self._on_window_closed, str(data.get("reason", "")))

    def _dispatch_drift(self, data: dict) -> None:
        self._deliver(self._on_drift_detected, bridge_protocol.parse_drift(data.get("drift")))

    @staticmethod
    def _deliver(callback: Optional[Callable], *args: Any) -> None:
        """Call an event callback and let it raise.

        The event path deliberately does NOT swallow here: :meth:`_handle_event`
        is the one place that still knows which event and which payload were in
        flight, and that is the only version of the log line worth having.
        """
        if callback is not None:
            callback(*args)

    def _notify(self, callback: Optional[Callable], *args: Any) -> None:
        """Call an injected non-event callback, never letting it kill the run loop.

        Used for the handshake-side callbacks (``on_attached``, ``on_version_skew``,
        ``on_attach_refused``), where there is no outer frame handler to contain a
        failure and a raising callback would abort the connection.
        """
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:  # callbacks must not kill the run loop
            self.logger.exception("bridge node callback %r failed: %s",
                                  getattr(callback, "__name__", callback), exc)

    # ------------------------------------------------------------------
    # Commands (§3)
    # ------------------------------------------------------------------
    async def attach(self, endpoints: Optional[list] = None, *, replace_all: bool = False,
                     timeout: float = ATTACH_TIMEOUT) -> StatusReport:
        """Declare this client and deliver the full desired endpoint set (§3.1).

        Called by the handshake on every (re)connect, which is what makes a fresh
        connection a full reconcile (PRD §5.4). ``endpoints`` defaults to a fresh
        read of the injected provider — never a cached list, since the allow-list
        may have changed while the socket was down.

        ``replace_all`` is the §3.1 opt-in required to empty a non-empty live set;
        the plugin passes it only on the deliberate allow-list-emptied path
        (PRD §7), so a stale client can never un-export everything by default.
        """
        return await self._attach(endpoints, replace_all=replace_all, timeout=timeout, inline=False)

    async def _attach(self, endpoints: Optional[list], *, replace_all: bool,
                      timeout: float, inline: bool) -> StatusReport:
        """The attach itself. ``inline`` selects how the response is waited for.

        The handshake issues its attach before the run loop's listen loop owns
        the socket, so there is no dispatcher to resolve a pending future — it
        pumps the socket itself. A caller re-attaching on a live connection goes
        through the normal correlated path.
        """
        specs = self._endpoint_provider() if endpoints is None else endpoints
        frame = self.proto.build_attach(self.plugin_version, specs, replace_all=replace_all)
        if inline:
            result = await self._handshake_request(frame, timeout, bridge_protocol.CMD_ATTACH)
        else:
            result = await self._request_frame(frame, timeout)
            self._attached = True
            self.recovery = False
        self.status = bridge_protocol.parse_status(result)
        return self.status

    async def upsert_endpoint(self, spec: Any, timeout: float = DEFAULT_TIMEOUT) -> int:
        """Create or update one endpoint (§3.2). Returns its endpoint number.

        A result without ``endpointNumber`` is a protocol error, not a 0: endpoint
        numbers ARE the exported accessory's identity (§6.3), and quietly
        substituting one would feed a fiction straight into the drift machinery
        that exists to notice exactly this kind of divergence.
        """
        result = await self._request_frame(self.proto.build_upsert_endpoint(spec), timeout)
        data = result if isinstance(result, dict) else {}
        if "endpointNumber" not in data:
            raise BridgeProtocolError(
                bridge_protocol.ERR_MALFORMED_ARGS,
                f"upsert_endpoint result carried no endpointNumber: {result!r}")
        return int(data["endpointNumber"])

    async def remove_endpoint(self, indigo_device_id: int, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """Remove one endpoint (§3.3). ``False`` means it was already absent."""
        result = await self._request_frame(self.proto.build_remove_endpoint(indigo_device_id), timeout)
        return bool((result or {}).get("removed", False))

    async def set_state(self, indigo_device_id: int, states: dict) -> None:
        """Push Indigo-originated state outward — FIRE AND FORGET (§3.4).

        Deliberately does not await the response. This is called from Indigo's
        device thread via the runtime bridge, and a state push that waited on the
        node would make every device update as slow as the slowest Matter write —
        the one thing PRD §XG2's 500ms budget cannot afford.

        The node still answers, and the shared unmatched-response path logs any
        error at warning level (§3.4): an unnoticed failure here looks exactly
        like "the ecosystem shows stale state", so it must never be silent.

        A push while the socket is down is dropped rather than raised: the node
        caches nothing across reconnects and ``attach`` re-delivers the full
        state (§6.2), so there is nothing to recover. A push while the client is
        HALTED is a different story — nothing is coming to reconcile it — so that
        one is a warning that says so.
        """
        if self._ws is None or not self.attached:
            self._log_dropped_state_push(indigo_device_id)
            return
        frame = self.proto.build_set_state(indigo_device_id, states)
        self._remember_send_context(self.proto.message_id_of(frame),
                                    f"set_state dev {indigo_device_id}")
        try:
            await self._send_frame(frame)
        except _SEND_ERRORS as exc:
            # The listen loop has not noticed the socket is gone yet, so
            # ``connected`` is still True and the check above passed. Dropping is
            # still right (attach re-delivers), but it must not surface as an
            # exception on Indigo's device thread.
            self.logger.debug("bridge node send failed (%s); dropping set_state for %s",
                              exc, indigo_device_id)

    def _log_dropped_state_push(self, indigo_device_id: int) -> None:
        """Say what a dropped state push actually means right now.

        While halted there is no reconnect coming and no attach to reconcile
        anything, so "attach will reconcile" would be a promise we are not going
        to keep — the ecosystem will show stale state until someone acts.
        """
        if self.halted:
            self.logger.warning(
                "bridge client halted (%s); state pushes are NOT being delivered — "
                "dropping set_state for %s and every push until the bridge is fixed and resumed",
                self.halted_reason or "no reason recorded", indigo_device_id,
            )
            return
        self.logger.debug(
            "bridge node not attached; dropping set_state for %s (attach will reconcile)",
            indigo_device_id,
        )

    async def set_reachable(self, indigo_device_id: int, reachable: bool,
                            timeout: float = DEFAULT_TIMEOUT) -> None:
        """Set Bridged Device Basic Information ``Reachable`` (§3.5)."""
        await self._request_frame(self.proto.build_set_reachable(indigo_device_id, reachable), timeout)

    async def get_status(self, timeout: float = DEFAULT_TIMEOUT) -> StatusReport:
        """Read the live endpoint/fabric/drift report (§3.6)."""
        result = await self._request_frame(self.proto.build_get_status(), timeout)
        self.status = bridge_protocol.parse_status(result)
        return self.status

    async def get_pairing(self, timeout: float = DEFAULT_TIMEOUT) -> PairingReport:
        """Read pairing state and the current codes, if any (§3.7)."""
        result = await self._request_frame(self.proto.build_get_pairing(), timeout)
        return bridge_protocol.parse_pairing(result)

    async def open_commissioning_window(
        self,
        duration_seconds: int = bridge_protocol.DEFAULT_WINDOW_SECONDS,
        timeout: float = LONG_TIMEOUT,
    ) -> CommissioningWindow:
        """Open an enhanced commissioning window for another ecosystem (§3.8)."""
        result = await self._request_frame(self.proto.build_open_window(duration_seconds), timeout)
        return bridge_protocol.parse_window(result)

    async def remove_fabric(self, fabric_index: int, timeout: float = LONG_TIMEOUT) -> None:
        """Drop one ecosystem's fabric (§3.9)."""
        await self._request_frame(self.proto.build_remove_fabric(fabric_index), timeout)

    async def factory_reset(self, preserve_endpoint_numbers: bool = True,
                            timeout: float = LONG_TIMEOUT) -> None:
        """Wipe commissioning credentials and re-advertise (§3.10).

        Endpoint numbers are preserved by default so re-pairing the same
        ecosystems restores the same accessories; passing ``False`` is the
        explicit "the map itself is corrupt" path.
        """
        await self._request_frame(
            self.proto.build_factory_reset(preserve_endpoint_numbers), timeout)

    async def rebuild_endpoint_map(self, timeout: float = LONG_TIMEOUT) -> StatusReport:
        """Reallocate endpoint numbers from scratch (§3.11).

        The recovery path out of ``endpoint_map_invalid``. This DUPLICATES
        accessories in paired ecosystems, which is why it is a separate command
        the plugin only issues after the user confirms a warning dialog.

        When it is called from the recovery state (the node refused our attach)
        it also re-attaches: the map is valid again, so the connection can stop
        being a three-command lifeline and go back to being an export.
        """
        result = await self._request_frame(self.proto.build_rebuild_endpoint_map(), timeout)
        self.status = bridge_protocol.parse_status(result)
        if self.recovery:
            self.logger.info("endpoint map rebuilt; re-attaching to the bridge node")
            status = await self.attach()
            self._notify(self._on_attached, status)
            return status
        return self.status
