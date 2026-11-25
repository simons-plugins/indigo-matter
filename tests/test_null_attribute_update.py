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


def _lines(mock):
    return [c[0][0] % tuple(c[0][1:]) for c in mock.call_args_list]


def _null_infos(mock_logger):
    return [m for m in _lines(mock_logger.info) if "null" in m]


def test_first_null_logs_one_info_naming_the_device(ds, indigo_env, mock_logger):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY, "Plug")
    dev_id = ds.lookup(101, 1)
    ds.handle_event(_wire(101, "1/6/0", None))
    infos = _null_infos(mock_logger)
    assert len(infos) == 1, infos
    msg = infos[0]
    assert f'device {dev_id} "{devices[dev_id].name}"' in msg
    assert "node 101" in msg and "endpoint 1" in msg
    assert "cluster 6" in msg and "attribute 0" in msg
    assert "removed" not in msg  # a null may also be a nullable attribute that is unknown
    assert not mock_logger.warning.called


def test_a_repeated_identical_null_stays_at_debug(ds, indigo_env, mock_logger):
    ds.create_from_raw(RELAY, "Plug")
    ds.handle_event(_wire(101, "1/6/0", None))
    ds.handle_event(_wire(101, "1/6/0", None))
    assert len(_null_infos(mock_logger)) == 1
    dbg = [m for m in _lines(mock_logger.debug) if "null" in m and "cl6" in m and "attr0" in m]
    assert dbg, "the repeat is still visible at debug, naming its address"


def test_a_different_attribute_or_device_logs_its_own_info(ds, indigo_env, mock_logger):
    ds.create_from_raw(DIMMER, "Lamp")
    ds.create_from_raw(RELAY, "Plug")
    ds.handle_event(_wire(102, "1/6/0", None))
    ds.handle_event(_wire(102, "1/8/0", None))
    ds.handle_event(_wire(101, "1/6/0", None))
    assert len(_null_infos(mock_logger)) == 3


def test_the_prime_path_logs_once_across_repeated_reconciles(ds, indigo_env, mock_logger):
    ds.create_from_raw(RELAY, "Plug")
    nulled = {**RELAY, "attributes": {**RELAY["attributes"], "1/6/0": None}}
    ds.reconcile_all([nulled])
    ds.reconcile_all([nulled])
    infos = _null_infos(mock_logger)
    assert len(infos) == 1, infos
    ds.handle_event(_wire(101, "1/6/0", None))  # same key as the prime: still once
    assert len(_null_infos(mock_logger)) == 1


def test_a_device_created_from_a_null_snapshot_says_so(ds, indigo_env, mock_logger):
    """No value was ever received, so the wording must not claim a 'last value'."""
    _indigo, devices = indigo_env
    ds.create_from_raw(_node(110, {"1/6/0": None}, 266), "Plug")
    dev = devices[ds.lookup(110, 1)]
    assert dev.states["onOffState"] is False  # the creation default, not a reading
    infos = _null_infos(mock_logger)
    assert len(infos) == 1
    assert "creation default" in infos[0]


def test_null_with_no_device_stays_silent(ds, mock_logger):
    ds.handle_event(_wire(555, "1/6/0", None))
    assert not _null_infos(mock_logger)


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
    assert dev.states["onOffState"] is True


def test_null_for_an_unhandled_cluster_does_not_raise(ds, indigo_env, mock_logger):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY, "Plug")
    before = dict(devices[ds.lookup(101, 1)].states)
    ds.handle_event(_wire(101, "1/9999/3", None))
    assert not mock_logger.warning.called
    assert devices[ds.lookup(101, 1)].states == before


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


def test_a_snapshot_null_alone_does_not_overwrite_a_real_state(ds, indigo_env):
    """Pins the prime site on its own (no live null first)."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY, "Plug")
    dev = devices[ds.lookup(101, 1)]
    nulled = {**RELAY, "attributes": {**RELAY["attributes"], "1/6/0": None}}
    ds.reconcile_all([nulled])
    assert dev.states["onOffState"] is True


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
    node = _with_node_evidence(RELAY)
    node["node_id"] = 111
    node["attributes"]["0/53/1"] = 5  # RoutingRole = Router
    result = ds.create_from_raw(node, "Plug")
    node_dev = devices[result["nodeDeviceId"]]
    ds.handle_event(_wire(111, "0/53/1", 2))  # SleepyEndDevice
    assert node_dev.states.get("threadRole") != "SleepyEndDevice"  # gate still shut
    ds.handle_event(_wire(111, f"0/53/{thread_mesh.ATTR_NEIGHBOR_TABLE}", None))
    assert node_dev.states.get("threadRole") == "SleepyEndDevice"


def test_thread_opt_out_holds_through_the_prime_site(ds, indigo_env):
    _indigo, devices = indigo_env
    node = _with_node_evidence(RELAY)
    node["node_id"] = 112
    node["attributes"]["0/53/1"] = 2  # SleepyEndDevice
    node["attributes"][f"0/53/{thread_mesh.ATTR_NEIGHBOR_TABLE}"] = None
    res = ds.create_from_raw(node, "Plug")
    assert devices[res["nodeDeviceId"]].states.get("threadRole") == "SleepyEndDevice"


# --- "make it fatal": prove the handler is never even called with None -------

STUB_CLUSTER = 0x7E57


class _Stub(ClusterHandler):
    cluster_id = STUB_CLUSTER
    cluster_name = "Stub"

    def __init__(self, node_scoped, accepts_null):
        self.node_scoped = node_scoped
        self.accepts_null = accepts_null
        self.calls = []

    def create_indigo_devices(self, node, endpoint):
        return []

    def attributes_to_subscribe(self):
        return [0]

    def on_attribute_update(self, indigo_dev, attribute_id, value):
        self.calls.append(value)
        if value is None and not self.accepts_null:
            raise AssertionError("handler was called with a null")
        return {}

    def handle_indigo_action(self, indigo_dev, action):
        return None


def _stub_ds(indigo_env, mock_logger, stub):
    import device_sync
    importlib.reload(device_sync)
    from matter_handlers.registry import HandlerRegistry, default_handlers
    return device_sync.DeviceSync(HandlerRegistry(default_handlers() + [stub]), mock_logger)


def _stub_node(value):
    return {**RELAY, "attributes": {**RELAY["attributes"], f"1/{STUB_CLUSTER}/0": value}}


@pytest.mark.parametrize("node_scoped", [False, True])
def test_a_null_never_reaches_a_handler_that_does_not_accept_it(
        indigo_env, mock_logger, node_scoped):
    stub = _Stub(node_scoped, accepts_null=False)
    stub_ds = _stub_ds(indigo_env, mock_logger, stub)
    stub_ds.create_from_raw(_stub_node(None), "Plug")          # prime site, create
    stub_ds.reconcile_all([_stub_node(None)])                   # prime site, reconcile
    stub_ds.handle_event(_wire(101, f"1/{STUB_CLUSTER}/0", None))  # live site
    assert None not in stub.calls
    assert not mock_logger.warning.called  # no "bad update" from the AssertionError
    stub_ds.handle_event(_wire(101, f"1/{STUB_CLUSTER}/0", 5))  # a real value still arrives
    assert 5 in stub.calls


@pytest.mark.parametrize("node_scoped", [False, True])
def test_a_handler_that_accepts_null_receives_it_at_both_sites(
        indigo_env, mock_logger, node_scoped):
    stub = _Stub(node_scoped, accepts_null=True)
    stub_ds = _stub_ds(indigo_env, mock_logger, stub)
    stub_ds.create_from_raw(_stub_node(None), "Plug")
    assert stub.calls == [None], "prime site"
    stub_ds.handle_event(_wire(101, f"1/{STUB_CLUSTER}/0", None))
    assert stub.calls == [None, None], "live site"


def test_default_for_handlers_is_to_skip_nulls():
    assert ClusterHandler.accepts_null is False
