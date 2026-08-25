"""Tests for GenericSwitchHandler (cluster 0x003B) and the node_event pipeline.

Covers:
- Protocol: node_event golden frame parsing, malformed frame handling
- DeviceSync: EVT_NODE_EVENT routing (active gate, unknown cluster, handler
  exception isolation)
- GenericSwitchHandler.on_node_event: all event mappings, multi-press counts,
  pressCount increment, InitialPress suppression
- GenericSwitchHandler.create_indigo_devices spec shape
- GenericSwitchHandler.on_attribute_update: returns {} (transient CurrentPosition)
- Registry registration and cluster lookup
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import protocol
from protocol import MatterEvent, Protocol
from matter_handlers.generic_switch import (
    ATTR_CURRENT_POSITION,
    ATTR_FEATURE_MAP,
    CLUSTER_SWITCH,
    EVT_INITIAL_PRESS,
    EVT_LONG_PRESS,
    EVT_MULTI_PRESS_COMPLETE,
    EVT_SHORT_RELEASE,
    FEATURE_LATCHING_SWITCH,
    FEATURE_MOMENTARY_LONG_PRESS,
    FEATURE_MOMENTARY_MULTI_PRESS,
    FEATURE_MOMENTARY_RELEASE,
    FEATURE_MOMENTARY_SWITCH,
    GenericSwitchHandler,
    PROP_SWITCH_FEATURES,
    maps_initial_press,
    switch_features,
)
from matter_handlers.registry import HandlerRegistry
from indigo_fakes import FakeDev, FakeDeviceFactory, FakeDevices, FakeFolderFactory


# ---------------------------------------------------------------------------
# Protocol: node_event frame parsing
# ---------------------------------------------------------------------------

def _proto():
    return Protocol()


# GOLDEN FRAME — verified from matter-server v0.6.2 source:
# @matter-server/ws-controller/src/server/WebSocketControllerHandler.ts
# (eventChanged handler, lines 209-257):
#   ws.send(toBigIntAwareJson({ event: "node_event", data: nodeEvent }))
# where nodeEvent = {node_id, endpoint_id, cluster_id, event_id,
#                    event_number, priority, timestamp, timestamp_type, data}
# and `data` is convertMatterToWebSocketNameBased output (camelCase field names).
# For MultiPressComplete the inner data field is {previousPosition: int,
#   totalNumberOfPressesCounted: int} (from @matter/types switch.d.ts).
GOLDEN_NODE_EVENT_FRAME = {
    "event": "node_event",
    "data": {
        "node_id": 7,
        "endpoint_id": 1,
        "cluster_id": 0x003B,
        "event_id": EVT_SHORT_RELEASE,
        "event_number": 42,
        "priority": 1,
        "timestamp": 1700000000,
        "timestamp_type": 1,
        "data": {"previousPosition": 0},
    },
}


def test_parse_node_event_golden_frame():
    """Golden frame: all fields populated correctly from the verified wire shape."""
    proto = _proto()
    evt = proto.parse_event(GOLDEN_NODE_EVENT_FRAME)
    assert evt.kind == protocol.EVT_NODE_EVENT
    assert evt.node_id == 7
    assert evt.endpoint == 1
    assert evt.cluster == 0x003B
    assert evt.event_id == EVT_SHORT_RELEASE
    assert evt.event_data == {"previousPosition": 0}
    assert evt.value is None  # not populated for node_event
    assert evt.attribute is None


def test_parse_node_event_multi_press_complete():
    """MultiPressComplete: event_data carries totalNumberOfPressesCounted."""
    proto = _proto()
    frame = {
        "event": "node_event",
        "data": {
            "node_id": 3,
            "endpoint_id": 2,
            "cluster_id": 0x003B,
            "event_id": EVT_MULTI_PRESS_COMPLETE,
            "event_number": 10,
            "priority": 1,
            "timestamp": 1700000001,
            "timestamp_type": 1,
            "data": {"previousPosition": 0, "totalNumberOfPressesCounted": 3},
        },
    }
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_EVENT
    assert evt.event_id == EVT_MULTI_PRESS_COMPLETE
    assert evt.event_data["totalNumberOfPressesCounted"] == 3


def test_parse_node_event_null_inner_data():
    """data field may be null (spec allows it for events with no payload)."""
    proto = _proto()
    frame = {
        "event": "node_event",
        "data": {
            "node_id": 1,
            "endpoint_id": 1,
            "cluster_id": 0x003B,
            "event_id": EVT_INITIAL_PRESS,
            "event_number": 1,
            "priority": 1,
            "timestamp": 0,
            "timestamp_type": 0,
            "data": None,
        },
    }
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_EVENT
    assert evt.event_data is None


def test_parse_node_event_malformed_non_dict_data():
    """Non-dict data in a node_event frame: degrades to node_id=None."""
    proto = _proto()
    frame = {"event": "node_event", "data": "unexpected-string"}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_EVENT
    # Non-dict falls into the generic dict branch and doesn't match node_id key
    assert evt.node_id is None
    assert evt.endpoint is None
    assert evt.cluster is None


def test_parse_node_event_missing_fields():
    """Partial node_event dict: missing keys produce None, not KeyError."""
    proto = _proto()
    frame = {
        "event": "node_event",
        "data": {"node_id": 5},  # missing endpoint_id, cluster_id, event_id
    }
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_NODE_EVENT
    assert evt.node_id == 5
    assert evt.endpoint is None
    assert evt.cluster is None
    assert evt.event_id is None


def test_evt_node_event_constant_value():
    assert protocol.EVT_NODE_EVENT == "node_event"


@pytest.fixture
def indigo_env(mock_indigo_base):
    indigo = mock_indigo_base
    devices = FakeDevices()
    indigo.devices = devices
    indigo.devices.folder = FakeFolderFactory(devices)
    indigo.device = FakeDeviceFactory(devices)
    indigo.kProtocol = SimpleNamespace(Plugin="plugin")
    return indigo, devices


@pytest.fixture
def ds(indigo_env, mock_logger):
    import device_sync
    importlib.reload(device_sync)
    reg = HandlerRegistry()
    return device_sync.DeviceSync(reg, mock_logger)


# Minimal Switch node: endpoint 1 exposes cluster 0x003B (GenericSwitch)
SWITCH_NODE = {
    "node_id": 10,
    "available": True,
    "attributes": {
        "0/40/1": "IKEA",
        "0/40/3": "SOMRIG",
        "1/59/1": 0,          # CurrentPosition in cluster 0x003B (decimal 59)
        "1/29/0": [{"0": 15, "1": 2}],  # Descriptor.DeviceTypeList (no cluster filter in parse)
    },
}


# ---------------------------------------------------------------------------
# DeviceSync: node_event routing
# ---------------------------------------------------------------------------

def _make_button_dev(devices, dev_id=3001, node_id=10, endpoint_id=1):
    dev = FakeDev(
        dev_id, "IKEA Button", "matterButton",
        props={"nodeId": str(node_id), "endpointId": str(endpoint_id)},
        initial_states={"lastButtonEvent": "", "pressCount": 0},
    )
    devices.add(dev)
    return dev


def test_node_event_routes_to_handler(ds, indigo_env, mock_logger):
    """A well-formed node_event for a known device updates Indigo states."""
    _indigo, devices = indigo_env
    dev = _make_button_dev(devices)
    ds._index[(10, 1)] = {"matterButton": dev.id}

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_EVENT,
        node_id=10, endpoint=1, cluster=CLUSTER_SWITCH,
        event_id=EVT_SHORT_RELEASE, event_data={"previousPosition": 0},
        raw=GOLDEN_NODE_EVENT_FRAME,
    ))
    assert devices[dev.id].states["lastButtonEvent"] == "shortPress"
    assert devices[dev.id].states["pressCount"] == 1


def test_node_event_active_gate_blocks_inactive_device(ds, indigo_env):
    """node_event respects the _active gate: inactive device is skipped."""
    _indigo, devices = indigo_env
    dev = _make_button_dev(devices)
    ds._index[(10, 1)] = {"matterButton": dev.id}
    # mark a DIFFERENT device active → ours is gated out
    ds.set_active(99999, True)

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_EVENT,
        node_id=10, endpoint=1, cluster=CLUSTER_SWITCH,
        event_id=EVT_SHORT_RELEASE, event_data=None,
    ))
    # states unchanged
    assert devices[dev.id].states.get("lastButtonEvent", "") == ""


def test_node_event_deleted_device_debug_not_traceback(ds, indigo_env, mock_logger):
    """#84: a deletion race on the EVENT path is a debug line, not a KeyError.

    This is the chattier route (switch presses, lock operations) — the
    unguarded lookup used to put a full traceback in the event log per event
    via the plugin's _on_matter_event handler.
    """
    _indigo, devices = indigo_env
    dev = _make_button_dev(devices)
    ds._index[(10, 1)] = {"matterButton": dev.id}
    del devices._by_id[dev.id]

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_EVENT,
        node_id=10, endpoint=1, cluster=CLUSTER_SWITCH,
        event_id=EVT_SHORT_RELEASE, event_data=None,
    ))  # must not raise

    debug_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.debug.call_args_list]
    assert any("vanished" in m and str(dev.id) in m for m in debug_msgs)
    warn_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("bad node_event" in m for m in warn_msgs)


def test_node_event_unknown_cluster_ignored(ds, indigo_env):
    """node_event for a cluster with no registered handler is silently dropped."""
    _indigo, devices = indigo_env
    dev = _make_button_dev(devices)
    ds._index[(10, 1)] = {"matterButton": dev.id}

    # cluster 0xFFFF has no handler
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_EVENT,
        node_id=10, endpoint=1, cluster=0xFFFF,
        event_id=1, event_data=None,
    ))
    # no crash, no state change
    assert devices[dev.id].states.get("lastButtonEvent", "") == ""


def test_node_event_unknown_device_ignored(ds, indigo_env):
    """node_event for an unknown (node_id, endpoint) is silently dropped."""
    _indigo, devices = indigo_env
    # no device registered → lookup returns None
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_EVENT,
        node_id=999, endpoint=1, cluster=CLUSTER_SWITCH,
        event_id=EVT_SHORT_RELEASE, event_data=None,
    ))  # must not raise


def test_node_event_malformed_frame_is_logged(ds, mock_logger):
    """A node_event with node_id=None is logged and dropped."""
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_EVENT,
        node_id=None, endpoint=1, cluster=CLUSTER_SWITCH,
        event_id=EVT_SHORT_RELEASE, event_data=None,
        raw={"event": "node_event", "data": "bad"},
    ))
    assert mock_logger.warning.called


def test_node_event_handler_exception_is_isolated(ds, indigo_env, mock_logger):
    """A handler that raises must not propagate; warning is logged."""
    _indigo, devices = indigo_env
    dev = _make_button_dev(devices)
    ds._index[(10, 1)] = {"matterButton": dev.id}

    # Patch the handler to raise
    handler = ds.registry.handler_for_cluster(CLUSTER_SWITCH)
    original = handler.on_node_event
    try:
        handler.on_node_event = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        ds.handle_event(MatterEvent(
            kind=protocol.EVT_NODE_EVENT,
            node_id=10, endpoint=1, cluster=CLUSTER_SWITCH,
            event_id=EVT_SHORT_RELEASE, event_data=None,
        ))
    finally:
        handler.on_node_event = original

    assert mock_logger.warning.called
    # device states unchanged
    assert devices[dev.id].states.get("lastButtonEvent", "") == ""


# ---------------------------------------------------------------------------
# GenericSwitchHandler.on_node_event: event mapping
# ---------------------------------------------------------------------------

def _handler():
    return GenericSwitchHandler()


class _FakeDevStates(dict):
    """A dict subclass used as dev.states so .get() works naturally."""


def _fake_dev_dict(press_count=0):
    """Fake device where states.get() works via a real dict subclass."""
    dev = MagicMock()
    dev.states = _FakeDevStates({"pressCount": press_count, "lastButtonEvent": ""})
    return dev


@pytest.mark.parametrize("event_id,expected_label", [
    (EVT_SHORT_RELEASE, "shortPress"),
    (EVT_LONG_PRESS,    "longPress"),
])
def test_on_node_event_basic_events(event_id, expected_label):
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, event_id, None)
    assert result["lastButtonEvent"] == expected_label
    assert result["pressCount"] == 1


def test_on_node_event_initial_press_suppressed():
    """InitialPress returns {} — too noisy to surface before terminal event."""
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, EVT_INITIAL_PRESS, None)
    assert result == {}


def test_on_node_event_multi_press_double():
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE,
                              {"totalNumberOfPressesCounted": 2})
    assert result["lastButtonEvent"] == "doublePress"
    assert result["pressCount"] == 1


def test_on_node_event_multi_press_triple():
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE,
                              {"totalNumberOfPressesCounted": 3})
    assert result["lastButtonEvent"] == "triplePress"


def test_on_node_event_multi_press_n_greater_than_3():
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE,
                              {"totalNumberOfPressesCounted": 5})
    assert result["lastButtonEvent"] == "multiPress5"


def test_on_node_event_multi_press_count_1_is_suppressed():
    """#76: count=1 is the NORMAL close of a single press — the ShortRelease
    ~0.5s earlier already surfaced and counted it, so acting here double-bumped
    pressCount on every clean single click (live BILRESA evidence)."""
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE,
                              {"totalNumberOfPressesCounted": 1})
    assert result == {}


def test_on_node_event_multi_press_missing_count_defaults_to_1_and_suppresses():
    """Missing totalNumberOfPressesCounted defaults to 1 → suppressed (#76)."""
    h = _handler()
    dev = _fake_dev_dict()
    assert h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE, {}) == {}


def test_on_node_event_multi_press_null_data_suppresses():
    """data=None for MultiPressComplete: treats as count=1 → suppressed (#76)."""
    h = _handler()
    dev = _fake_dev_dict()
    assert h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE, None) == {}


def test_a_clean_single_press_bumps_press_count_exactly_once():
    """#76 regression, the BILRESA pair: InitialPress (ignored) → ShortRelease
    (counts) → MultiPressComplete count=1 (~0.5s later, must NOT count).
    Before the fix a 3-single-press test produced pressCount += 6, not 3."""
    h = _handler()
    dev = _fake_dev_dict()
    assert h.on_node_event(dev, EVT_INITIAL_PRESS, None) == {}
    first = h.on_node_event(dev, EVT_SHORT_RELEASE, {"previousPosition": 0})
    assert first == {"lastButtonEvent": "shortPress", "pressCount": 1}
    dev.states.update(first)
    assert h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE,
                           {"previousPosition": 0,
                            "totalNumberOfPressesCounted": 1}) == {}
    assert dev.states["pressCount"] == 1


def test_a_genuine_double_press_still_counts_via_multi_press_complete():
    """The count >= 2 path is the one MultiPressComplete exists for — it must
    survive #76's suppression of the count <= 1 close."""
    h = _handler()
    dev = _fake_dev_dict()
    result = h.on_node_event(dev, EVT_MULTI_PRESS_COMPLETE,
                              {"totalNumberOfPressesCounted": 2})
    assert result["lastButtonEvent"] == "doublePress"
    assert result["pressCount"] == 1


def test_on_node_event_unknown_event_id_returns_empty():
    """Unknown event id (e.g. LongRelease 0x04) → {} (gracefully ignored)."""
    h = _handler()
    dev = _fake_dev_dict()
    assert h.on_node_event(dev, 0x04, None) == {}
    assert h.on_node_event(dev, 0xFF, None) == {}


def test_press_count_increments_from_nonzero():
    """pressCount increments even when starting from a nonzero value."""
    h = _handler()
    dev = _fake_dev_dict(press_count=7)
    result = h.on_node_event(dev, EVT_SHORT_RELEASE, None)
    assert result["pressCount"] == 8


def test_press_count_increments_on_repeated_same_event():
    """pressCount must advance even when the same event repeats.

    Indigo triggers fire only on state *change*, so pressCount guarantees a
    trigger fires on every press even if lastButtonEvent is the same string.
    """
    h = _handler()
    dev = _fake_dev_dict(press_count=3)
    result1 = h.on_node_event(dev, EVT_SHORT_RELEASE, None)
    assert result1["pressCount"] == 4
    # Simulate second press: pressCount is now 4
    dev.states.get = lambda k, d=None: {"pressCount": 4, "lastButtonEvent": "shortPress"}.get(k, d)
    result2 = h.on_node_event(dev, EVT_SHORT_RELEASE, None)
    assert result2["pressCount"] == 5


# ---------------------------------------------------------------------------
# GenericSwitchHandler.on_attribute_update
# ---------------------------------------------------------------------------

def test_on_attribute_update_returns_empty_for_current_position():
    """CurrentPosition is transient — no state to map in v1."""
    h = _handler()
    dev = MagicMock()
    assert h.on_attribute_update(dev, ATTR_CURRENT_POSITION, 0) == {}
    assert h.on_attribute_update(dev, ATTR_CURRENT_POSITION, 1) == {}
    assert h.on_attribute_update(dev, ATTR_CURRENT_POSITION, None) == {}


def test_on_attribute_update_returns_empty_for_unknown_attr():
    h = _handler()
    dev = MagicMock()
    assert h.on_attribute_update(dev, 0xFFFF, 42) == {}


# ---------------------------------------------------------------------------
# GenericSwitchHandler.handle_indigo_action
# ---------------------------------------------------------------------------

def test_handle_indigo_action_is_none():
    """Buttons have no actionable Matter commands."""
    h = _handler()
    assert h.handle_indigo_action(MagicMock(), MagicMock()) is None


# ---------------------------------------------------------------------------
# GenericSwitchHandler.attributes_to_subscribe
# ---------------------------------------------------------------------------

def test_attributes_to_subscribe_contains_current_position():
    attrs = _handler().attributes_to_subscribe()
    assert ATTR_CURRENT_POSITION in attrs
    assert len(attrs) == 1


# ---------------------------------------------------------------------------
# GenericSwitchHandler.create_indigo_devices spec
# ---------------------------------------------------------------------------

def _fake_node(node_id=10, product_name="SOMRIG", vendor_name="IKEA",
               suggested_name=None, features=None, feature_endpoint=1):
    node = MagicMock()
    node.node_id = node_id
    node.product_name = product_name
    node.vendor_name = vendor_name
    node.suggested_name = suggested_name
    # A real NodeInfo.attributes is a plain dict; MagicMock's default .get would
    # answer every lookup with another Mock, which is not what "the node has not
    # reported a FeatureMap" looks like.
    node.attributes = ({} if features is None
                       else {(feature_endpoint, CLUSTER_SWITCH, ATTR_FEATURE_MAP): features})
    return node


def _fake_endpoint(endpoint_id=1):
    ep = MagicMock()
    ep.endpoint_id = endpoint_id
    return ep


def test_create_devices_returns_one_spec():
    specs = _handler().create_indigo_devices(_fake_node(), _fake_endpoint())
    assert len(specs) == 1


def test_create_devices_spec_shape():
    node = _fake_node(node_id=10, product_name="SOMRIG", vendor_name="IKEA",
                      suggested_name="Hall Button")
    ep   = _fake_endpoint(endpoint_id=2)
    spec = _handler().create_indigo_devices(node, ep)[0]

    assert spec.device_type_id == "matterButton"
    assert spec.name == "Hall Button"
    assert spec.props["nodeId"]      == "10"
    assert spec.props["endpointId"]  == "2"
    assert spec.props["vendorName"]  == "IKEA"
    assert spec.props["productName"] == "SOMRIG"
    assert spec.initial_states == {"lastButtonEvent": "", "pressCount": 0}


def test_create_devices_uses_product_name_when_no_suggested_name():
    node = _fake_node(suggested_name=None, product_name="RODRET")
    spec = _handler().create_indigo_devices(node, _fake_endpoint())[0]
    assert spec.name == "RODRET"


def test_create_devices_fallback_name():
    node = _fake_node(node_id=99, suggested_name=None, product_name=None)
    spec = _handler().create_indigo_devices(node, _fake_endpoint())[0]
    assert spec.name == "Matter 99"


# ---------------------------------------------------------------------------
# Issue #231 — a momentary-only switch: InitialPress is the whole press
# ---------------------------------------------------------------------------
# Reported by coolcaper777 on an Aqara Light Switch H2 (vertical): the wired
# gang worked, the two WIRELESS gangs did nothing at all. Their Switch
# endpoints (4 and 5) report FeatureMap 0x02 — MS and nothing else — so by
# Matter 1.2 §1.13.7 they can only ever emit InitialPress, and the handler
# discarded it while waiting for a terminal event that is never coming.

MS_ONLY = FEATURE_MOMENTARY_SWITCH                                   # the Aqara H2: 0x02
MS_PLUS_RELEASE = FEATURE_MOMENTARY_SWITCH | FEATURE_MOMENTARY_RELEASE


@pytest.mark.parametrize("features, expected", [
    (MS_ONLY,                                                     True),
    (MS_PLUS_RELEASE,                                             False),
    (FEATURE_MOMENTARY_SWITCH | FEATURE_MOMENTARY_LONG_PRESS,     False),
    (FEATURE_MOMENTARY_SWITCH | FEATURE_MOMENTARY_MULTI_PRESS,    False),
    (0x1E,                                                        False),  # MS+MSR+MSL+MSM (SOMRIG)
    (FEATURE_LATCHING_SWITCH,                                     False),  # LS: SwitchLatched only
    (0,                                                           False),
    (None,                                                        False),  # not reported yet
])
def test_maps_initial_press_truth_table(features, expected):
    assert maps_initial_press(features) is expected


def test_switch_features_reads_the_endpoints_feature_map():
    node = _fake_node(features=MS_ONLY, feature_endpoint=4)
    assert switch_features(node, _fake_endpoint(endpoint_id=4)) == MS_ONLY


def test_switch_features_is_none_when_the_node_has_not_reported_it():
    """ADR-0003 — an absent attribute is no information, not "no features"."""
    assert switch_features(_fake_node(), _fake_endpoint()) is None


def test_create_devices_stamps_the_feature_map():
    node = _fake_node(features=MS_ONLY, feature_endpoint=4)
    spec = _handler().create_indigo_devices(node, _fake_endpoint(endpoint_id=4))[0]
    assert spec.props[PROP_SWITCH_FEATURES] == "2", "stringified, like nodeId/endpointId"


def test_create_devices_omits_the_prop_when_the_feature_map_is_unknown():
    """Omitted rather than defaulted: a guess here decides whether every press
    on this device is kept or dropped, and the reconcile heal fills it in."""
    spec = _handler().create_indigo_devices(_fake_node(), _fake_endpoint())[0]
    assert PROP_SWITCH_FEATURES not in spec.props


def _dev_with_features(features):
    props = {} if features is None else {PROP_SWITCH_FEATURES: str(features)}
    return FakeDev(4001, "Aqara Gang 1", "matterButton", props=props,
                   initial_states={"lastButtonEvent": "", "pressCount": 0})


def test_initial_press_is_the_press_on_a_momentary_only_switch():
    dev = _dev_with_features(MS_ONLY)
    assert _handler().on_node_event(dev, EVT_INITIAL_PRESS, {"newPosition": 1}) == {
        "lastButtonEvent": "shortPress", "pressCount": 1}


def test_initial_press_is_still_suppressed_when_a_terminal_event_follows():
    dev = _dev_with_features(MS_PLUS_RELEASE)
    assert _handler().on_node_event(dev, EVT_INITIAL_PRESS, {"newPosition": 1}) == {}


def test_a_release_capable_switch_still_counts_one_press_per_press():
    """The regression #76 guards. InitialPress then ShortRelease is ONE press —
    #231 must not reintroduce the double-count by mapping the leading edge."""
    dev = _dev_with_features(MS_PLUS_RELEASE)
    for event_id in (EVT_INITIAL_PRESS, EVT_SHORT_RELEASE):
        states = _handler().on_node_event(dev, event_id, {})
        dev.states.update(states)
    assert dev.states["pressCount"] == 1


def test_initial_press_is_suppressed_when_the_prop_is_absent():
    """A button created before #231, before the reconcile heal reaches it. The
    safe default is the old behaviour — never a double-count on a device we
    have no evidence about."""
    assert _handler().on_node_event(_dev_with_features(None), EVT_INITIAL_PRESS, {}) == {}


@pytest.mark.parametrize("raw", ["", "banana", None])
def test_an_unreadable_feature_map_prop_suppresses_rather_than_raises(raw):
    dev = FakeDev(4002, "Odd", "matterButton", props={PROP_SWITCH_FEATURES: raw},
                  initial_states={"lastButtonEvent": "", "pressCount": 0})
    assert _handler().on_node_event(dev, EVT_INITIAL_PRESS, {}) == {}


def test_momentary_only_presses_keep_incrementing_the_counter():
    """Every press repeats the same label, so pressCount is the only thing an
    Indigo trigger can fire on."""
    dev = _dev_with_features(MS_ONLY)
    for expected in (1, 2, 3):
        dev.states.update(_handler().on_node_event(dev, EVT_INITIAL_PRESS, {}))
        assert dev.states["pressCount"] == expected


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

def test_registry_resolves_switch_cluster():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_SWITCH)
    assert handler is not None
    assert isinstance(handler, GenericSwitchHandler)


def test_registry_resolves_by_device_type():
    reg = HandlerRegistry()
    handler = reg.handler_for_device(MagicMock(deviceTypeId="matterButton"))
    assert isinstance(handler, GenericSwitchHandler)


def test_registry_cluster_id_is_correct():
    assert CLUSTER_SWITCH == 0x003B


def test_base_handler_on_node_event_default_returns_empty():
    """Base class default on_node_event returns {} without raising."""
    from matter_handlers.on_off import OnOffHandler
    h = OnOffHandler()
    dev = MagicMock()
    result = h.on_node_event(dev, 0x01, {"someField": 1})
    assert result == {}


def test_button_display_is_ui_display_state_fallback():
    # The button disclaims BOTH built-in displays so Indigo falls back to the
    # Devices.xml UiDisplayStateId (lastButtonEvent) — precedence verified
    # live on jarvis (issue #56 follow-up). Without this the device list
    # showed "off" forever.
    from matter_handlers.generic_switch import GenericSwitchHandler
    assert GenericSwitchHandler.display_props == {
        "SupportsOnState": False, "SupportsSensorValue": False,
    }
