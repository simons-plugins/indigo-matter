"""M1: protocol adapter round-trips, using golden frames from IMPLEMENTATION §2.

These are table-driven so that correcting a wire field name against the pinned
matter-server release is a one-line fixture edit, not a rewrite.
"""
from __future__ import annotations

import pytest

import protocol
from protocol import (
    CommissioningWindow,
    MatterCommand,
    MatterEvent,
    MatterWrite,
    Protocol,
    ProtocolError,
    parse_commissioning_window,
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


def test_build_write_uses_attribute_path(proto):
    w = MatterWrite(node_id=30, endpoint=1, cluster=0x0201, attribute=0x0012, value=2150)
    frame = proto.build_write(w, message_id="5")
    assert frame["message_id"] == "5"
    assert frame["command"] == protocol.CMD_WRITE_ATTR
    args = frame["args"]
    assert args[protocol.ARG_NODE_ID] == 30
    assert args["attribute_path"] == "1/513/18"  # decimal ep/cluster/attribute
    assert args["value"] == 2150


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
    # real shape: data is the node-details object (has node_id)
    frame = {"event": "node_added", "data": {"node_id": 42, "attributes": {}}}
    evt = proto.parse_event(frame)
    assert isinstance(evt, MatterEvent)
    assert evt.kind == protocol.EVT_NODE_ADDED
    assert evt.node_id == 42


def test_parse_attribute_updated_real_array_shape(proto):
    # GOLDEN: verified against ws-controller v0.6.2 — data is [node_id, path, value]
    frame = {"event": "attribute_updated", "data": [42, "1/6/0", True]}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_ATTRIBUTE_UPDATED
    assert (evt.node_id, evt.endpoint, evt.cluster, evt.attribute) == (42, 1, 6, 0)
    assert evt.value is True


def test_parse_attribute_updated_numeric_value(proto):
    frame = {"event": "attribute_updated", "data": [7, "1/1026/0", 2150]}
    evt = proto.parse_event(frame)
    assert (evt.node_id, evt.endpoint, evt.cluster, evt.attribute) == (7, 1, 1026, 0)
    assert evt.value == 2150


def test_parse_node_removed_bare_id(proto):
    # real shape: data is the bare node_id
    frame = {"event": "node_removed", "data": 42}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_REMOVED
    assert evt.node_id == 42


def test_parse_node_updated_extracts_node_id(proto):
    # node_updated carries the full node-details object (used for availability)
    frame = {"event": "node_updated", "data": {"node_id": 42, "available": False, "attributes": {}}}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_UPDATED
    assert evt.node_id == 42


def test_parse_server_shutdown_event(proto):
    evt = proto.parse_event({"event": "server_shutdown", "data": {}})
    assert evt.kind == protocol.EVT_SERVER_SHUTDOWN
    assert evt.node_id is None


def test_parse_malformed_attribute_updated_degrades_to_node_none(proto):
    # a truncated/!list data must not crash; it yields a node_id=None event that
    # device_sync drops (and now logs) rather than misapplying.
    evt = proto.parse_event({"event": "attribute_updated", "data": [42, "1/6"]})
    assert evt.kind == protocol.EVT_ATTRIBUTE_UPDATED
    assert evt.node_id is None and evt.endpoint is None


def test_parse_node_event_hex_string_ids(proto):
    """node_event ids sent as decimal strings or hex strings are coerced to int.

    The wire sends ints in practice, but the codebase's _to_int contract tolerates
    hex strings everywhere (matching how parse_attr_key handles attribute_updated).
    This test asserts that string ids in a node_event frame round-trip correctly.
    """
    frame = {
        "event": "node_event",
        "data": {
            "node_id": "7",          # decimal string
            "endpoint_id": "1",      # decimal string
            "cluster_id": "0x003B",  # hex string
            "event_id": "0x03",      # hex string (ShortRelease)
            "event_number": 42,
            "priority": 1,
            "timestamp": 1700000000,
            "timestamp_type": 1,
            "data": {"previousPosition": 0},
        },
    }
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_EVENT
    assert evt.node_id == 7
    assert evt.endpoint == 1
    assert evt.cluster == 0x003B
    assert evt.event_id == 0x03
    assert evt.event_data == {"previousPosition": 0}


# ---------------------------------------------------------------------------
# is_node_not_exists — the one error code that changes a decision
# ---------------------------------------------------------------------------

def test_node_not_exists_recognised_from_the_numeric_code():
    assert protocol.is_node_not_exists(protocol.ProtocolError(5, "Node 38 does not exist"))


def test_node_not_exists_recognised_when_the_code_arrives_as_a_string():
    # ProtocolError.code is whatever the JSON carried, so it may not be an int.
    assert protocol.is_node_not_exists(protocol.ProtocolError("5", "Node 38 does not exist"))


def test_other_error_codes_are_not_node_not_exists():
    # Fails CLOSED: misreading a real failure as "already gone" would forget a node
    # that is still commissioned, which is worse than making the user retry.
    for code in (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 100):
        assert not protocol.is_node_not_exists(protocol.ProtocolError(code, "boom"))


def test_unreadable_or_absent_codes_are_not_node_not_exists():
    assert not protocol.is_node_not_exists(protocol.ProtocolError(None, "no code"))
    assert not protocol.is_node_not_exists(protocol.ProtocolError("not-a-number", "junk"))
    assert not protocol.is_node_not_exists(ConnectionError("offline"))   # no .code at all


def test_node_not_exists_recognised_from_a_hex_string_code():
    # _to_int parses base-0, so "0x05" is as valid on the wire as 5 or "5".
    assert protocol.is_node_not_exists(protocol.ProtocolError("0x05", "does not exist"))


# ---------------------------------------------------------------------------
# _to_int — plain int first, base-0 fallback (issue #210 review)
# ---------------------------------------------------------------------------

def test_to_int_accepts_a_zero_padded_decimal_string():
    # int(x, 0) — the obvious one-liner — RAISES on "03": base-0 parsing
    # requires a leading zero to carry an "0x"/"0o"/"0b" prefix. Plain
    # int("03") has no such restriction, so it is tried first.
    assert protocol._to_int("03") == 3
    assert protocol._to_int("04") == 4


def test_to_int_still_understands_hex_strings():
    assert protocol._to_int("0x05") == 5
    assert protocol._to_int(5) == 5


# ---------------------------------------------------------------------------
# is_node_unreachable — NodeNotReady(3)/NodeNotResolving(4) (issue #210)
# ---------------------------------------------------------------------------

def test_node_unreachable_recognised_from_the_numeric_codes():
    assert protocol.is_node_unreachable(protocol.ProtocolError(3, "not ready"))
    assert protocol.is_node_unreachable(protocol.ProtocolError(4, "not resolving"))


def test_node_unreachable_recognised_from_zero_padded_string_codes():
    # The wire-code coercion bug this fixes, applied to the codes it actually
    # gates a decision on: "03"/"04" must not be misread as "unrecognised".
    assert protocol.is_node_unreachable(protocol.ProtocolError("03", "not ready"))
    assert protocol.is_node_unreachable(protocol.ProtocolError("04", "not resolving"))


def test_node_unreachable_false_for_other_codes():
    # Fails CLOSED, like is_node_not_exists — a code we cannot read as 3/4 is
    # never guessed to be one.
    for code in (0, 1, 2, 5, 6, 7, 8, 9, 10, 11, 100):
        assert not protocol.is_node_unreachable(protocol.ProtocolError(code, "boom"))
    assert not protocol.is_node_unreachable(protocol.ProtocolError(None, "no code"))
    assert not protocol.is_node_unreachable(ConnectionError("offline"))


# ---------------------------------------------------------------------------
# parse_commissioning_window (issue #210) — open_commissioning_window's result,
# VERIFIED snake_case against matter-server v1.2.2 (module header), camelCase
# fallback kept only because source-verified is not wire-verified.
# ---------------------------------------------------------------------------

def test_parse_commissioning_window_snake_case():
    window = parse_commissioning_window({
        "setup_pin_code": 12345678,
        "setup_manual_code": "34970112332",
        "setup_qr_code": "MT:-24J0AFN00KA0648G00",
    })
    assert isinstance(window, CommissioningWindow)
    assert window.manual_code == "34970112332"
    assert window.qr_code == "MT:-24J0AFN00KA0648G00"
    assert window.pin_code == 12345678


def test_parse_commissioning_window_camel_case_fallback():
    # Defensive only — 1.2.2 does not send this shape, but source-verified is
    # not the same as wire-verified against whatever is actually running.
    window = parse_commissioning_window({
        "manualPairingCode": "34970112332",
        "qrPairingCode": "MT:-24J0AFN00KA0648G00",
    })
    assert window.manual_code == "34970112332"
    assert window.qr_code == "MT:-24J0AFN00KA0648G00"


def test_parse_commissioning_window_missing_manual_code_raises_naming_payload():
    payload = {"setup_qr_code": "MT:-24J0AFN00KA0648G00"}
    with pytest.raises(ValueError) as exc:
        parse_commissioning_window(payload)
    assert "manual" in str(exc.value).lower()
    assert repr(payload) in str(exc.value)


def test_parse_commissioning_window_missing_qr_alone_succeeds():
    # Only the manual code is required — there is a manual-entry path in every
    # ecosystem's app, but no QR-only one, so a missing QR degrades rather than
    # failing the whole share.
    window = parse_commissioning_window({"setup_manual_code": "34970112332"})
    assert window.manual_code == "34970112332"
    assert window.qr_code is None
    assert window.pin_code is None


def test_parse_commissioning_window_ignores_unknown_extra_keys():
    window = parse_commissioning_window({
        "setup_manual_code": "34970112332",
        "discriminator": 3840,
        "some_future_field": "whatever",
    })
    assert window.manual_code == "34970112332"
