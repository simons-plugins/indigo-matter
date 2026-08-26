"""Persistent WebSocket client to the Matter **bridge node** (the export side).

The outbound twin of :mod:`matter_client`: same transport (:class:`ws_json_client.WsJsonClient`),
different peer. Wire shapes come from :mod:`bridge_protocol`; the contract is
`docs/BRIDGE_PROTOCOL.md`, and a bare ``§N`` below refers to it.

Two things differ from the controller client and both are deliberate:

* **The handshake fails closed on version skew (§2).** launchd keeps the old
  bridge node running across plugin reloads (PRD §4.2 PM-B), so plugin/node
  skew is the failure that will actually happen. On a mismatch we do NOT attach,
  we surface ``on_version_skew`` and stop reconnecting — pairings untouched,
  user told what this plugin needs: the paired bridge-node release, installed
  from *Plugins ▸ Matter ▸ Install/update the Matter bridge*. Restarting the
  agent is deliberately NOT the remedy — launchd relaunches the same node.
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

#: Floor for the attach deadline. The node's own 10s timer (§2) bounds how long
#: we may take to *send* an attach, not how long it may take to answer one, so
#: this deadline is free to exceed it — and has to (see below).
ATTACH_TIMEOUT = 8.0
#: Fixed cost of an attach: one round trip plus the node's own bookkeeping.
ATTACH_TIMEOUT_BASE = 2.0
#: Marginal cost per endpoint in the desired set. An ``attach`` reconciles by
#: removing what is no longer wanted before it answers, and the node paces bulk
#: removals ~100ms apart (§3.3) — so the answer to a large attach arrives on the
#: far side of ``0.1s × removals`` (E3a, measured). 0.15 leaves headroom for the
#: creates in the same reconcile without making a hung node cost minutes.
ATTACH_TIMEOUT_PER_ENDPOINT = 0.15


def attach_timeout_for(endpoint_count: int) -> float:
    """The deadline for one ``attach`` carrying ``endpoint_count`` endpoints.

    THE formula (E3a decision, zero protocol change): ~80 endpoints lands just
    over 8s of pacing alone, so a fixed deadline would time out exactly on the
    databases that most need export to work. Everything smaller keeps the flat
    :data:`ATTACH_TIMEOUT` floor, so the common case is unchanged.

    **The count is asymmetric, and callers have to know which one they mean.**
    What the node spends its time on is the ~100ms-paced REMOVALS (§3.3), not
    the creates — and an attach's removals are everything it holds that the
    desired set omits. For an ordinary reconnect the two counts track each
    other: the node's set came from our last attach, so drift is bounded by
    whatever the user changed while the socket was down, and ``len(specs)`` is
    a fine proxy. For the ONE caller that deliberately sends nothing —
    ``export_bridge._replace_all_then_stop``, the §3.1 un-export — they are
    opposites: zero sent, *everything* removed. That caller passes its own
    ``timeout`` over the removal count; defaulting would hand a 60-device
    un-export the 8s floor and time it out mid-reconcile.
    """
    return max(ATTACH_TIMEOUT,
               ATTACH_TIMEOUT_BASE + ATTACH_TIMEOUT_PER_ENDPOINT * max(0, int(endpoint_count)))


#: Slack on top of the work a §3.11 rebuild can actually do, so the *caller's*
#: deadline is never the tighter of the two.
REBUILD_TIMEOUT_HEADROOM = 5.0


def rebuild_timeout_for(endpoint_count: int) -> float:
    """Wall-clock ceiling for one §3.11 rebuild **including its re-attach**.

    A caller that waits on ``rebuild_endpoint_map`` is waiting on two deadlines
    in series — :data:`LONG_TIMEOUT` for the rebuild itself, then a full
    :func:`attach_timeout_for` for the re-attach — and a flat number chosen
    without reference to either is a trap the moment the export list is large:
    the menu's own 45s expired mid-re-attach on ~90 endpoints and reported a
    failure over a rebuild that had already succeeded and could not be undone.
    """
    return LONG_TIMEOUT + attach_timeout_for(endpoint_count) + REBUILD_TIMEOUT_HEADROOM

#: Attach refusals that reconnecting cannot fix, with the remedy the user needs.
#: Everything NOT listed here (``internal``, ``malformed_args``, …) is treated as
#: transient and retried on the normal backoff — retrying these three instead
#: produces an endless connect/attach/refuse loop that also hammers the node.
TERMINAL_ATTACH_ERRORS = {
    bridge_protocol.ERR_VERSION_MISMATCH: (
        "the node speaks a different protocol version — this plugin speaks bridge protocol "
        f"v{bridge_protocol.PROTOCOL_VERSION} and needs the paired bridge-node release; install "
        "it with Plugins ▸ Matter ▸ Install/update the Matter bridge once it is available. "
        "Restarting the agent alone relaunches the same node"
    ),
    bridge_protocol.ERR_MASS_REMOVAL_REFUSED: (
        "attaching would have un-exported every accessory and the request did not carry the "
        "explicit opt-in (§3.1) — check the export allow-list; nothing was removed"
    ),
    bridge_protocol.ERR_ENDPOINT_MAP_INVALID: (
        "the node's endpoint-number map is unreadable, so it is serving nothing until it is "
        "rebuilt — confirm the rebuild in the plugin (PRD §7); it adopts the numbers that "
        "exist now and renumbers nothing"
    ),
}

#: The remedy when an ``endpoint_map_invalid`` refusal is really about the
#: IDENTITY file. §1.1 has one code for every refuse-to-start reason, and the
#: reason text is the only thing that distinguishes them — so the map remedy
#: above is not merely unhelpful here, it points at the §3.11 rebuild, which the
#: node refuses outright for this reason (it cannot fix an identity, and
#: clearing the refusal would serve endpoints under a serial nobody has seen).
IDENTITY_REFUSAL_REMEDY = (
    "the node's identity file is unreadable, so it is serving nothing — a rebuild CANNOT fix "
    "this and the node will refuse one. The unusable file was moved aside as "
    "identity.json.unreadable-<timestamp>; restore or repair it and restart the bridge node"
)


def remedy_for(code: str, details: str) -> Optional[str]:
    """The user-facing remedy for a terminal refusal, or ``None`` if untriaged."""
    if (code == bridge_protocol.ERR_ENDPOINT_MAP_INVALID
            and bridge_protocol.REFUSE_IDENTITY_UNREADABLE in (details or "")):
        return IDENTITY_REFUSAL_REMEDY
    return TERMINAL_ATTACH_ERRORS.get(code)

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
        replace_all_provider: Optional[Callable[[], int]] = None,
        export_count_provider: Optional[Callable[[], int]] = None,
        connect: Optional[Callable[[str], Awaitable]] = None,
        on_attached: Optional[Callable[[StatusReport, bool], None]] = None,
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
        #: How many endpoints an un-export still owes the node — 0 for none.
        #: Non-zero makes the *handshake's* attach carry §3.1's `replace_all`
        #: and sizes its deadline (``export_bridge._pending_replace_all``, XAC7).
        #: The user emptied the allow-list, the node was unreachable, and
        #: without this the accessories stay in every paired ecosystem for good,
        #: because nothing else ever attaches with an empty set again.
        self._replace_all_provider = replace_all_provider or (lambda: 0)
        #: How many entries the export allow-list DECLARES, before the classifier
        #: has had its say (``export_bridge._declared_export_count``). The gap
        #: between this and ``len(specs)`` is what :meth:`_replace_all` reads to
        #: tell "the user exported nothing" apart from "everything the user
        #: exported turned out to be unbridgeable" — see there for why the
        #: difference is a permanent halt rather than a nuance.
        self._export_count_provider = export_count_provider or (lambda: 0)
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
        self._attached_event = asyncio.Event()
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

    async def wait_attached(self, timeout: float = 30.0) -> None:
        """Block until an ``attach`` the node accepted has landed.

        :meth:`wait_connected` only proves the transport is up — the socket
        exists the moment the handshake starts, but ``self.status`` and
        ``self._attached`` are not populated until the attach that follows
        it completes, on the run loop, some time later. A caller that reads
        ``status`` or relies on ``set_state`` being deliverable right after
        ``wait_connected`` returns is trusting that the node is already
        serving its endpoint set — which is exactly the assumption that is
        NOT guaranteed: the attach might still be in flight, or might have
        been refused (§1.1's ``endpoint_map_invalid``), leaving a live socket
        that is connected but not attached. Wait on this instead of
        ``wait_connected`` whenever what you need is the node actually
        serving your endpoints, not merely a live socket to it.
        """
        await asyncio.wait_for(self._attached_event.wait(), timeout)

    def _mark_disconnected(self) -> None:
        self._attached = False
        self._attached_event.clear()
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
                f"{bridge_protocol.PROTOCOL_VERSION} (bridge {hello.bridge_version}); this "
                "plugin needs the paired bridge-node release — install it with Plugins ▸ "
                "Matter ▸ Install/update the Matter bridge once it is available. Restarting "
                "the agent alone relaunches the same node",
                reason="version_skew",
            )
        self.logger.info(
            "connected to bridge node (bridge %s, matter.js %s), attaching",
            hello.bridge_version, hello.matter_js_version,
        )
        # The desired set is read BEFORE the connection is declared usable. It
        # is blocking Indigo IPC (see :meth:`_attach`) so it happens off the
        # loop either way, but doing the hop here keeps ``connected`` meaning
        # "the attach is on its way" rather than "we are still deciding what to
        # send" — which is what every waiter on ``wait_connected`` assumes.
        specs = await self._gather_endpoints()
        owed = self._owed_removals()
        replace_all = self._replace_all(owed, specs)
        # Requests are legal from here on: attach is one.
        self._mark_connected()
        try:
            status = await self._attach(specs, replace_all=replace_all, timeout=None, inline=True)
        except BridgeProtocolError as exc:
            if str(exc.code) == bridge_protocol.ERR_MASS_REMOVAL_REFUSED and not replace_all:
                try:
                    if await self._retry_with_intent():
                        return
                except BridgeProtocolError as retried:
                    exc = retried
            self._handle_attach_refused(exc)
            return
        self._attached = True
        self._attached_event.set()
        self.recovery = False
        self._notify(self._on_attached, status, replace_all)

    def _owed_removals(self) -> int:
        """How many endpoints an un-export still owes the node, per the provider.

        The provider answers with a count rather than a flag because the two
        questions it feeds need different halves of it: *whether* to carry
        §3.1's opt-in, and *how long* the resulting attach may take (the node
        paces bulk removals ~100ms apart, §3.3).
        """
        try:
            return max(0, int(self._replace_all_provider() or 0))
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.warning("could not read the pending un-export flag (%s); "
                                "attaching without it", exc)
            return 0

    def _replace_all(self, owed: int, specs: Optional[list] = None) -> bool:
        """Whether this attach carries §3.1's ``intent: replace_all``.

        **The desired set's size does not GATE this**, and used to — "a
        `replace_all` alongside real endpoints is meaningless, so only ask when
        we are sending nothing" — which turned a debt plus a *disjoint* re-add
        into a permanent halt: the removals still emptied the node's live set,
        the guard still fired, and the attach that carried no intent was refused
        with ``mass_removal_refused``, which halts.

        Carrying the intent whenever a debt exists is safe in the direction that
        matters. §3.1's guard fires only when the reconcile would leave the live
        set EMPTY, so on any attach carrying real endpoints the flag is inert —
        it cannot license a removal the desired set was not already asking for.
        What it does is stop the one refusal that has no retry behind it.

        **The second reason is issue #141's restore, and it is new.** An
        allow-list with entries in it can still produce *zero* specs: every entry
        is re-classified on every attach (``export_bridge.endpoint_specs``) and
        each one is skipped if its Indigo device was deleted, stopped being
        exportable, lost the declared role, or raised out of ``states_for``. A
        single-device allow-list whose device the user deleted does it on its
        own. Before the restore that combination was harmless — with nothing
        ever created before the plugin attached, the node's live set was empty,
        §3.1's guard was never armed, and the empty attach was a no-op. The node
        now comes up holding the previously-exported set, so the very same
        routine restart sends ``[]`` against a NON-empty live set, is refused
        with ``mass_removal_refused``, and ``mass_removal_refused`` HALTS — every
        reload, for good, with a message about an allow-list that is not empty.

        Sending the intent is the honest reconcile: the desired set really is
        empty, and the alternative is a node serving accessories the plugin can
        neither drive nor push state to. It is not silent — ``endpoint_specs``
        names every skipped device, and the line below says what is about to
        happen — and it is not destructive of identity: §3.3 retains each
        endpoint-number allocation, so fixing whatever broke the devices brings
        the same accessories back rather than new ones.
        """
        if owed > 0:
            self.logger.info(
                "attaching with intent: replace_all — finishing an un-export of %d accessory "
                "record(s) that did not complete earlier", owed)
            return True
        if specs is None or specs:
            return False
        declared = self._declared_exports()
        if declared <= 0:
            # A genuinely empty allow-list. XG5 means the client should not even
            # exist here, and an empty attach against an empty live set is a
            # no-op anyway — so no intent, and the §3.1 guard keeps its teeth.
            return False
        self.logger.warning(
            "attaching with intent: replace_all — the export list still has %d entry/entries but "
            "NONE of them can be bridged right now (see the warnings above naming each one), so "
            "the desired set is empty. Those accessories are being removed from every paired "
            "ecosystem; their endpoint numbers are kept, so fixing the devices restores the same "
            "accessories rather than new ones.", declared)
        return True

    def _declared_exports(self) -> int:
        """How many entries the allow-list declares, before classification."""
        try:
            return max(0, int(self._export_count_provider() or 0))
        except Exception as exc:  # pylint: disable=broad-except
            # Nothing that reads this may fail closed onto "0": that is the
            # value meaning "genuinely empty allow-list", which is exactly the
            # answer that lets the halt above happen.
            self.logger.warning("could not read the declared export count (%s); assuming the "
                                "export list is empty", exc)
            return 0

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
        remedy = remedy_for(code, exc.details)
        if remedy is None:
            self._notify(self._on_attach_refused, code, exc.details)
            raise HandshakeFailed(f"bridge node refused attach: {code} ({exc.details})")
        self.logger.error("bridge node refused attach (%s): %s. %s", code, exc.details, remedy)
        self._notify(self._on_attach_refused, code, exc.details)
        if code in HALTING_ATTACH_ERRORS:
            raise ClientHalted(f"attach refused: {code} ({exc.details})", reason=code)
        # endpoint_map_invalid: stay connected, un-attached, in recovery.
        self.recovery = True

    async def _retry_with_intent(self) -> bool:
        """Answer a ``mass_removal_refused`` by re-attaching WITH the opt-in.

        Belt and braces behind :meth:`_replace_all`, and the belt is the part
        that matters: ``mass_removal_refused`` HALTS, and a halted client never
        retries anything. If we reach it while a debt is recorded then the
        removal the node refused is one the user genuinely asked for — that is
        what the debt IS — and halting there strands every exported accessory in
        every ecosystem permanently, behind a message blaming the allow-list.

        Exactly once, and only when a debt says so. A blind retry-with-intent
        would turn the §3.1 guard into a speed bump; this cannot fire for a
        client that was never told to un-export anything.
        """
        # RE-read, deliberately: the only way to be here at all is that the
        # first read did not see a debt (a debt always carries the intent), so
        # the interesting case is precisely the one where that read was wrong —
        # a prefs read that glitched, or a debt written between the two.
        owed = self._owed_removals()
        if owed <= 0:
            return False
        self.logger.warning(
            "the bridge node refused the attach as a mass removal, but an un-export of %d "
            "accessory record(s) IS outstanding — retrying once with intent: replace_all "
            "rather than halting", owed)
        status = await self._attach(None, replace_all=True, timeout=None, inline=True)
        self._attached = True
        self._attached_event.set()
        self.recovery = False
        self._notify(self._on_attached, status, True)
        return True

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
                     timeout: Optional[float] = None) -> StatusReport:
        """Declare this client and deliver the full desired endpoint set (§3.1).

        Called by the handshake on every (re)connect, which is what makes a fresh
        connection a full reconcile (PRD §5.4). ``endpoints`` defaults to a fresh
        read of the injected provider — never a cached list, since the allow-list
        may have changed while the socket was down.

        ``replace_all`` is the §3.1 opt-in required to empty a non-empty live set;
        the plugin passes it only on the deliberate allow-list-emptied path
        (PRD §7), so a stale client can never un-export everything by default.

        ``timeout`` defaults to :func:`attach_timeout_for` over the set actually
        being sent — which is why it cannot be a default argument value.
        """
        return await self._attach(endpoints, replace_all=replace_all, timeout=timeout, inline=False)

    async def _gather_endpoints(self) -> list:
        """Read the injected endpoint provider, OFF the loop.

        The provider is ``export_bridge.endpoint_specs``, which does one
        synchronous ``indigo.devices[id]`` IPC copy PER exported device — 60
        exports is 60 blocking round trips to IndigoServer. This loop is shared
        with the inbound matter-server client, so running that inline would
        stall live Matter device updates behind an export reconcile every time
        the bridge reconnects.

        The provider itself deliberately stays an ordinary blocking callable:
        it is the injected seam's public shape, and making it a coroutine would
        push the same problem into every implementation instead of solving it
        once here.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._endpoint_provider)

    async def _attach(self, endpoints: Optional[list], *, replace_all: bool,
                      timeout: Optional[float], inline: bool) -> StatusReport:
        """The attach itself. ``inline`` selects how the response is waited for.

        The handshake issues its attach before the run loop's listen loop owns
        the socket, so there is no dispatcher to resolve a pending future — it
        pumps the socket itself. A caller re-attaching on a live connection goes
        through the normal correlated path.

        """
        specs = await self._gather_endpoints() if endpoints is None else endpoints
        if timeout is None:
            # Sized over whichever count the node will actually spend time on.
            # An attach's cost is its ~100ms-paced REMOVALS (§3.3), and for the
            # discharge attach those are the opposite of what is sent: nothing
            # goes, everything owed comes back off. `len(specs)` alone handed an
            # 80-accessory un-export the 8s floor, timed it out mid-reconcile,
            # then tore the socket down and retried — forever, with the
            # accessories still in every ecosystem.
            #
            # The declared count is the third candidate and covers the same
            # shape without a debt: when the classifier skips a whole allow-list
            # (:meth:`_replace_all`) the node removes its entire restored set
            # against `len(specs) == 0` and `owed == 0`, and the allow-list's own
            # length is the only proxy for how much that is.
            timeout = attach_timeout_for(
                max(len(specs), self._owed_removals(), self._declared_exports()))
        frame = self.proto.build_attach(self.plugin_version, specs, replace_all=replace_all)
        if inline:
            result = await self._handshake_request(frame, timeout, bridge_protocol.CMD_ATTACH)
        else:
            result = await self._request_frame(frame, timeout)
            self._attached = True
            self._attached_event.set()
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

    async def remove_endpoint(self, indigo_device_id: int, *, permanent: bool = False,
                              timeout: float = DEFAULT_TIMEOUT) -> bool:
        """Remove one endpoint (§3.3). ``False`` means it was already absent.

        ``permanent`` (issue #274) is the confirmed-gone declaration — see
        ``bridge_protocol.BridgeProtocol.build_remove_endpoint``. Defaults to
        ``False`` so `replace()`'s two-command supersede/re-adopt sequence,
        which calls this directly rather than through `ExportBridge.remove`,
        is unaffected.
        """
        result = await self._request_frame(
            self.proto.build_remove_endpoint(indigo_device_id, permanent=permanent), timeout)
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

    async def remove_fabric(self, fabric_index: int,
                            timeout: float = LONG_TIMEOUT) -> bridge_protocol.FabricRemoval:
        """Drop one ecosystem's fabric (§3.9). Returns what the node actually did.

        Returning the outcome rather than ``None`` is the whole §3.9 fix: an
        index with no fabric behind it is a *success* on the wire, and a caller
        that tells the user "that ecosystem has been unpaired" in the past tense
        has to be able to tell that apart from a real removal.
        """
        result = await self._request_frame(self.proto.build_remove_fabric(fabric_index), timeout)
        return bridge_protocol.parse_fabric_removal(result)

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
        """§3.11 — adopt the live endpoint numbers as the new persisted map.

        The recovery path out of ``endpoint_map_invalid``. It renumbers
        nothing — any duplication belongs to the storage loss that caused the
        refusal — but it irreversibly discards the persisted baseline, which
        is why it is a separate command the plugin only issues after the user
        confirms a warning dialog.

        When it is called from the recovery state (the node refused our attach)
        it also re-attaches: the map is valid again, so the connection can stop
        being a three-command lifeline and go back to being an export.

        **The rebuild and the re-attach are two outcomes, not one.** By the time
        the node answers this command it has already written the new map and
        stopped refusing — that is what its StatusReport *means*, and it is the
        irreversible half. Letting a failure of the re-attach after it propagate
        as though the whole thing failed produced a menu that told the user "the
        bridge node is unchanged and still refusing to export" when neither
        clause was true, left ``recovery`` set so every state push went on being
        dropped, and invited them to repeat an operation that had already
        discarded the persisted baseline.

        So the recovery flag is cleared the moment the report lands, and the
        re-attach goes through the same triage as any other refused attach
        (:meth:`_handle_attach_refused`) rather than bypassing it. Its failure
        is reported and swallowed here: the caller gets the rebuild's status,
        and whether the connection came back is readable from :attr:`attached`.
        """
        result = await self._request_frame(self.proto.build_rebuild_endpoint_map(), timeout)
        self.status = bridge_protocol.parse_status(result)
        if not self.recovery:
            return self.status
        # The node has rebuilt and stopped refusing. Whatever happens next, THAT
        # is now the truth about it, and nothing below may make it look otherwise.
        self.recovery = False
        rebuilt = self.status
        self.logger.info("endpoint map rebuilt; re-attaching to the bridge node")
        # The debt outlives the refusal: a node that was refusing never took
        # our un-export either, so the re-attach has to carry the same opt-in
        # the handshake would have. The desired set is read HERE rather than
        # inside `attach`, so this decision sees the same specs the frame will
        # carry — deciding first and gathering afterwards would judge the
        # all-skipped case (see :meth:`_replace_all`) on a stale read, or on no
        # read at all.
        specs = await self._gather_endpoints()
        replace_all = self._replace_all(self._owed_removals(), specs)
        try:
            status = await self.attach(specs, replace_all=replace_all)
        except BridgeProtocolError as exc:
            self._triage_rebuilt_reattach(exc)
            return rebuilt
        self._notify(self._on_attached, status, replace_all)
        return status

    def _triage_rebuilt_reattach(self, exc: BridgeProtocolError) -> None:
        """Handle a refusal of the post-rebuild re-attach, without unwinding.

        :meth:`_handle_attach_refused` is written for the handshake, where the
        run loop is the thing that catches what it raises — it is what turns a
        :class:`ClientHalted` into the :attr:`halted` latch and a
        :class:`HandshakeFailed` into a backoff. Here there is no run loop above
        us: we are on a live connection, called from a menu. Re-using the triage
        is still right (a refusal means the same thing whoever asked), so the
        two states it can only signal by raising are applied here instead, and
        the exception stops — the rebuild it followed genuinely happened and
        must not be reported as a failure.
        """
        try:
            self._handle_attach_refused(exc)
        except ClientHalted as halt:
            # Exactly what WsJsonClient.run does with one, because nothing else
            # is going to see this one.
            self.halted = True
            self.halted_reason = halt.reason or None
            self.logger.error(
                "the endpoint map was rebuilt, but re-attaching HALTED the bridge client (%s); "
                "nothing is being exported and it will not retry on its own", halt)
        except HandshakeFailed as failed:
            self.logger.warning(
                "the endpoint map was rebuilt, but re-attaching was refused (%s); the ordinary "
                "reconnect will try again", failed)

    async def list_orphans(self, timeout: float = DEFAULT_TIMEOUT) -> list:
        """§3.12 — every left-behind accessory identity the re-adopt picker
        (issue #219) could offer. Read-only: no argument, no state change on
        the node — the same "local and quick" cost class as :meth:`get_status`.
        """
        result = await self._request_frame(self.proto.build_list_orphans(), timeout)
        return bridge_protocol.parse_orphans(result)
