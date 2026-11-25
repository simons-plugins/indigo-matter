"""#374 - a JSON null attribute value must never become a made-up Indigo state.

From matter-server 1.5.0 an attribute that is REMOVED from a node (firmware
update, re-interview) is reported as an ordinary ``attribute_updated`` event whose
value is null. A null means "no value known", so the plugin keeps the last known
state. These tests drive the real wire path (``Protocol.parse_event`` ->
``DeviceSync.handle_event``) and the node-snapshot priming path.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import protocol
import thread_mesh
from indigo_fakes import FakeDeviceFactory, FakeDevices, FakeFolderFactory
from matter_handlers.base import ClusterHandler
from matter_handlers.thread_network_diagnostics import ThreadNetworkDiagnosticsHandler
from test_device_sync import _with_node_evidence


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
    from matter_handlers.registry import HandlerRegistry
    return device_sync.DeviceSync(HandlerRegistry(), mock_logger)


def _wire(node_id, path, value):
    """The attribute_updated frame as matter-server sends it, parsed for real."""
    return protocol.Protocol().parse_event(
        {"event": "attribute_updated", "data": [node_id, path, value]})


def _node(node_id, attrs, device_type):
    return {"node_id": node_id, "available": True,
            "attributes": {"1/29/0": [{"0": device_type}], **attrs}}


RELAY = _node(101, {"1/6/0": True}, 266)
DIMMER = _node(102, {"1/6/0": True, "1/8/0": 127}, 257)
TEMP = _node(103, {"1/1026/0": 2150}, 770)
HUMIDITY = _node(104, {"1/1029/0": 4500}, 775)
OCCUPANCY = _node(105, {"1/1030/0": 1}, 263)
CONTACT = _node(106, {"1/69/0": True}, 21)
THERMOSTAT = _node(107, {"1/513/0": 2100, "1/513/18": 2000, "1/513/17": 2400,
                         "1/513/28": 4, "1/513/41": 1}, 769)
LOCK = _node(108, {"1/257/0": 1}, 11)

# (name, node, path of the attribute that goes null)
CASES = [
    ("onoff", RELAY, "1/6/0"),
    ("onoff-on-dimmer", DIMMER, "1/6/0"),
    ("level", DIMMER, "1/8/0"),
    ("temperature", TEMP, "1/1026/0"),
    ("humidity", HUMIDITY, "1/1029/0"),
    ("occupancy", OCCUPANCY, "1/1030/0"),
    ("contact", CONTACT, "1/69/0"),
    ("thermo-temp", THERMOSTAT, "1/513/0"),
    ("thermo-heat", THERMOSTAT, "1/513/18"),
    ("thermo-cool", THERMOSTAT, "1/513/17"),
    ("thermo-mode", THERMOSTAT, "1/513/28"),
    ("thermo-running", THERMOSTAT, "1/513/41"),
    ("lock", LOCK, "1/257/0"),
]


def _all_states(devices, ds_, node_id):
    return {i: dict(devices[i].states) for i in ds_._all_dev_ids_for_endpoint(node_id, 1)}  # pylint: disable=protected-access


@pytest.mark.parametrize("name,node,path", CASES, ids=[c[0] for c in CASES])
def test_null_update_keeps_every_state(ds, indigo_env, name, node, path):
    _indigo, devices = indigo_env
    ds.create_from_raw(node, name)
    before = _all_states(devices, ds, node["node_id"])
    assert before, "sanity: a device exists"

    ds.handle_event(_wire(node["node_id"], path, None))

    assert _all_states(devices, ds, node["node_id"]) == before


def test_null_update_is_logged_at_debug_with_its_address(ds, indigo_env, mock_logger):
    ds.create_from_raw(RELAY, "Plug")
    ds.handle_event(_wire(101, "1/6/0", None))
    msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.debug.call_args_list]
    hit = [m for m in msgs if "null" in m and "101" in m and "ep1" in m
           and "cl6" in m and "attr0" in m]
    assert hit, msgs
    assert not mock_logger.warning.called


def test_real_value_after_null_is_applied(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY, "Plug")
    dev = devices[ds.lookup(101, 1)]
    ds.handle_event(_wire(101, "1/6/0", None))
    assert dev.states["onOffState"] is True
    ds.handle_event(_wire(101, "1/6/0", False))
    assert dev.states["onOffState"] is False


def test_false_and_zero_are_values_not_nulls(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(DIMMER, "Lamp")
    dev = devices[ds.lookup(102, 1)]
    ds.handle_event(_wire(102, "1/8/0", 0))
    assert dev.states["brightnessLevel"] == 0
    ds.handle_event(_wire(102, "1/6/0", False))
    assert dev.states["onOffState"] is False


def test_null_does_not_touch_the_error_state(ds, indigo_env):
    """A null is neither proof of life (do not clear a real error) nor of death."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY, "Plug")
    dev = devices[ds.lookup(101, 1)]
    dev.errorState = "unreachable"
    ds.handle_event(_wire(101, "1/6/0", None))
    assert dev.errorState == "unreachable"


def test_null_for_an_unhandled_cluster_does_not_raise(ds, indigo_env, mock_logger):
    ds.create_from_raw(RELAY, "Plug")
    ds.handle_event(_wire(101, "1/9999/3", None))
    assert not mock_logger.warning.called


def test_null_for_an_unknown_node_does_not_raise(ds, mock_logger):
    ds.handle_event(_wire(555, "1/6/0", None))
    assert not mock_logger.warning.called


def test_null_for_a_node_scoped_cluster_keeps_battery(ds, indigo_env):
    _indigo, devices = indigo_env
    node = _node(109, {"1/1026/0": 2000, "1/47/12": 200, "1/47/31": []}, 770)
    ds.create_from_raw(node, "Battery temp")
    dev = devices[ds.lookup(109, 1)]
    before = dict(dev.states)
    ds.handle_event(_wire(109, "1/47/12", None))
    assert dev.states == before


def test_a_later_snapshot_with_a_null_does_not_resurrect_a_fabricated_value(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY, "Plug")
    dev = devices[ds.lookup(101, 1)]
    ds.handle_event(_wire(101, "1/6/0", None))
    nulled = {**RELAY, "attributes": {**RELAY["attributes"], "1/6/0": None}}
    ds.reconcile_all([nulled])
    assert dev.states["onOffState"] is True


def test_thread_diagnostics_keeps_its_meaning_for_null(ds, indigo_env):
    """ThreadNetworkDiagnostics reads null as 'unknown, still seen' (#334 B2.1),
    so it opts out of the null skip: a null NeighborTable still opens its gate."""
    _indigo, devices = indigo_env
    assert ThreadNetworkDiagnosticsHandler.accepts_null is True
    node = _with_node_evidence(RELAY)
    node["node_id"] = 111
    node["attributes"]["0/53/1"] = 5  # RoutingRole = Router
    result = ds.create_from_raw(node, "Plug")
    node_dev = devices[result["nodeDeviceId"]]
    ds.handle_event(_wire(111, "0/53/1", 2))  # SleepyEndDevice
    ds.handle_event(_wire(111, f"0/53/{thread_mesh.ATTR_NEIGHBOR_TABLE}", None))
    assert node_dev.states.get("threadRole") == "Sleepy end device"


def test_default_for_handlers_is_to_skip_nulls():
    assert ClusterHandler.accepts_null is False
