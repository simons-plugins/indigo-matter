"""ThreadNetworkDiagnostics cluster (0x0035) → the matterNode Thread states (#334).

Tests cover:
- A device with the four Thread states seeded gets threadRole/threadNeighbourCount/
  threadLinkRssi/threadHealth from feeding the real fixture's attributes, one at a
  time (device_sync's actual calling contract), through on_attribute_update.
- A device without those states (an old state list) always gets {} back.
- A malformed/never-role-established update degrades to {} without raising.
- attributes_to_subscribe carries exactly what the four states consume.
- Registration: the handler is in default_handlers(), reachable by cluster id,
  and 0x0035 is a recognized cluster in device_sync.
- Read-only: handle_indigo_action always returns None.
- device_sync integration: priming a real node (via create_from_raw, the same
  path a fresh commission/reconcile uses) through the real fixture's Thread
  attributes writes the four states onto the resulting matterNode device.
"""
from __future__ import annotations

import importlib
import json
import os
from types import SimpleNamespace

import pytest

from matter_handlers.registry import HandlerRegistry
from matter_handlers.thread_network_diagnostics import (
    ATTR_ATTRIBUTE_LIST,
    ATTR_LEADER_ROUTER_ID,
    ATTR_NEIGHBOR_TABLE,
    ATTR_PARENT_CHANGE_COUNT,
    ATTR_ROUTE_TABLE,
    ATTR_ROUTING_ROLE,
    CLUSTER_THREAD_DIAG,
    ThreadNetworkDiagnosticsHandler,
)

_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "thread_mesh", "nodes.json"
)


def _load_fixture_node(node_id: int) -> dict:
    with open(_FIXTURE_PATH, encoding="utf-8") as fh:
        nodes = json.load(fh)
    for node in nodes:
        if node["node_id"] == node_id:
            return node
    raise KeyError(node_id)


def _thread_attrs(node: dict) -> dict:
    """Just the ThreadNetworkDiagnostics (cluster 0x35) keys of a fixture node,
    as {attribute_id: value} — the shape on_attribute_update receives one of
    at a time."""
    out = {}
    for key, value in node["attributes"].items():
        ep, cluster, attribute = (int(part) for part in key.split("/"))
        if ep == 0 and cluster == CLUSTER_THREAD_DIAG:
            out[attribute] = value
    return out


# ---------------------------------------------------------------------------
# Handler unit tests
# ---------------------------------------------------------------------------

class _Dev:
    """Minimal Indigo device stub for handler tests."""

    def __init__(self, states: dict | None = None, node_id: int = 0x34):
        self.states = states if states is not None else {
            "threadRole": "", "threadNeighbourCount": 0,
            "threadLinkRssi": -128, "threadHealth": "",
        }
        self.name = "Router Node"
        self.pluginProps = {"nodeId": str(node_id), "endpointId": "0"}


_THREAD_STATE_KEYS = {"threadRole", "threadNeighbourCount", "threadLinkRssi", "threadHealth"}


def test_feeding_0x34_attributes_yields_router_single_neighbour_warn():
    """0x34 (ground truth, SPEC-thread-mesh.md): router id 62, ONE neighbour at
    -74 dBm → Router / 1 neighbour / -74 dBm / warn (single_neighbour)."""
    node = _load_fixture_node(0x34)
    attrs = _thread_attrs(node)
    h = ThreadNetworkDiagnosticsHandler()
    dev = _Dev(node_id=0x34)

    for attribute_id, value in attrs.items():
        result = h.on_attribute_update(dev, attribute_id, value)
        # A partial update along the way must never invent a role/health that
        # isn't backed by data yet — every intermediate result (once RoutingRole
        # has arrived) must be a subset of the four known keys.
        assert set(result) <= _THREAD_STATE_KEYS
    # The fixture dict's last key isn't necessarily one this handler subscribes
    # to (real priming walks the WHOLE cluster snapshot, not just our subset —
    # see the loop above); re-trigger on a subscribed attribute now that the
    # cache holds everything, the same as any later live update would.
    result = h.on_attribute_update(dev, ATTR_ROUTING_ROLE, attrs[ATTR_ROUTING_ROLE])

    assert result["threadRole"] == "Router"
    assert result["threadNeighbourCount"] == 1
    assert result["threadLinkRssi"] == -74
    assert result["threadHealth"] == "warn"


def test_feeding_0x2e_attributes_yields_bad_weak_link_and_unstable_parent():
    """0x2E (ground truth): SED, parent lastRssi -95, 10 parent changes →
    weak_link BAD outranks unstable_parent WARN, so threadHealth is "bad"."""
    node = _load_fixture_node(0x2E)
    attrs = _thread_attrs(node)
    h = ThreadNetworkDiagnosticsHandler()
    dev = _Dev(node_id=0x2E)

    for attribute_id, value in attrs.items():
        h.on_attribute_update(dev, attribute_id, value)
    result = h.on_attribute_update(dev, ATTR_ROUTING_ROLE, attrs[ATTR_ROUTING_ROLE])

    assert result["threadRole"] == "SleepyEndDevice"
    assert result["threadHealth"] == "bad"
    assert result["threadLinkRssi"] == -95  # Neighbour.rssi prefers LastRssi


def test_device_without_thread_states_always_returns_empty():
    """An old-state-list device (created before #334) must stay quiet — same
    guard idiom as every other merge-into handler in this package."""
    node = _load_fixture_node(0x34)
    attrs = _thread_attrs(node)
    h = ThreadNetworkDiagnosticsHandler()
    dev = _Dev(states={"onOffState": False}, node_id=0x34)

    for attribute_id, value in attrs.items():
        assert h.on_attribute_update(dev, attribute_id, value) == {}


def test_unsubscribed_attribute_is_a_noop():
    h = ThreadNetworkDiagnosticsHandler()
    assert h.on_attribute_update(_Dev(), 0x0000, 25) == {}  # Channel — not subscribed


def test_malformed_value_before_any_role_yields_empty_without_raising():
    """No RoutingRole ever cached ⇒ parse_node_diag returns None ⇒ {} — and a
    garbage NeighborTable value must not raise getting there."""
    h = ThreadNetworkDiagnosticsHandler()
    dev = _Dev()
    assert h.on_attribute_update(dev, ATTR_NEIGHBOR_TABLE, "not-a-list-of-structs") == {}
    assert h.on_attribute_update(dev, ATTR_ROUTE_TABLE, object()) == {}
    assert h.on_attribute_update(dev, ATTR_PARENT_CHANGE_COUNT, {"unexpected": "shape"}) == {}


def test_malformed_value_after_role_established_degrades_not_raises():
    """Once RoutingRole is known, a malformed NeighborTable must not raise —
    thread_mesh already skips malformed structs internally; this pins the
    outer handler never surfaces an exception either way."""
    h = ThreadNetworkDiagnosticsHandler()
    dev = _Dev(node_id=0x34)
    h.on_attribute_update(dev, ATTR_ROUTING_ROLE, 5)  # Router
    result = h.on_attribute_update(dev, ATTR_NEIGHBOR_TABLE, "garbage")
    assert set(result) <= _THREAD_STATE_KEYS  # degrades to a valid (if empty-ish) result


def test_unparsable_node_id_props_returns_empty_without_raising():
    h = ThreadNetworkDiagnosticsHandler()
    dev = _Dev()
    dev.pluginProps = {}  # no nodeId at all
    assert h.on_attribute_update(dev, ATTR_ROUTING_ROLE, 5) == {}


def test_is_non_primary():
    h = ThreadNetworkDiagnosticsHandler()
    assert h.is_primary_for(None, None) is False


def test_creates_no_devices():
    h = ThreadNetworkDiagnosticsHandler()
    assert h.create_indigo_devices(None, None) == []


def test_node_scoped_flag():
    """Same-endpoint like BasicInformation, unlike PowerSource: this cluster
    lives on endpoint 0, same as its target matterNode device."""
    h = ThreadNetworkDiagnosticsHandler()
    assert h.node_scoped is False


def test_handle_action_always_returns_none():
    h = ThreadNetworkDiagnosticsHandler()
    assert h.handle_indigo_action(_Dev(), object()) is None


def test_subscribes_to_exactly_what_the_four_states_consume():
    h = ThreadNetworkDiagnosticsHandler()
    assert set(h.attributes_to_subscribe()) == {
        ATTR_ROUTING_ROLE, ATTR_NEIGHBOR_TABLE, ATTR_ROUTE_TABLE,
        ATTR_LEADER_ROUTER_ID, ATTR_PARENT_CHANGE_COUNT, ATTR_ATTRIBUTE_LIST,
    }


def test_registered_in_default_handlers():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_THREAD_DIAG)
    assert handler is not None
    assert isinstance(handler, ThreadNetworkDiagnosticsHandler)


def test_registry_resolves_matter_node_device_type():
    reg = HandlerRegistry()
    handler = reg.handler_for_device(SimpleNamespace(deviceTypeId="matterNode"))
    assert handler is not None
    assert handler.device_type_id == "matterNode"


# ---------------------------------------------------------------------------
# device_sync integration — priming a real node through the real fixture
# (mirrors tests/test_power_source.py's device_sync integration section)
# ---------------------------------------------------------------------------

# Since pytest adds tests/ to sys.path via conftest, import directly.
from indigo_fakes import FakeDev, FakeDeviceFactory, FakeDevices, FakeFolderFactory  # noqa: E402


def _thread_node_raw(fixture_node_id: int) -> dict:
    """A device_sync-shaped raw node: an ep1 actuator (so this looks like a
    real node with an endpoint plan) plus ep0's real ThreadNetworkDiagnostics
    attributes from the fixture, plus BasicInformation AttributeList evidence
    (NodeLabel, 5) so `_ensure_node_device`'s ADR-0003 gate creates a matterNode
    device at all (issue #204)."""
    fixture = _load_fixture_node(fixture_node_id)
    attributes = {
        "1/29/0": [{"0": 266, "1": 2}],   # Descriptor.DeviceTypeList: OnOffPlugInUnit
        "1/6/0": False,                    # OnOff
        "0/40/65531": [1, 2, 3, 4, 5, 10],  # BasicInformation AttributeList: NodeLabel + SwVersion
    }
    for key, value in fixture["attributes"].items():
        ep, cluster, _attribute = (int(part) for part in key.split("/"))
        if ep == 0 and cluster == CLUSTER_THREAD_DIAG:
            attributes[key] = value
    return {
        "node_id": fixture_node_id,
        "available": fixture["available"],
        "attributes": attributes,
    }


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
    return device_sync.DeviceSync(HandlerRegistry(), mock_logger)


def test_recognized_clusters_includes_thread_diagnostics(ds):
    assert CLUSTER_THREAD_DIAG in ds._recognized_clusters  # pylint: disable=protected-access


def test_priming_a_real_node_writes_thread_states_to_the_node_device(ds, indigo_env):
    """The full device_sync path (create_from_raw → _ensure_node_device →
    _prime_states), fed the real fixture's router-id-62 node (0x34): a single
    neighbour at -74 dBm → warn (single_neighbour)."""
    _indigo, devices = indigo_env
    raw = _thread_node_raw(0x34)
    result = ds.create_from_raw(raw, "Router Node")
    node_dev_id = result["nodeDeviceId"]
    assert node_dev_id is not None, "BasicInformation evidence must produce a matterNode device"
    dev = devices[node_dev_id]
    assert dev.deviceTypeId == "matterNode"
    assert dev.states.get("threadRole") == "Router"
    assert dev.states.get("threadNeighbourCount") == 1
    assert dev.states.get("threadLinkRssi") == -74
    assert dev.states.get("threadHealth") == "warn"


def test_priming_a_node_without_thread_cluster_leaves_states_blank(ds, indigo_env):
    """A Wi-Fi node's matterNode device: never fed cluster 0x35, so the Thread
    states stay at their Devices.xml default — no update ever reaches them."""
    _indigo, devices = indigo_env
    raw = {
        "node_id": 0x99,
        "available": True,
        "attributes": {
            "1/29/0": [{"0": 266, "1": 2}],
            "1/6/0": False,
            "0/40/65531": [1, 2, 3, 4, 5, 10],
        },
    }
    result = ds.create_from_raw(raw, "WiFi Plug")
    node_dev_id = result["nodeDeviceId"]
    assert node_dev_id is not None
    dev = devices[node_dev_id]
    assert dev.states.get("threadRole") == 0  # Devices.xml default, never written


def test_live_attribute_event_updates_the_node_device_via_plain_dispatch(ds, indigo_env):
    """node_scoped = False (same-endpoint like BasicInformation): a live
    attribute_updated event on cluster 0x35/endpoint 0 must reach the
    matterNode device through _on_attribute's ORDINARY _lookup_for_cluster
    path, not the PowerSource-style node-scoped fan-out. Primes 0x34 (1
    neighbour → warn), then a live NeighborTable update adding a second,
    healthy neighbour must clear single_neighbour and flip threadHealth to
    "ok" — observable proof the live event actually reached the device."""
    import protocol
    from protocol import MatterEvent

    _indigo, devices = indigo_env
    raw = _thread_node_raw(0x34)
    result = ds.create_from_raw(raw, "Router Node")
    node_dev_id = result["nodeDeviceId"]
    assert node_dev_id is not None
    dev = devices[node_dev_id]
    assert dev.states.get("threadHealth") == "warn"  # single_neighbour, primed

    two_neighbours = _thread_attrs(_load_fixture_node(0x34))[ATTR_NEIGHBOR_TABLE] + [{
        "0": 1, "1": 5, "2": 4096, "3": 500, "4": 8, "5": 3,
        "6": -50, "7": -50, "8": 0, "9": 0,
        "10": True, "11": True, "12": True, "13": False,
    }]
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x34, endpoint=0, cluster=CLUSTER_THREAD_DIAG,
        attribute=ATTR_NEIGHBOR_TABLE, value=two_neighbours,
    ))

    assert dev.states.get("threadNeighbourCount") == 2
    assert dev.states.get("threadHealth") == "ok"
