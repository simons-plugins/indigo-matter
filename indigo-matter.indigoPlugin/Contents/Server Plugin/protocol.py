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
# Wire field names. VERIFIED against the matter-server v0.6.2 source on jarvis
# (@matter-server/ws-controller/src/server/WebSocketControllerHandler.ts):
#   - device_command args: node_id / endpoint_id / cluster_id / command_name /
#     payload  (server camelizes command_name, so "On"/"Off"/"Toggle" work).
#   - success response: {message_id, result}; error: {message_id, error_code,
#     details}; event: {event, data}.
#   - start_listening returns the node dump as its result AND turns on the event
#     firehose (no per-attribute subscription needed).
#   - server_info is pushed as a BARE object on connect (no event/message_id).
#   - attribute_updated data is [node_id, "ep/cl/at", value]; node_removed data
#     is a bare node_id; node_added/updated data is the node-details object.
#   - node_event data is a MatterNodeEvent object (verified from
#     @matter-server/ws-client/dist/esm/models/model.d.ts):
#       {node_id, endpoint_id, cluster_id, event_id, event_number,
#        priority, timestamp, timestamp_type, data}
#     The inner `data` payload is converted via convertMatterToWebSocketNameBased
#     (camelCase field names). For GenericSwitch cluster (0x003B):
#       InitialPress (0x01): data = {newPosition: int}
#       LongPress    (0x02): data = {newPosition: int}
#       ShortRelease (0x03): data = {previousPosition: int}
#       MultiPressComplete (0x06): data = {previousPosition: int,
#                                          totalNumberOfPressesCounted: int}
# If a future release changes these, this module is still the only place to edit.
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
EVT_NODE_EVENT = "node_event"


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
class MatterWrite:
    """A Matter attribute write (e.g. a thermostat setpoint or mode).

    Some Matter operations are attribute writes, not cluster commands —
    thermostats set setpoints/modes by writing OccupiedHeatingSetpoint /
    SystemMode etc. rather than invoking a command.
    """
    node_id: int
    endpoint: int
    cluster: int
    attribute: int
    value: Any


# node_event MatterNodeEvent object field names (wire-level; rename-firewall)
EVT_NODE_EVENT_NODE_ID     = "node_id"
EVT_NODE_EVENT_ENDPOINT_ID = "endpoint_id"
EVT_NODE_EVENT_CLUSTER_ID  = "cluster_id"
EVT_NODE_EVENT_EVENT_ID    = "event_id"
EVT_NODE_EVENT_DATA        = "data"


@dataclass
class MatterEvent:
    """A normalised inbound event from matter-server."""
    kind: str
    node_id: Optional[int] = None
    endpoint: Optional[int] = None
    cluster: Optional[int] = None
    attribute: Optional[int] = None
    value: Any = None
    # Populated for EVT_NODE_EVENT frames (cluster events — button presses,
    # lock operations, etc.) — None for all other event kinds.
    event_id: Optional[int] = None
    event_data: Any = None
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

    def build_write(self, write: MatterWrite, message_id: Optional[str] = None) -> dict:
        # write_attribute args: node_id + attribute_path ("ep/cluster/attribute") + value
        return self.build_request(
            CMD_WRITE_ATTR,
            {
                ARG_NODE_ID: write.node_id,
                "attribute_path": self.attr_key(write.endpoint, write.cluster, write.attribute),
                "value": write.value,
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
        """Normalise an inbound event to a :class:`MatterEvent`.

        Real matter-server v0.6.2 event ``data`` shapes:
          - attribute_updated: ``[node_id, "endpoint/cluster/attribute", value]``
          - node_removed:      bare ``node_id``
          - node_added / node_updated: the node-details object (has ``node_id``)
          - node_event:        MatterNodeEvent dict — see wire-shape comment block
                               at the top of this module for the verified shape.
          - others (endpoint_*): a dict with ``node_id``
        """
        name = frame.get(KEY_EVENT, "")
        data = frame.get(KEY_DATA)
        node_id = endpoint = cluster = attribute = value = None
        event_id = event_data = None

        if name == EVT_ATTRIBUTE_UPDATED and isinstance(data, (list, tuple)) and len(data) >= 3:
            node_id = data[0]
            endpoint, cluster, attribute = self.parse_attr_key(str(data[1]))
            value = data[2]
        elif name == EVT_NODE_REMOVED:
            node_id = data if not isinstance(data, dict) else data.get("node_id")
        elif name == EVT_NODE_EVENT and isinstance(data, dict):
            # MatterNodeEvent: {node_id, endpoint_id, cluster_id, event_id, data, ...}
            # Wire field names are isolated to the EVT_NODE_EVENT_* constants above.
            # Coerce integer fields defensively through _to_int (None-safe) so that
            # hex-string ids ("0x003B") are tolerated everywhere, matching how the
            # attribute_updated branch coerces via parse_attr_key.
            _raw_node_id  = data.get(EVT_NODE_EVENT_NODE_ID)
            _raw_endpoint = data.get(EVT_NODE_EVENT_ENDPOINT_ID)
            _raw_cluster  = data.get(EVT_NODE_EVENT_CLUSTER_ID)
            _raw_event_id = data.get(EVT_NODE_EVENT_EVENT_ID)
            node_id   = _to_int(_raw_node_id)  if _raw_node_id  is not None else None
            endpoint  = _to_int(_raw_endpoint) if _raw_endpoint is not None else None
            cluster   = _to_int(_raw_cluster)  if _raw_cluster  is not None else None
            event_id  = _to_int(_raw_event_id) if _raw_event_id is not None else None
            event_data = data.get(EVT_NODE_EVENT_DATA)
        elif isinstance(data, dict):
            node_id = data.get("node_id")

        return MatterEvent(
            kind=name,
            node_id=node_id,
            endpoint=endpoint,
            cluster=cluster,
            attribute=attribute,
            value=value,
            event_id=event_id,
            event_data=event_data,
            raw=frame,
        )
