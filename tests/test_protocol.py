"""M1: protocol adapter round-trips, using golden frames from IMPLEMENTATION §2.

These are table-driven so that correcting a wire field name against the pinned
matter-server release is a one-line fixture edit, not a rewrite.
"""
from __future__ import annotations

import pytest

import protocol
from protocol import (
    MatterCommand,
    MatterEvent,
    Protocol,
    ProtocolError,
)


@pytest.fixture
def proto():
    return Protocol()


# ----------------------------------------------------------------------
# Outbound: requests and commands
# ----------------------------------------------------------------------
def test_build_request_envelope(proto):
    frame = proto.build_request(protocol.CMD_GET_NODE, {"node_id": 42}, message_id="7")
    assert frame == {"message_id": "7", "command": "get_node", "args": {"node_id": 42}}


def test_build_request_auto_increments_message_id(proto):
    a = proto.build_request(protocol.CMD_GET_NODES)
    b = proto.build_request(protocol.CMD_GET_NODES)
    assert a["message_id"] != b["message_id"]
    assert a["args"] == {}


def test_build_command_maps_matter_command_to_wire_fields(proto):
    cmd = MatterCommand(node_id=42, endpoint=1, cluster=0x0006, command="On", args={})
    frame = proto.build_command(cmd, message_id="3")
    assert frame["message_id"] == "3"
    assert frame["command"] == protocol.CMD_DEVICE
    args = frame["args"]
    assert args[protocol.ARG_NODE_ID] == 42
    assert args[protocol.ARG_ENDPOINT] == 1
    assert args[protocol.ARG_CLUSTER] == 0x0006
    assert args[protocol.ARG_COMMAND] == "On"
    assert args[protocol.ARG_PAYLOAD] == {}


# ----------------------------------------------------------------------
# Attribute key helpers
# ----------------------------------------------------------------------
@pytest.mark.parametrize("ep,cluster,attr,expected", [
    (1, 6, 0, "1/6/0"),
    (1, 0x0006, 0x0000, "1/6/0"),
    (2, 0x0402, 0, "2/1026/0"),
])
def test_attr_key(proto, ep, cluster, attr, expected):
    assert proto.attr_key(ep, cluster, attr) == expected


@pytest.mark.parametrize("key,expected", [
    ("1/6/0", (1, 6, 0)),
    ("1/0x0006/0x0000", (1, 6, 0)),  # hex components tolerated
])
def test_parse_attr_key(proto, key, expected):
    assert proto.parse_attr_key(key) == expected


# ----------------------------------------------------------------------
# Inbound classification
# ----------------------------------------------------------------------
def test_classify_response_vs_event(proto):
    response = {"message_id": "2", "result": {"node_id": 42}}
    event = {"event": "node_added", "data": {"node_id": 42}}
    assert proto.is_response(response) and not proto.is_event(response)
    assert proto.is_event(event) and not proto.is_response(event)
    assert proto.message_id_of(response) == "2"


# ----------------------------------------------------------------------
# Inbound results (IMPLEMENTATION §2.2 / §2.5)
# ----------------------------------------------------------------------
def test_parse_result_returns_payload(proto):
    # from §2.5: commission_with_code reply
    frame = {"message_id": "1", "result": {"node_id": 42}}
    assert proto.parse_result(frame) == {"node_id": 42}


def test_parse_result_none_result(proto):
    # from §2.5: device_command reply is result: null
    assert proto.parse_result({"message_id": "3", "result": None}) is None


def test_parse_result_raises_on_error_frame(proto):
    frame = {"message_id": "9", "error_code": 50, "details": "PASE failed"}
    with pytest.raises(ProtocolError) as exc:
        proto.parse_result(frame)
    assert exc.value.code == 50
    assert "PASE failed" in exc.value.details


# ----------------------------------------------------------------------
# Inbound events
# ----------------------------------------------------------------------
def test_parse_node_added_event(proto):
    frame = {"event": "node_added", "data": {"node_id": 42}}
    evt = proto.parse_event(frame)
    assert isinstance(evt, MatterEvent)
    assert evt.kind == protocol.EVT_NODE_ADDED
    assert evt.node_id == 42


def test_parse_attribute_updated_with_path_string(proto):
    frame = {"event": "attribute_updated",
             "data": {"node_id": 42, "attribute": "1/6/0", "value": True}}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_ATTRIBUTE_UPDATED
    assert (evt.node_id, evt.endpoint, evt.cluster, evt.attribute) == (42, 1, 6, 0)
    assert evt.value is True


def test_parse_attribute_updated_with_discrete_fields(proto):
    frame = {"event": "attribute_updated",
             "data": {"node_id": 7, "endpoint": 1, "cluster": 1026,
                      "attribute_id": 0, "value": 2150}}
    evt = proto.parse_event(frame)
    assert (evt.node_id, evt.endpoint, evt.cluster, evt.attribute) == (7, 1, 1026, 0)
    assert evt.value == 2150
