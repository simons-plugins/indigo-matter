"""Persistent WebSocket client to matter-server.

Runs entirely on the plugin's asyncio loop (see ``async_runtime``). The
transport — run loop, reconnect backoff, request/response correlation,
disconnect handling — lives in :mod:`ws_json_client`, shared with the outbound
bridge client. What stays here is everything matter-server-specific: the URI
built from the ``serverLocation`` prefs, the ``server_info`` + ``start_listening``
handshake, and the convenience wrappers for the controller's commands.

All wire field names go through :mod:`protocol`, so this module never hard-codes
matter-server's vocabulary. The connection factory is injectable so the client
is testable against an in-process fake WebSocket (no Node process needed).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

import protocol
from protocol import MatterCommand, MatterWrite, Protocol
# MAX_BACKOFF is re-exported: it was importable from here before the transport
# was extracted, and callers (and tests) still reach for it at this address.
from ws_json_client import (  # pylint: disable=unused-import
    MAX_BACKOFF,
    WsJsonClient,
    pref_str,
)

# Commissioning is the one RPC that legitimately takes minutes: discovery +
# PASE over the LAN was observed at ~124s in the 2026-06-09 live rehearsal,
# against the old 60s timeout (issue #16). 300s covers the observed worst case
# with headroom; this deliberately applies to commission_with_code only — every
# other RPC keeps the short default so a wedged matter-server still fails fast.
COMMISSION_TIMEOUT = 300.0


class MatterClient(WsJsonClient):
    """A reconnecting matter-server WebSocket client."""

    PEER = "matter-server"

    def __init__(
        self,
        proto: Protocol,
        logger: Any,
        prefs: dict,
        *,
        connect: Optional[Callable[[str], Awaitable]] = None,
        on_event: Optional[Callable[[protocol.MatterEvent], None]] = None,
        on_connect: Optional[Callable[[], Awaitable]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
        on_repeated_failure: Optional[Callable[[int], None]] = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ) -> None:
        self.proto = proto
        if pref_str(prefs, "serverLocation", "local") == "local":
            # Turnkey local: the plugin runs matter-server here on loopback, so
            # always dial it there and ignore any stale host/port in prefs.
            uri = "ws://localhost:5580/ws"
        else:
            host = pref_str(prefs, "matterServerHost", "localhost")
            port = pref_str(prefs, "matterServerPort", "5580")
            path = pref_str(prefs, "matterServerPath", "/ws")
            uri = f"ws://{host}:{port}{path}"
        super().__init__(
            uri, logger,
            connect=connect, on_connect=on_connect, on_disconnect=on_disconnect,
            on_repeated_failure=on_repeated_failure, now=now, sleep=sleep,
        )
        self._on_event = on_event
        self.server_info: Optional[dict] = None

    # ------------------------------------------------------------------
    # Handshake + frame vocabulary (the WsJsonClient hooks)
    # ------------------------------------------------------------------
    async def _handshake(self, first: Any) -> None:
        # matter-server pushes server_info as a BARE object on connect (no
        # event/message_id wrapper) — verified against ws-controller v0.6.2.
        if isinstance(first, dict):
            self.server_info = first
        self._mark_connected()
        # start_listening returns the full node dump as its result AND enables
        # the event firehose; matter-server streams every attribute_updated with
        # no per-attribute subscription needed (verified against v0.6.2), so the
        # handlers' attributes_to_subscribe() is only a future filtering aid.
        # Sent without awaiting the result: the node dump is re-fetched by the
        # on_connect reconcile, so nothing here needs the response.
        await self._send_frame(self.proto.build_request(protocol.CMD_START_LISTENING))
        # Log the server version so a restart/install can be confirmed to have landed on
        # the intended version (sdk_version e.g. "matter-server/1.2.2 (matter.js/…)").
        version = (self.server_info or {}).get("sdk_version", "version unknown")
        self.logger.info("connected to matter-server (%s), listening", version)

    def _is_event(self, frame: dict) -> bool:
        return self.proto.is_event(frame)

    def _is_response(self, frame: dict) -> bool:
        return self.proto.is_response(frame)

    def _message_id_of(self, frame: dict) -> Optional[str]:
        return self.proto.message_id_of(frame)

    def _parse_result(self, frame: dict) -> Any:
        return self.proto.parse_result(frame)

    def _error_of(self, frame: dict) -> Optional[tuple]:
        if protocol.KEY_ERROR_CODE not in frame:
            return None
        return frame.get(protocol.KEY_ERROR_CODE), str(frame.get(protocol.KEY_ERROR_DETAILS, ""))

    def _handle_event(self, frame: dict) -> None:
        evt = self.proto.parse_event(frame)
        if self._on_event is None:
            return
        try:
            self._on_event(evt)
        except Exception as exc:  # handler must not kill the loop
            # Name the event and its data: which event broke the handler is the
            # whole question, and a bare traceback from inside a callback that
            # takes every event kind cannot answer it.
            self.logger.exception("matter-server event %s failed on %r: %s",
                                  frame.get(protocol.KEY_EVENT, "?"),
                                  frame.get(protocol.KEY_DATA), exc)

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------
    async def request(self, command: str, args: Optional[dict] = None, timeout: float = 10.0) -> Any:
        return await self._request_frame(self.proto.build_request(command, args), timeout)

    async def send_command(self, cmd: MatterCommand, timeout: float = 5.0) -> Any:
        return await self._request_frame(self.proto.build_command(cmd), timeout)

    async def write(self, write: MatterWrite, timeout: float = 5.0) -> Any:
        return await self._request_frame(self.proto.build_write(write), timeout)

    # convenience wrappers
    async def get_nodes(self) -> Any:
        return await self.request(protocol.CMD_GET_NODES)

    async def get_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_GET_NODE, {"node_id": node_id})

    async def commission_with_code(self, code: str, timeout: float = COMMISSION_TIMEOUT) -> Any:
        # network_only=True → IP discovery (mDNS) + PASE/CASE over IP, no BLE.
        # The plugin only ever joins devices that are ALREADY commissioned and on
        # the network (the "share model": Apple Home is admin 1; we join as a
        # second administrator via an open commissioning window). We never do
        # fresh Wi-Fi/Thread onboarding — that is the controlling app's job.
        return await self.request(protocol.CMD_COMMISSION, {"code": code, "network_only": True}, timeout)

    async def remove_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_REMOVE_NODE, {"node_id": node_id})

    async def open_commissioning_window(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_OPEN_WINDOW, {"node_id": node_id})

    async def interview_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_INTERVIEW, {"node_id": node_id})

    async def set_default_fabric_label(self, label: str) -> Any:
        # Persists the label for future commissions AND pushes UpdateFabricLabel
        # to already-commissioned nodes (matter-server calls updateFabricLabel).
        return await self.request(protocol.CMD_SET_FABRIC_LABEL, {"label": label})
