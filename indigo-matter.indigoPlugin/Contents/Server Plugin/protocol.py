"""matter-server WebSocket protocol adapter — the rename firewall.

matter-server speaks a python-matter-server-compatible WebSocket protocol. The
docs disagree on exact field names (IMPLEMENTATION.md §2 uses
``device_command {node_id, endpoint, cluster, command, args}``; the canonical
python-matter-server doc uses ``{node_id, endpoint_id, cluster_id,
command_name, payload}`` and a single ``attribute_path`` string). **All of these
are assumed unverified until checked against the pinned matter-server release's
``docs/websockets_api.md`` at the start of M2.**

This module is the ONLY place that knows the wire field names. Everything else
(``matter_client``, ``device_sync``, the cluster handlers) deals only in
normalised :class:`MatterCommand` / :class:`MatterEvent` objects, so a
server-side rename is a one-line change to the constants below plus one
golden-frame fixture update — nothing else in the codebase moves.

No Indigo dependency — fully unit-testable in isolation.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------------
# Wire field names — change HERE and nowhere else if the pinned release differs.
# Defaults follow the canonical python-matter-server WebSocket API, since
# matter-server advertises drop-in compatibility with it.
# --------------------------------------------------------------------------

# Envelope
KEY_MESSAGE_ID = "message_id"
KEY_COMMAND = "command"
KEY_ARGS = "args"
KEY_RESULT = "result"
KEY_ERROR_CODE = "error_code"
KEY_ERROR_DETAILS = "details"
KEY_EVENT = "event"
KEY_DATA = "data"

# Commands the plugin sends
CMD_SERVER_INFO = "server_info"
CMD_GET_NODES = "get_nodes"
CMD_GET_NODE = "get_node"
CMD_COMMISSION = "commission_with_code"
CMD_OPEN_WINDOW = "open_commissioning_window"
CMD_INTERVIEW = "interview_node"
CMD_REMOVE_NODE = "remove_node"
CMD_READ_ATTR = "read_attribute"
CMD_WRITE_ATTR = "write_attribute"
CMD_DEVICE = "device_command"            # invoke a cluster command
CMD_START_LISTENING = "start_listening"

# device_command argument keys (the most contested names — see module docstring)
ARG_NODE_ID = "node_id"
ARG_ENDPOINT = "endpoint_id"             # IMPLEMENTATION.md used "endpoint"
ARG_CLUSTER = "cluster_id"               # IMPLEMENTATION.md used "cluster"
ARG_COMMAND = "command_name"             # IMPLEMENTATION.md used "command"
ARG_PAYLOAD = "payload"                  # IMPLEMENTATION.md used "args"

# Event names the plugin handles
EVT_NODE_ADDED = "node_added"
EVT_NODE_UPDATED = "node_updated"
EVT_NODE_REMOVED = "node_removed"
EVT_ATTRIBUTE_UPDATED = "attribute_updated"
EVT_SERVER_SHUTDOWN = "server_shutdown"


@dataclass
class MatterCommand:
    """A Matter cluster command to send via matter-server.

    Cluster handlers emit these; ``Protocol.build_command`` turns them into the
    wire frame. ``cluster`` is the numeric cluster id; ``command`` is the Matter
    command name (e.g. ``"On"``); ``args`` is the command payload.
    """
    node_id: int
    endpoint: int
    cluster: int
    command: str
    args: dict = field(default_factory=dict)


@dataclass
class MatterEvent:
    """A normalised inbound event from matter-server."""
    kind: str
    node_id: Optional[int] = None
    endpoint: Optional[int] = None
    cluster: Optional[int] = None
    attribute: Optional[int] = None
    value: Any = None
    raw: dict = field(default_factory=dict)


class ProtocolError(Exception):
    """Raised when a response frame carries an error_code."""

    def __init__(self, code: Any, details: str = "") -> None:
        super().__init__(f"matter-server error {code}: {details}")
        self.code = code
        self.details = details


def _to_int(value: Any) -> int:
    """Parse an int that may be decimal or a hex string like '0x0006'."""
    if isinstance(value, int):
        return value
    return int(str(value), 0)


class Protocol:
    """Builds outbound frames and parses inbound ones."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def next_id(self) -> str:
        return str(next(self._ids))

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------
    def build_request(self, command: str, args: Optional[dict] = None,
                      message_id: Optional[str] = None) -> dict:
        return {
            KEY_MESSAGE_ID: message_id or self.next_id(),
            KEY_COMMAND: command,
            KEY_ARGS: dict(args) if args else {},
        }

    def build_command(self, cmd: MatterCommand, message_id: Optional[str] = None) -> dict:
        return self.build_request(
            CMD_DEVICE,
            {
                ARG_NODE_ID: cmd.node_id,
                ARG_ENDPOINT: cmd.endpoint,
                ARG_CLUSTER: cmd.cluster,
                ARG_COMMAND: cmd.command,
                ARG_PAYLOAD: dict(cmd.args),
            },
            message_id,
        )

    @staticmethod
    def attr_key(endpoint: int, cluster: int, attribute: int) -> str:
        """The ``endpoint/cluster/attribute`` subscription/path key (decimal)."""
        return f"{int(endpoint)}/{int(cluster)}/{int(attribute)}"

    @staticmethod
    def parse_attr_key(key: str) -> tuple[int, int, int]:
        endpoint, cluster, attribute = key.split("/")
        return _to_int(endpoint), _to_int(cluster), _to_int(attribute)

    # ------------------------------------------------------------------
    # Inbound classification
    # ------------------------------------------------------------------
    @staticmethod
    def is_event(frame: dict) -> bool:
        return KEY_EVENT in frame

    @staticmethod
    def is_response(frame: dict) -> bool:
        return KEY_EVENT not in frame and KEY_MESSAGE_ID in frame

    @staticmethod
    def message_id_of(frame: dict) -> Optional[str]:
        return frame.get(KEY_MESSAGE_ID)

    # ------------------------------------------------------------------
    # Inbound parsing
    # ------------------------------------------------------------------
    def parse_result(self, frame: dict) -> Any:
        """Return the result payload, or raise :class:`ProtocolError`."""
        if KEY_ERROR_CODE in frame:
            raise ProtocolError(frame.get(KEY_ERROR_CODE), str(frame.get(KEY_ERROR_DETAILS, "")))
        return frame.get(KEY_RESULT)

    def parse_event(self, frame: dict) -> MatterEvent:
        name = frame.get(KEY_EVENT, "")
        data = frame.get(KEY_DATA, {}) or {}
        node_id = data.get("node_id")
        endpoint = cluster = attribute = value = None
        if name == EVT_ATTRIBUTE_UPDATED:
            endpoint, cluster, attribute = self._extract_attr_path(data)
            value = data.get("value")
        return MatterEvent(
            kind=name,
            node_id=node_id,
            endpoint=endpoint,
            cluster=cluster,
            attribute=attribute,
            value=value,
            raw=frame,
        )

    def _extract_attr_path(self, data: dict) -> tuple[Optional[int], Optional[int], Optional[int]]:
        """Pull endpoint/cluster/attribute from an attribute_updated payload.

        Tolerates both a single ``"ep/cl/at"`` path string (under any of a few
        documented key names) and discrete fields.
        """
        path = data.get("attribute") or data.get("attribute_path") or data.get("key")
        if isinstance(path, str) and "/" in path:
            return self.parse_attr_key(path)
        endpoint = data.get("endpoint", data.get("endpoint_id"))
        cluster = data.get("cluster", data.get("cluster_id"))
        attribute = data.get("attribute_id")
        return (
            _to_int(endpoint) if endpoint is not None else None,
            _to_int(cluster) if cluster is not None else None,
            _to_int(attribute) if attribute is not None else None,
        )
