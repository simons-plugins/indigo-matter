"""Persistent WebSocket client to matter-server.

Runs entirely on the plugin's asyncio loop (see ``async_runtime``). Responsible
for: connecting, capturing ``server_info``, sending ``start_listening``,
streaming events to an ``on_event`` callback, correlating request/response by
``message_id``, and auto-reconnecting with exponential backoff (capped 30s).

All wire field names go through :mod:`protocol`, so this module never hard-codes
matter-server's vocabulary. The connection factory is injectable so the client
is testable against an in-process fake WebSocket (no Node process needed).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable, Optional

import protocol
from protocol import MatterCommand, Protocol, ProtocolError

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


class MatterClient:
    """A reconnecting matter-server WebSocket client."""

    def __init__(
        self,
        proto: Protocol,
        logger: Any,
        prefs: dict,
        *,
        connect: Optional[Callable[[str], Awaitable]] = None,
        on_event: Optional[Callable[[protocol.MatterEvent], None]] = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ) -> None:
        self.proto = proto
        self.logger = logger
        host = prefs.get("matterServerHost", "localhost")
        port = prefs.get("matterServerPort", "5580")
        path = prefs.get("matterServerPath", "/ws")
        self.uri = f"ws://{host}:{port}{path}"
        self._connect = connect or _default_connect
        self._on_event = on_event
        self._sleep = sleep
        self._now = now

        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._closing = False
        self._connected_event = asyncio.Event()

        # liveness flags read by the watchdog (runConcurrentThread)
        self.connected = False
        self.server_info: Optional[dict] = None
        self.last_event_ts: Optional[float] = None

    # ------------------------------------------------------------------
    # Long-lived run loop
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Connect → listen → reconnect (backoff) until :meth:`close`."""
        attempt = 0
        while not self._closing:
            try:
                await self._connect_once()
                attempt = 0
                await self._listen()
            except asyncio.CancelledError:
                raise
            except _WS_ERRORS as exc:
                self.logger.warning("matter-server connection lost: %s", exc)
            except Exception as exc:  # pragma: no cover - defensive
                self.logger.exception(exc)
            finally:
                self._mark_disconnected()
            if self._closing:
                break
            delay = min(2 ** attempt, MAX_BACKOFF)
            attempt += 1
            self.logger.debug("reconnecting to matter-server in %.0fs", delay)
            await self._sleep(delay)

    async def _connect_once(self) -> None:
        self.logger.debug("connecting to %s", self.uri)
        self._ws = await self._connect(self.uri)
        first = await self._recv()
        if self.proto.is_event(first):
            self.server_info = first.get(protocol.KEY_DATA, {})
        self.connected = True
        self._connected_event.set()
        # TODO(M4-ondevice): start_listening is assumed to stream ALL attribute
        # changes (python-matter-server behaviour). If the pinned matter-server
        # release requires explicit per-attribute subscriptions, wire up the
        # handlers' attributes_to_subscribe() here — currently that method is
        # defined but unused, and OnOff state would silently never update.
        await self._send_frame(self.proto.build_request(protocol.CMD_START_LISTENING))
        self.logger.info("connected to matter-server, listening")

    async def _listen(self) -> None:
        async for raw in self._ws:
            self._dispatch(json.loads(raw))

    def _dispatch(self, frame: dict) -> None:
        if self.proto.is_event(frame):
            self.last_event_ts = self._now()
            evt = self.proto.parse_event(frame)
            if self._on_event is not None:
                try:
                    self._on_event(evt)
                except Exception as exc:  # pragma: no cover - handler must not kill the loop
                    self.logger.exception(exc)
            return
        if self.proto.is_response(frame):
            mid = self.proto.message_id_of(frame)
            fut = self._pending.get(mid)
            if fut is not None and not fut.done():
                try:
                    fut.set_result(self.proto.parse_result(frame))
                except ProtocolError as exc:
                    fut.set_exception(exc)

    def _mark_disconnected(self) -> None:
        self.connected = False
        self._connected_event.clear()
        # fail any in-flight requests so callers don't hang
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("matter-server disconnected"))
        self._pending.clear()

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    async def _request_frame(self, frame: dict, timeout: float) -> Any:
        if self._ws is None or not self.connected:
            raise ConnectionError("matter-server not connected")
        mid = frame[protocol.KEY_MESSAGE_ID]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            await self._send_frame(frame)
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def request(self, command: str, args: Optional[dict] = None, timeout: float = 10.0) -> Any:
        return await self._request_frame(self.proto.build_request(command, args), timeout)

    async def send_command(self, cmd: MatterCommand, timeout: float = 5.0) -> Any:
        return await self._request_frame(self.proto.build_command(cmd), timeout)

    # convenience wrappers
    async def get_nodes(self) -> Any:
        return await self.request(protocol.CMD_GET_NODES)

    async def get_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_GET_NODE, {"node_id": node_id})

    async def commission_with_code(self, code: str, timeout: float = 60.0) -> Any:
        return await self.request(protocol.CMD_COMMISSION, {"code": code}, timeout)

    async def remove_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_REMOVE_NODE, {"node_id": node_id})

    async def open_commissioning_window(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_OPEN_WINDOW, {"node_id": node_id})

    async def interview_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_INTERVIEW, {"node_id": node_id})

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------
    async def wait_connected(self, timeout: float = 30.0) -> None:
        await asyncio.wait_for(self._connected_event.wait(), timeout)

    async def close(self) -> None:
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        self._mark_disconnected()

    # ------------------------------------------------------------------
    # Low-level IO
    # ------------------------------------------------------------------
    async def _send_frame(self, frame: dict) -> None:
        await self._ws.send(json.dumps(frame))

    async def _recv(self) -> dict:
        return json.loads(await self._ws.recv())
