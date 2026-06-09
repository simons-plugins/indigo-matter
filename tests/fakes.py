"""In-process fake matter-server WebSocket for tests.

Mocks matter-server at the WebSocket layer (IMPLEMENTATION §7), so the WS client,
HTTP handlers, and reconciliation can all be tested without a Node process.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

import protocol

_CLOSE = object()


class FakeWebSocket:
    """A controllable stand-in for a ``websockets`` connection.

    On connect it emits a ``server_info`` event (matching matter-server's
    announce-on-connect behaviour). Client commands are recorded in ``sent`` and
    answered by an optional ``responder`` callback. Tests can also ``push_event``
    server-initiated events.
    """

    def __init__(self, responder: Optional[Callable[[dict], list]] = None,
                 server_info: Optional[dict] = None):
        self._outbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self._closed = False
        self._responder = responder
        # matter-server pushes server_info as a BARE object on connect (no
        # event/message_id wrapper) — match that real handshake shape.
        self._outbox.put_nowait(json.dumps(
            server_info or {"sdk_version": "matter-server/0.6.2", "fabric_id": "0x0"}
        ))

    async def send(self, raw: str) -> None:
        if self._closed:
            raise ConnectionError("send on closed socket")
        frame = json.loads(raw)
        self.sent.append(frame)
        if self._responder is not None:
            for response in (self._responder(frame) or []):
                await self._outbox.put(json.dumps(response))

    async def recv(self) -> str:
        if self._closed and self._outbox.empty():
            raise ConnectionError("recv on closed socket")
        item = await self._outbox.get()
        if item is _CLOSE:
            raise ConnectionError("socket closed")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionError as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        self._closed = True
        await self._outbox.put(_CLOSE)

    async def push_event(self, event: str, data: dict) -> None:
        await self._outbox.put(json.dumps({protocol.KEY_EVENT: event, protocol.KEY_DATA: data}))

    def sent_commands(self) -> list[str]:
        return [f.get(protocol.KEY_COMMAND) for f in self.sent]


def scripted_responder(results: dict) -> Callable[[dict], list]:
    """Build a responder that answers each command with a canned result.

    ``results`` maps command name -> result payload (or a callable(frame)->payload).
    Unknown commands get a ``result: None`` reply so requests never hang.
    """
    def _respond(frame: dict) -> list:
        command = frame.get(protocol.KEY_COMMAND)
        mid = frame.get(protocol.KEY_MESSAGE_ID)
        if command in results:
            value = results[command]
            if callable(value):
                value = value(frame)
            return [{protocol.KEY_MESSAGE_ID: mid, protocol.KEY_RESULT: value}]
        return [{protocol.KEY_MESSAGE_ID: mid, protocol.KEY_RESULT: None}]
    return _respond


async def returns(value):
    """Await-able that yields ``value`` (for the client's connect factory)."""
    return value
