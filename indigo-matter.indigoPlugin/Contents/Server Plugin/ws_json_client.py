"""Reconnecting JSON-over-WebSocket client core — shared transport for both peers.

The plugin talks to two Node processes over the same envelope grammar:

* **matter-server** (inbound controller) — :mod:`matter_client`
* **the bridge node** (outbound export) — :mod:`bridge_client`, BRIDGE_PROTOCOL.md

Everything they share lives here: the run loop (connect → listen → reconnect with
``min(2**attempt, 30)`` backoff), ``message_id`` → future correlation with a
``wait_for`` deadline, disconnect handling (in-flight futures fail with
``ConnectionError``; ``on_disconnect`` only on a genuine drop of a live
connection), and the once-per-streak repeated-failure diagnostic.

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
    _WS_ERRORS: tuple = (OSError, ConnectionError, WebSocketException)

    def _default_connect(uri: str) -> Awaitable:
        return websockets.connect(uri, open_timeout=10, ping_interval=20)
except ImportError:  # pragma: no cover
    websockets = None
    _WS_ERRORS = (OSError, ConnectionError)

    def _default_connect(uri: str) -> Awaitable:
        raise RuntimeError("websockets library not available")

MAX_BACKOFF = 30.0


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
    """


class WsJsonClient:
    """A reconnecting WebSocket client that speaks request/response/event JSON."""

    #: Peer name used in log lines; subclasses override.
    PEER = "peer"

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
        self._closing = False
        self._connected_event = asyncio.Event()
        self._reconcile_task: Optional[asyncio.Task] = None

        # liveness flags read by the watchdog (runConcurrentThread)
        self.connected = False
        self.halted = False
        self.last_event_ts: Optional[float] = None

    # ------------------------------------------------------------------
    # Long-lived run loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Connect → listen → reconnect (backoff) until :meth:`close` or a halt."""
        attempt = 0
        while not self._closing:
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
                self.logger.error("%s handshake refused, not reconnecting: %s", self.PEER, exc)
                await self._close_socket()
            except _WS_ERRORS as exc:
                self.logger.warning("%s connection lost (%s): %s", self.PEER, self.uri, exc)
                self._maybe_report_repeated_failure(attempt)
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.exception(exc)
            finally:
                self._mark_disconnected()
            if self._closing or self.halted:
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
        """
        self.halted = False

    async def _connect_once(self) -> None:
        self.logger.debug("connecting to %s", self.uri)
        self._ws = await self._connect(self.uri)
        # Both peers push a BARE object as the first frame (no event/message_id
        # wrapper): matter-server's server_info, the bridge node's hello.
        first = await self._recv()
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
            self._dispatch(json.loads(raw))

    def _dispatch(self, frame: dict) -> None:
        if self._is_event(frame):
            self.last_event_ts = self._now()
            self._handle_event(frame)
            return
        if not self._is_response(frame):
            return
        mid = self._message_id_of(frame)
        fut = self._pending.get(mid)
        if fut is None:
            self._log_unmatched(frame, mid)
            return
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
        """
        error = self._error_of(frame)
        if error is None:
            self.logger.debug("%s: unmatched response (message_id=%s)", self.PEER, mid)
            return
        code, details = error
        self.logger.warning(
            "%s error for an un-awaited request (message_id=%s): %s %s",
            self.PEER, mid, code, details,
        )

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

    async def _handshake_request(self, frame: dict, timeout: float) -> Any:
        """Send a request and pump the socket inline until its response lands.

        Only for :meth:`_handshake`: the listen loop is not running yet, so there
        is no dispatcher to resolve a pending future. Any other frame seen while
        waiting goes through the normal dispatcher rather than being dropped.
        """
        mid = self._message_id_of(frame)
        await self._send_frame(frame)

        async def _pump() -> Any:
            while True:
                incoming = await self._recv()
                if self._is_response(incoming) and self._message_id_of(incoming) == mid:
                    return self._parse_result(incoming)
                self._dispatch(incoming)

        return await asyncio.wait_for(_pump(), timeout)

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
        if self._ws is not None:
            await self._ws.close()

    # ------------------------------------------------------------------
    # Low-level IO
    # ------------------------------------------------------------------
    async def _send_frame(self, frame: dict) -> None:
        await self._ws.send(json.dumps(frame))

    async def _recv(self) -> Any:
        return json.loads(await self._ws.recv())
