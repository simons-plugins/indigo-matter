"""Reconnecting JSON-over-WebSocket client core — shared transport for both peers.

The plugin talks to two Node processes over the same envelope grammar:

* **matter-server** (inbound controller) — :mod:`matter_client`
* **the bridge node** (outbound export) — :mod:`bridge_client`, BRIDGE_PROTOCOL.md

Everything they share lives here: the run loop (connect → listen → reconnect with
``min(2**attempt, 30)`` backoff), ``message_id`` → future correlation with a
``wait_for`` deadline, disconnect handling (in-flight futures fail with
``ConnectionError``; ``on_disconnect`` only on a genuine drop of a live
connection), and the once-per-streak repeated-failure diagnostic.

The failure handling is deliberate and shared for the same reason the happy path
is: a peer that opens the socket and never speaks gets :data:`HELLO_TIMEOUT`
rather than an unbounded wait; a frame that will not decode is dropped with the
frame in the log and the connection kept; every way out of a connection attempt
closes the socket; and a halt latched inside one iteration of the loop wins over
a :meth:`WsJsonClient.resume` racing it from another thread.

What is *not* shared is the handshake: matter-server pushes ``server_info`` and
then wants ``start_listening``, while the bridge node pushes a bare hello frame
and then wants ``attach``. That step is the :meth:`WsJsonClient._handshake` hook.
Frame classification is hooked too, so :mod:`protocol`'s rename firewall stays
intact — this module never names a wire field.

No Indigo dependency; the connection factory is injectable so both clients are
testable against an in-process fake WebSocket (no Node process).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

# Exceptions that mean "the socket dropped, reconnect". websockets raises its own
# WebSocketException family; we also accept the stdlib connection errors so the
# in-process fake can signal a drop without importing websockets internals.
try:  # pragma: no cover - import guard
    import websockets
    from websockets.exceptions import WebSocketException
    WS_ERRORS: tuple = (OSError, ConnectionError, WebSocketException)

    def _default_connect(uri: str) -> Awaitable:
        return websockets.connect(uri, open_timeout=10, ping_interval=20)
except ImportError:  # pragma: no cover
    websockets = None
    WS_ERRORS = (OSError, ConnectionError)

    def _default_connect(uri: str) -> Awaitable:
        raise RuntimeError("websockets library not available")

MAX_BACKOFF = 30.0

#: How much of a bad frame goes in the log line. Enough to recognise it, not
#: enough to let a peer that is spraying megabytes fill the Indigo event log.
LOG_FRAME_CHARS = 500

#: How many fire-and-forget sends keep a "what was this?" note (H4). Bounded
#: because nothing ever pops the entry for a response the peer never sends.
SEND_CONTEXT_LIMIT = 256


def _truncate(value: Any, limit: int = LOG_FRAME_CHARS) -> str:
    """Render ``value`` for a log line, capped at ``limit`` characters."""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= limit else f"{text[:limit]}… ({len(text)} chars)"


def pref_str(prefs: dict, key: str, default: str) -> str:
    """Read a string pref, falling back to ``default`` when blank *or* absent.

    ``prefs.get(key, default)`` only fires the default when the key is ABSENT; a
    present-but-empty ``""`` would sail through and yield e.g. ``ws://host:/ws``,
    which websockets then silently connects to on port 80.
    """
    return str(prefs.get(key) or "").strip() or default


class ClientHalted(Exception):
    """Raised by :meth:`WsJsonClient._handshake` to stop reconnecting.

    For failures that reconnecting cannot fix — protocol version skew being the
    one that actually happens (BRIDGE_PROTOCOL §2, skew fails closed) — a retry
    loop is just a spinning error. The run loop exits and stays exited until
    :meth:`WsJsonClient.resume` is called.

    ``reason`` is a short machine-readable tag (an error code, ``version_skew``)
    published as :attr:`WsJsonClient.halted_reason` so a supervisor can tell the
    halts apart without parsing the message.
    """

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class HandshakeFailed(Exception):
    """A handshake failure a reconnect might genuinely fix.

    The counterpart to :class:`ClientHalted`: the run loop logs it plainly (the
    peer told us *why*, so a traceback adds nothing) and retries with the normal
    backoff, counting it toward the repeated-failure diagnostic.
    """


class WsJsonClient:
    """A reconnecting WebSocket client that speaks request/response/event JSON."""

    #: Peer name used in log lines; subclasses override.
    PEER = "peer"

    #: Deadline for the peer's bare opening frame. Both peers announce
    #: themselves the instant the socket opens, so anything that does not is not
    #: the peer we think it is — most often something else already on the port.
    #: Without this the first ``recv`` waits forever and the client is wedged in
    #: a state where ``connected`` is False and no reconnect is ever attempted.
    HELLO_TIMEOUT = 5.0

    def __init__(
        self,
        uri: str,
        logger: Any,
        *,
        connect: Optional[Callable[[str], Awaitable]] = None,
        on_connect: Optional[Callable[[], Awaitable]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_repeated_failure: Optional[Callable[[int], None]] = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ) -> None:
        self.uri = uri
        self.logger = logger
        self._connect = connect or _default_connect
        # on_connect: async, scheduled after each (re)connect — used to re-reconcile.
        # on_disconnect: sync, fired on a real drop (not intentional close) — used to
        # mark devices unreachable. Together they give sleep/wake + crash recovery.
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        # on_repeated_failure: sync, fired ONCE per failure streak (after a couple of
        # consecutive reconnect failures, reset on the next successful connect) so a
        # supervisor can surface WHY the server is unreachable without log spam.
        self._on_repeated_failure = on_repeated_failure
        self._diag_fired = False
        self._sleep = sleep
        self._now = now

        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        # message_id → short human description of a request nobody is awaiting,
        # so its error response can name the device instead of just an id (H4).
        self._send_context: dict[str, str] = {}
        self._closing = False
        self._running = False
        self._connected_event = asyncio.Event()
        self._reconcile_task: Optional[asyncio.Task] = None

        # liveness flags read by the watchdog (runConcurrentThread)
        self.connected = False
        self.halted = False
        #: Why we halted (``ClientHalted.reason``), or ``None``.
        self.halted_reason: Optional[str] = None
        self.last_event_ts: Optional[float] = None

    # ------------------------------------------------------------------
    # Long-lived run loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Connect → listen → reconnect (backoff) until :meth:`close` or a halt."""
        attempt = 0
        self._running = True
        try:
            await self._run_loop(attempt)
        finally:
            self._running = False

    async def _run_loop(self, attempt: int) -> None:
        while not self._closing:
            # Latched locally: whether THIS iteration saw a halt. ``self.halted``
            # alone is racy — ``resume`` can clear it from another task while we
            # are awaiting the socket teardown below, which would silently turn a
            # fail-closed halt back into a reconnect loop.
            halted_here = False
            try:
                await self._connect_once()
                attempt = 0
                self._diag_fired = False
                await self._listen()
            except asyncio.CancelledError:
                raise
            except ClientHalted as exc:
                # Fails closed: no retry, no further frames on this socket.
                self.halted = True
                self.halted_reason = exc.reason or None
                halted_here = True
                self.logger.error("%s handshake refused, not reconnecting: %s", self.PEER, exc)
            except HandshakeFailed as exc:
                # The peer answered and said why; retry, but do not pretend this
                # was a dropped socket and do not dump a traceback for it.
                self.logger.warning("%s handshake failed (%s): %s — retrying", self.PEER, self.uri, exc)
                self._maybe_report_repeated_failure(attempt)
            except asyncio.TimeoutError as exc:
                # Caught BEFORE WS_ERRORS on purpose: from 3.11 asyncio.TimeoutError
                # IS the builtin TimeoutError, which is an OSError — so it would
                # otherwise land in the branch below and log "connection lost: "
                # with an empty reason, the least useful line we could emit.
                self.logger.warning("%s timed out (%s): %s", self.PEER, self.uri,
                                    str(exc) or "no detail given")
                self._maybe_report_repeated_failure(attempt)
            except WS_ERRORS as exc:
                self.logger.warning("%s connection lost (%s): %s", self.PEER, self.uri, exc)
                self._maybe_report_repeated_failure(attempt)
            except Exception as exc:  # defensive: never let the run loop die
                self.logger.exception(
                    "%s (%s): unexpected failure, tearing down the connection: %s",
                    self.PEER, self.uri, exc)
                self._maybe_report_repeated_failure(attempt)
            finally:
                # Every exit from an iteration closes the socket. Leaking it left
                # a half-open connection behind on each cycle — and with the node
                # holding the old socket "attached", the 1s reconnect became a
                # connect/attach hammer.
                await self._close_socket()
                self._mark_disconnected()
            if self._closing or halted_here or self.halted:
                break
            delay = min(2 ** attempt, MAX_BACKOFF)
            attempt += 1
            self.logger.debug("reconnecting to %s in %.0fs", self.PEER, delay)
            await self._sleep(delay)

    def _maybe_report_repeated_failure(self, attempt: int) -> None:
        """Fire ``on_repeated_failure`` once per streak, after ≥2 consecutive fails.

        ``attempt`` is 0 on the first failure, so ``>= 1`` means the second failure
        onward — enough to tell a real problem from a one-off blip (e.g. the server
        restarting) without emitting a diagnostic on every backoff cycle.
        """
        if self._on_repeated_failure is None or self._diag_fired or attempt < 1:
            return
        self._diag_fired = True
        try:
            self._on_repeated_failure(attempt + 1)
        except Exception:  # pragma: no cover - diagnostics must not kill the run loop
            self.logger.exception("on_repeated_failure hook raised")

    def rearm_failure_diagnostic(self) -> None:
        """Allow ``on_repeated_failure`` to fire again this streak.

        The hook is normally latched once per failure streak (reset only on a
        successful connect). A supervisor that *suppresses* one firing (e.g. during an
        expected restart) calls this to DEFER — not drop — the diagnostic, so a
        still-failing server surfaces its real error on the next cycle instead of going
        silent for the whole streak.
        """
        self._diag_fired = False

    def resume(self) -> None:
        """Clear the :class:`ClientHalted` latch so :meth:`run` can be started again.

        The halt exists so a version-skewed peer is not hammered forever; whoever
        fixed the skew (restarted the agent, updated the node) calls this.

        This only clears the latch — it does **not** restart :meth:`run`. The
        caller owns the run task and must start a new one (that is also why
        calling it while a run loop is live is refused: the running loop would
        not pick it up, and clearing the flag mid-teardown is exactly the race
        the local latch in :meth:`_run_loop` guards against).
        """
        if self._running:
            self.logger.warning(
                "%s: resume() ignored while the client is still running — stop it first, "
                "then start a new run task", self.PEER)
            return
        self.logger.info("%s: halt cleared, reconnecting is allowed again (start a new run task)",
                         self.PEER)
        self.halted = False
        self.halted_reason = None

    async def _connect_once(self) -> None:
        self.logger.debug("connecting to %s", self.uri)
        self._ws = await self._connect(self.uri)
        # Both peers push a BARE object as the first frame (no event/message_id
        # wrapper): matter-server's server_info, the bridge node's hello.
        try:
            first = await asyncio.wait_for(self._recv(), self.HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"no handshake frame within {self.HELLO_TIMEOUT:g}s from {self.uri} — "
                "is another service on this port?") from None
        await self._handshake(first)
        self._mark_connected()
        # Re-reconcile on every (re)connect so devices that changed (or that we
        # missed) while disconnected are corrected and 'unreachable' is cleared.
        # Scheduled as its own task so it doesn't block the listen loop; keep a
        # strong ref (else it can be GC'd) and log any failure rather than letting
        # it vanish into a never-retrieved task exception.
        if self._on_connect is not None:
            self._reconcile_task = asyncio.create_task(self._on_connect())
            self._reconcile_task.add_done_callback(self._on_reconcile_done)

    async def _listen(self) -> None:
        async for raw in self._ws:
            self._handle_raw(raw)

    def _handle_raw(self, raw: Any) -> None:
        """Decode and dispatch ONE frame, containing anything it throws.

        A malformed frame is the peer's bug, not a reason to drop a working
        connection: tearing down here would reconnect, re-attach, and very
        likely be handed the same bad frame again. Log it (with enough of the
        frame to identify it), drop it, keep listening.
        """
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError) as exc:
            self.logger.warning("%s: undecodable frame dropped (%s): %s",
                                self.PEER, exc, _truncate(raw))
            return
        self._handle_frame(frame)

    def _handle_frame(self, frame: Any) -> None:
        """Dispatch one already-decoded frame, containing anything it throws."""
        if not isinstance(frame, dict):
            self.logger.warning("%s: non-object frame dropped: %s", self.PEER, _truncate(frame))
            return
        try:
            self._dispatch(frame)
        except Exception as exc:  # a bad frame must not kill the listen loop
            self.logger.warning("%s: frame dropped, dispatch raised (%s): %s",
                                self.PEER, exc, _truncate(frame))

    def _dispatch(self, frame: dict) -> None:
        if self._is_event(frame):
            self.last_event_ts = self._now()
            self._handle_event(frame)
            return
        if not self._is_response(frame):
            # Neither an event nor a response: not something this protocol has a
            # slot for. Silently returning made a peer talking a different
            # grammar look exactly like a healthy idle connection.
            self.logger.debug("%s: frame is neither event nor response, dropped: %s",
                              self.PEER, _truncate(frame))
            return
        mid = self._message_id_of(frame)
        fut = self._pending.get(mid)
        if fut is None:
            self._log_unmatched(frame, mid)
            return
        self._send_context.pop(mid, None)
        if not fut.done():
            try:
                fut.set_result(self._parse_result(frame))
            except Exception as exc:  # protocol-level failure → surface to the caller
                fut.set_exception(exc)

    def _log_unmatched(self, frame: dict, mid: Optional[str]) -> None:
        """Log a response nobody is waiting for; errors loudly (BRIDGE_PROTOCOL §3.4).

        Fire-and-forget requests (``set_state``) still get a response, and an
        unnoticed failure there looks exactly like "the ecosystem shows stale
        state" — so an unmatched *error* is a warning, never a silent drop.

        The warning names the request when we still have a note for it: a bare
        ``message_id`` is not something anybody can act on, and "which device?"
        is the first question the log line has to answer.
        """
        context = self._send_context.pop(mid, None)
        described = f"{context} (message_id={mid})" if context else f"message_id={mid}"
        error = self._error_of(frame)
        if error is None:
            self.logger.debug("%s: unmatched response (%s)", self.PEER, described)
            return
        code, details = error
        self.logger.warning(
            "%s error for an un-awaited request [%s]: %s %s",
            self.PEER, described, code, details,
        )

    def _remember_send_context(self, message_id: Optional[str], context: str) -> None:
        """Note what an un-awaited request was, for :meth:`_log_unmatched`.

        Bounded and FIFO: a peer that never answers would otherwise grow this
        map for the lifetime of the plugin.
        """
        if message_id is None:
            return
        self._send_context[message_id] = context
        while len(self._send_context) > SEND_CONTEXT_LIMIT:
            del self._send_context[next(iter(self._send_context))]

    def _on_reconcile_done(self, task: "asyncio.Task") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.logger.exception(exc)  # on_connect failed — surface, don't swallow

    def _mark_connected(self) -> None:
        """Flip the liveness flags. Idempotent — handshake hooks may call it early."""
        self.connected = True
        self._connected_event.set()

    def _mark_disconnected(self) -> None:
        was_connected = self.connected
        self.connected = False
        self._connected_event.clear()
        # fail any in-flight requests so callers don't hang
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError(f"{self.PEER} disconnected"))
        self._pending.clear()
        # Notes about un-awaited requests on a socket that is gone can never be
        # matched now; keeping them would only mislabel a later message_id.
        self._send_context.clear()
        # Notify only on a genuine drop of a live connection — not on a failed
        # initial connect (was never connected) nor an intentional close().
        if was_connected and not self._closing and self._on_disconnect is not None:
            try:
                self._on_disconnect()
            except Exception as exc:  # pragma: no cover - callback must not kill the loop
                self.logger.exception(exc)

    # ------------------------------------------------------------------
    # Subclass hooks — the handshake and the frame vocabulary
    # ------------------------------------------------------------------
    async def _handshake(self, first: Any) -> None:
        """Complete the peer's handshake, given its bare first frame.

        Called with the socket open but before the listen loop starts, so a hook
        that needs a response must use :meth:`_handshake_request`. Raise
        :class:`ClientHalted` to refuse the peer outright.
        """
        raise NotImplementedError

    def _is_event(self, frame: dict) -> bool:
        """True if this frame is an unsolicited event."""
        raise NotImplementedError

    def _is_response(self, frame: dict) -> bool:
        """True if this frame answers a request."""
        raise NotImplementedError

    def _message_id_of(self, frame: dict) -> Optional[str]:
        """The correlation id carried by a request or response frame."""
        raise NotImplementedError

    def _parse_result(self, frame: dict) -> Any:
        """Return a response's payload, or raise the protocol's error type."""
        raise NotImplementedError

    def _error_of(self, frame: dict) -> Optional[tuple]:
        """``(code, details)`` if this response carries an error, else ``None``."""
        raise NotImplementedError

    def _handle_event(self, frame: dict) -> None:
        """Deliver an inbound event to whatever the subclass wires it to."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    async def _request_frame(self, frame: dict, timeout: float) -> Any:
        if self._ws is None or not self.connected:
            raise ConnectionError(f"{self.PEER} not connected")
        mid = self._message_id_of(frame)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send_frame(frame)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def _handshake_request(self, frame: dict, timeout: float,
                                 what: str = "handshake request") -> Any:
        """Send a request and pump the socket inline until its response lands.

        Only for :meth:`_handshake`: the listen loop is not running yet, so there
        is no dispatcher to resolve a pending future. Any other frame seen while
        waiting goes through the normal dispatcher rather than being dropped.

        ``what`` names the request in the timeout message — a bare
        ``TimeoutError`` stringifies to nothing at all.
        """
        mid = self._message_id_of(frame)
        await self._send_frame(frame)

        async def _pump() -> Any:
            while True:
                incoming = await self._recv()
                if isinstance(incoming, dict) and self._is_response(incoming) \
                        and self._message_id_of(incoming) == mid:
                    return self._parse_result(incoming)
                self._handle_frame(incoming)

        try:
            return await asyncio.wait_for(_pump(), timeout)
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"{what} not answered within {timeout:g}s by {self.PEER} ({self.uri})") from None

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------
    async def wait_connected(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout)

    async def close(self) -> None:
        self._closing = True
        await self._close_socket()
        self._mark_disconnected()

    async def _close_socket(self) -> None:
        """Close the socket, best effort.

        Called on every teardown path including ones that are already handling a
        failure — an exception raised while closing an already-broken socket
        would replace the real error with a meaningless one.
        """
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.debug("%s: closing the socket raised %s; dropping it anyway",
                              self.PEER, exc)

    # ------------------------------------------------------------------
    # Low-level IO
    # ------------------------------------------------------------------
    async def _send_frame(self, frame: dict) -> None:
        await self._ws.send(json.dumps(frame))

    async def _recv(self) -> Any:
        return json.loads(await self._ws.recv())
