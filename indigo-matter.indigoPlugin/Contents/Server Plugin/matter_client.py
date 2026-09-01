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

# A live attribute read/write against a SLEEPY device is seconds, not
# milliseconds: a Thread SED only hears us when it next polls its parent, and a
# sibling IKEA sleepy device on the same mesh was measured polling at ~9.5s
# intervals (issue #186 research). The 5s COMMAND_TIMEOUT the control path uses
# is therefore too short to be evidence of anything — it would report a healthy
# device as failed. Used by BOTH legs of the settings write/verify path
# (device_settings.apply_setting) — the only caller that must distinguish "the
# device said no" from "the device is asleep". The write leg used to fall
# through to write()'s 5s default while only the read got this, reporting
# healthy hardware as "could not be sent": the same false verdict, inverted.
ATTRIBUTE_TIMEOUT = 30.0

# open_commissioning_window's RPC deadline (issue #210) — deliberately its own
# constant, not COMMAND_TIMEOUT's 5s default: matter-server derives a fresh
# passcode and re-advertises, real work against a device that may be a sleepy
# Thread node, and the plugin's own SHARE_WINDOW_TIMEOUT (plugin_constants.py)
# wraps this one and must stay strictly above it — see that constant's
# docstring for why the ordering matters.
SHARE_WINDOW_RPC_TIMEOUT = 45.0


class MatterClient(WsJsonClient):
    """A reconnecting matter-server WebSocket client."""

    PEER = "matter-server"

    # The callbacks ARE the constructor's arity — same trade BridgeClient makes.
    # pylint: disable=too-many-arguments,too-many-locals
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
        on_late_response: Optional[Callable[[Any], None]] = None,
        outage_expected: Optional[Callable[[], bool]] = None,
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
            on_repeated_failure=on_repeated_failure, on_late_response=on_late_response,
            outage_expected=outage_expected, now=now, sleep=sleep,
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
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: anything on_event raises — it fans into the whole DeviceSync
            # update path, so the failure modes are open-ended. Loop protection here
            # is redundant with ws_json_client's frame catch; the reason this exists
            # is that only this frame still knows WHICH event and payload broke, and a
            # bare traceback from a callback taking every event kind cannot say.
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

    async def read(self, node_id: int, endpoint: int, cluster: int, attribute: int,
                   timeout: float = ATTRIBUTE_TIMEOUT) -> Any:
        """Read ONE attribute live from the device.

        The counterpart to :meth:`write`, and the thing ``get_node`` is not:
        ``get_node`` returns matter-server's **cache**, which for an unsubscribed
        attribute can be hours stale (proven live in the issue #186 session — a
        sleepy device's cached entry named a parent router that no longer
        existed, while this call returned the current value). Anything a decision
        depends on — above all a write's read-back verification — has to come
        through here.

        ``CMD_READ_ATTR`` has been defined in :mod:`protocol` since the first
        build with no caller; the arg shape below is live-verified against
        matter-server 1.2.2.
        """
        path = self.proto.attr_key(endpoint, cluster, attribute)
        result = await self.request(
            protocol.CMD_READ_ATTR,
            {protocol.ARG_NODE_ID: node_id, "attribute_path": path},
            timeout=timeout,
        )
        return self._unwrap_attribute(result, path)

    @staticmethod
    def _unwrap_attribute(result: Any, path: str) -> Any:
        """Pull the value out of read_attribute's path-keyed result.

        matter-server answers a single-attribute read with a DICT keyed by the
        requested path (``{"0/53/7": …}``), not the bare value — live-verified
        against 1.2.2.

        The unwrap is deliberately narrow rather than "if it's a dict of one,
        take the value", because plenty of attributes ARE dicts: OccupancySensing's
        HoldTimeLimits reads as ``{"0": 1, "1": 300, "2": 10}`` — a struct keyed
        by FIELD INDEX, which is how matter-server serialises every struct — and
        a loose unwrap would hand back ``1`` for a one-key variant of that. (Do
        not restate that attribute as ``{"min":…,"max":…}``: that rendering is
        prose from the issue #186 research doc about what the fields MEAN, and
        coding against it shipped a hold-time field that never appeared.) So: the
        exact path wins; failing that, a lone key that looks like an attribute
        path (matter-server has formatted these differently across versions)
        wins; anything else is returned untouched, on the grounds that a server
        which starts replying with the bare value should keep working here.
        """
        if not isinstance(result, dict):
            return result
        if path in result:
            return result[path]
        if len(result) == 1:
            (only_key, only_value), = result.items()
            if isinstance(only_key, str) and only_key.count("/") == 2:
                return only_value
        return result

    # convenience wrappers
    async def get_nodes(self) -> Any:
        return await self.request(protocol.CMD_GET_NODES)

    async def get_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_GET_NODE, {"node_id": node_id})

    async def commission_with_code(self, code: str, timeout: float = COMMISSION_TIMEOUT,
                                   context: Any = None) -> Any:
        # network_only=True → IP discovery (mDNS) + PASE/CASE over IP, no BLE.
        # The plugin only ever joins devices that are ALREADY commissioned and on
        # the network (the "share model": Apple Home is admin 1; we join as a
        # second administrator via an open commissioning window). We never do
        # fresh Wi-Fi/Thread onboarding — that is the controlling app's job.
        # Built explicitly (not via self.request) because commission is the one
        # RPC a caller correlates a LATE answer against (#23) — request() has no
        # context param, and this is COMMISSION_TIMEOUT's one caller anyway.
        frame = self.proto.build_request(protocol.CMD_COMMISSION, {"code": code, "network_only": True})
        return await self._request_frame(frame, timeout, context=context or "commission_with_code")

    async def remove_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_REMOVE_NODE, {"node_id": node_id})

    async def open_commissioning_window(self, node_id: int, duration: Optional[int] = None,
                                        timeout: float = SHARE_WINDOW_RPC_TIMEOUT,
                                        context: Any = None) -> Any:
        """Open an Enhanced commissioning window on an already-commissioned node
        (issue #210) — the "share this device with another ecosystem" primitive.

        ``duration`` is the window's lifetime in SECONDS; omit it and
        matter-server defaults to 900s (matter.js's own default). It is sent
        under the wire name ``protocol.ARG_WINDOW_DURATION`` ("timeout"), which
        is NOT the same thing as this method's own ``timeout`` parameter — that
        one is the ordinary RPC deadline every other wrapper here takes. Naming
        them identically on the wire while they mean opposite things is exactly
        the trap ``protocol.py``'s module header calls out; do not let a future
        edit wire one into the other.

        Built explicitly (not via :meth:`request`) for the same reason
        :meth:`commission_with_code` is: this is a caller that wants to
        correlate a LATE answer (``context``) against the request that sent
        it — ``request()`` has no context param. A device that misses its
        own RPC deadline (a sleepy Thread node) can still answer after this
        plugin gives up waiting; without a context, that answer's code dies
        in :meth:`WsJsonClient._log_unmatched` as a payload-free debug line.
        ``MatterServerMenuMixin._note_late_share_response`` is the consumer.
        """
        args = {protocol.ARG_NODE_ID: node_id}
        if duration is not None:
            args[protocol.ARG_WINDOW_DURATION] = int(duration)
        frame = self.proto.build_request(protocol.CMD_OPEN_WINDOW, args)
        return await self._request_frame(frame, timeout, context=context)

    async def interview_node(self, node_id: int) -> Any:
        return await self.request(protocol.CMD_INTERVIEW, {"node_id": node_id})

    async def set_default_fabric_label(self, label: str) -> Any:
        # Persists the label for future commissions AND pushes UpdateFabricLabel
        # to already-commissioned nodes (matter-server calls updateFabricLabel).
        return await self.request(protocol.CMD_SET_FABRIC_LABEL, {"label": label})
