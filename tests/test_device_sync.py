"""M4: reconciliation + the asyncio→Indigo write/command seams.

Uses a small fake Indigo (devices collection + device.create) so the real
state-update and device-creation paths run without a live Indigo server.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import protocol
from protocol import MatterEvent

from test_handlers import RELAY_NODE


class FakeDev:
    def __init__(self, dev_id, name, device_type_id, props):
        self.id = dev_id
        self.name = name
        self.deviceTypeId = device_type_id
        self.pluginProps = props
        self.states = {}
        self.error = None
        self.errorState = ""
        self.folderId = 0

    def updateStatesOnServer(self, kvlist):
        for kv in kvlist:
            self.states[kv["key"]] = kv["value"]

    def setErrorStateOnServer(self, value):
        self.error = value
        self.errorState = value

    def replaceOnServer(self):
        # Real Indigo persists the in-memory edits (name etc.); the fake already
        # mutated the object in place, so this is a no-op marker.
        self.replaced = True


class FakeFolder:
    def __init__(self, folder_id, name):
        self.id = folder_id
        self.name = name


class FakeFolderFactory:
    """Stands in for ``indigo.devices.folder`` (the folder command namespace)."""

    def __init__(self, devices):
        self.devices = devices

    def create(self, name):
        return self.devices.add_folder(name)


class FakeDevices:
    def __init__(self):
        self._by_id = {}
        self._counter = 1000
        self._folders = {}
        self._folder_counter = 0

    def next_id(self):
        self._counter += 1
        return self._counter

    def add(self, dev):
        self._by_id[dev.id] = dev

    def add_folder(self, name):
        self._folder_counter += 1
        folder = FakeFolder(self._folder_counter, name)
        self._folders[folder.id] = folder
        return folder

    @property
    def folders(self):
        return list(self._folders.values())

    def __iter__(self):
        return iter(list(self._by_id.values()))

    def __getitem__(self, dev_id):
        return self._by_id[dev_id]

    def iter(self, _filter=None):
        return list(self._by_id.values())


class FakeDeviceFactory:
    def __init__(self, devices):
        self.devices = devices
        self.created = []

    def create(self, protocol=None, deviceTypeId="", name="", props=None, folder=0, **kwargs):
        dev = FakeDev(self.devices.next_id(), name, deviceTypeId, dict(props or {}))
        if isinstance(folder, int) and folder:
            dev.folderId = folder
        self.devices.add(dev)
        self.created.append(dev)
        return dev

    def delete(self, dev):
        dev_id = dev.id if hasattr(dev, "id") else dev
        self.devices._by_id.pop(dev_id, None)

    def moveToFolder(self, dev_or_id, value=None):
        dev = dev_or_id if hasattr(dev_or_id, "folderId") else self.devices[dev_or_id]
        dev.folderId = value


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


def test_create_from_raw_creates_relay(ds, indigo_env):
    _indigo, devices = indigo_env
    result = ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert len(result["indigoDeviceIds"]) == 1
    dev_id = result["primaryDeviceId"]
    assert dev_id in result["indigoDeviceIds"]
    assert result["vendorName"] == "TP-Link"
    dev = devices[dev_id]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.name == "Office Plug"
    assert dev.pluginProps["nodeId"] == "42"
    assert dev.states["onOffState"] is False
    # index populated
    assert ds.lookup(42, 1) == dev_id
    assert ds.node_count() == 1


def test_created_device_address_is_node_id_hex(ds, indigo_env):
    # The node id must be recoverable from the Indigo UI (decommission needs it,
    # and the commission job that knew it may have died — issue #18).
    _indigo, devices = indigo_env
    result = ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev = devices[result["primaryDeviceId"]]
    assert dev.pluginProps["address"] == "0x2A"  # node 42


def test_device_creation_logs_at_info(ds, indigo_env, mock_logger):
    # An out-of-band join's ONLY event-log evidence is this line (issue #19):
    # it must be INFO and carry node id, vendor/product, and the device ids.
    _indigo, devices = indigo_env
    result = ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert mock_logger.info.called
    fmt, *args = mock_logger.info.call_args[0]
    message = fmt % tuple(args)
    assert "0x2A" in message
    assert "TP-Link" in message
    assert str(result["primaryDeviceId"]) in message

    # idempotent re-pass creates nothing → stays quiet
    mock_logger.info.reset_mock()
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert not mock_logger.info.called


def test_create_is_idempotent(ds):
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    first = ds.lookup(42, 1)
    result = ds.create_from_raw(RELAY_NODE, "Office Plug")
    # no duplicate created; same device referenced
    assert result["indigoDeviceIds"] == [first]


def test_attribute_event_updates_indigo_state(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=42, endpoint=1, cluster=0x0006, attribute=0x0000, value=True,
    ))
    assert devices[dev_id].states["onOffState"] is True


def test_attribute_event_for_unknown_device_is_ignored(ds):
    # no device created → lookup misses → no crash
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=999, endpoint=1, cluster=0x0006, attribute=0, value=True,
    ))


def test_node_removed_marks_unreachable(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    ds.handle_event(MatterEvent(kind=protocol.EVT_NODE_REMOVED, node_id=42))
    assert devices[dev_id].error == "unreachable"


def test_active_gate_blocks_inactive_devices(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    # mark a DIFFERENT device active → our device is now gated out
    ds.set_active(99999, True)
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=42, endpoint=1, cluster=0x0006, attribute=0, value=True,
    ))
    assert "onOffState" not in devices[dev_id].states or devices[dev_id].states["onOffState"] is False
    # activating it lets the update through
    ds.set_active(dev_id, True)
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=42, endpoint=1, cluster=0x0006, attribute=0, value=True,
    ))
    assert devices[dev_id].states["onOffState"] is True


def test_reconcile_marks_orphans_unreachable(ds, indigo_env):
    _indigo, devices = indigo_env
    # pre-existing device for a node that's no longer present
    orphan = FakeDev(5005, "Ghost", "matterRelay", {"nodeId": "99", "endpointId": "1"})
    devices.add(orphan)

    ds.reconcile_all([RELAY_NODE])

    # node 42 got created; orphan (99) marked unreachable, not deleted
    assert ds.lookup(42, 1) is not None
    assert orphan.error == "unreachable"
    assert 5005 in [d.id for d in devices]


def test_delete_node_excludes_failed_deletes_from_returned_ids(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)

    # make Indigo's delete blow up
    def boom(dev):
        raise RuntimeError("device in use")
    _indigo.device.delete = boom

    removed = ds.delete_node(42)
    assert removed == []  # nothing actually deleted → don't claim it was
    # index entry retained since the device still exists
    assert ds.lookup(42, 1) == dev_id


def test_partial_creation_is_surfaced_in_result(indigo_env, mock_logger):
    import device_sync
    importlib.reload(device_sync)
    from matter_handlers.registry import HandlerRegistry
    _indigo, devices = indigo_env

    ds = device_sync.DeviceSync(HandlerRegistry(), mock_logger)
    # force device creation to fail
    _indigo.device.create = lambda **kw: (_ for _ in ()).throw(RuntimeError("create failed"))

    result = ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert result["indigoDeviceIds"] == []
    assert result["primaryDeviceId"] is None
    assert result.get("partial") is True
    assert result["failedEndpoints"] == 1
    mock_logger.warning.assert_called()


def test_reconcile_skips_unparseable_node(ds):
    bad = {"no_node_id": True}  # parse_node will raise on int(None)
    # should not raise; bad node skipped, good node created
    ds.reconcile_all([bad, RELAY_NODE])
    assert ds.lookup(42, 1) is not None


def test_create_primes_states_from_node_snapshot(ds, indigo_env):
    _indigo, devices = indigo_env
    # OnOff currently TRUE in get_node → device must prime to True, not the default
    node = {"node_id": 50, "attributes": {"1/6/0": True, "1/29/0": [{"0": 266}]}}
    ds.create_from_raw(node, "On Plug")
    dev_id = ds.lookup(50, 1)
    assert devices[dev_id].states["onOffState"] is True


def test_create_primes_sensor_value(ds, indigo_env):
    _indigo, devices = indigo_env
    # static temperature -28.00 °C must appear immediately (no attribute_updated will come)
    node = {"node_id": 51, "attributes": {"1/1026/0": -2800, "1/29/0": [{"0": 770}]}}
    ds.create_from_raw(node, "Temp")
    dev_id = ds.lookup(51, 1)
    assert devices[dev_id].states["sensorValue"] == -28.0


def test_node_added_event_creates_device(ds, indigo_env):
    _indigo, devices = indigo_env
    node_details = {"node_id": 60, "attributes": {"1/6/0": False, "1/29/0": [{"0": 266}]}}
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_ADDED, node_id=60,
        raw={"event": "node_added", "data": node_details},
    ))
    assert ds.lookup(60, 1) is not None


def test_commission_creates_device_in_room_folder(ds, indigo_env):
    # A suggestedRoom maps to an Indigo device folder, created on demand.
    _indigo, devices = indigo_env
    result = ds.create_from_raw(RELAY_NODE, "Office Plug", "Office")
    dev = devices[result["primaryDeviceId"]]
    assert dev.name == "Office Plug"
    folder = next(f for f in _indigo.devices.folders if f.id == dev.folderId)
    assert folder.name == "Office"


def test_commission_overrides_name_and_room_after_node_added_race(ds, indigo_env):
    """The node_added auto-create must not rob the commission of name/room.

    Reproduces the live race: node_added fires first and creates the device with
    the bare product name and no folder; the commission job then runs with the
    user's chosen name + room and must rename/re-folder the *same* device rather
    than leave the bare one or create a duplicate.
    """
    _indigo, devices = indigo_env
    node_details = {"node_id": 70, "attributes": {"1/6/0": False, "1/29/0": [{"0": 266}]}}

    # 1. node_added wins the race
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_ADDED, node_id=70,
        raw={"event": "node_added", "data": node_details},
    ))
    dev_id = ds.lookup(70, 1)
    assert dev_id is not None
    assert devices[dev_id].name != "Hallway Lamp"  # bare product/fallback name
    assert devices[dev_id].folderId == 0

    # 2. commission job runs with the user's choices
    result = ds.create_from_raw(node_details, "Hallway Lamp", "Hallway")

    # same device, no duplicate, now bearing the chosen name + folder
    assert result["primaryDeviceId"] == dev_id
    assert result["indigoDeviceIds"] == [dev_id]
    dev = devices[dev_id]
    assert dev.name == "Hallway Lamp"
    folder = next(f for f in _indigo.devices.folders if f.id == dev.folderId)
    assert folder.name == "Hallway"


def test_mark_all_unreachable(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    node2 = {"node_id": 99, "attributes": {"1/6/0": False, "1/29/0": [{"0": 266}]}}
    ds.create_from_raw(node2, "Other Plug")
    ids = [ds.lookup(42, 1), ds.lookup(99, 1)]

    ds.mark_all_unreachable()
    for dev_id in ids:
        assert devices[dev_id].errorState == "unreachable"


def test_server_shutdown_event_marks_all_unreachable(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_SERVER_SHUTDOWN,
        raw={"event": "server_shutdown", "data": {}},
    ))
    assert devices[dev_id].errorState == "unreachable"


def test_reconcile_clears_unreachable_on_recovered_node(ds, indigo_env):
    # disconnect marks devices unreachable; the reconnect reconcile must clear it
    # for nodes that are present again, even if no attribute value changed.
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    ds.mark_all_unreachable()
    assert devices[dev_id].errorState == "unreachable"

    ds.reconcile_all([RELAY_NODE])
    assert devices[dev_id].errorState == ""


def test_reconcile_marks_unavailable_node_unreachable(ds, indigo_env):
    # get_nodes returns ALL commissioned nodes; an offline one (available=False)
    # must be marked unreachable, not cleared just because it's present.
    _indigo, devices = indigo_env
    attrs = {"1/6/0": False, "1/29/0": [{"0": 266}]}
    ds.create_from_raw({"node_id": 80, "attributes": attrs}, "Plug")
    dev_id = ds.lookup(80, 1)
    ds.reconcile_all([{"node_id": 80, "available": False, "attributes": attrs}])
    assert devices[dev_id].errorState == "unreachable"


def test_node_updated_availability_toggles_reachability(ds, indigo_env):
    # matter-server fires node_updated on availability changes.
    _indigo, devices = indigo_env
    attrs = {"1/6/0": False, "1/29/0": [{"0": 266}]}
    ds.create_from_raw({"node_id": 81, "attributes": attrs}, "Plug")
    dev_id = ds.lookup(81, 1)

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_UPDATED, node_id=81,
        raw={"event": "node_updated", "data": {"node_id": 81, "available": False, "attributes": attrs}},
    ))
    assert devices[dev_id].errorState == "unreachable"

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_UPDATED, node_id=81,
        raw={"event": "node_updated", "data": {"node_id": 81, "available": True, "attributes": attrs}},
    ))
    assert devices[dev_id].errorState == ""


MULTI_NODE = {
    "node_id": 92,
    "attributes": {
        "1/6/0": False, "1/29/0": [{"0": 266}],   # OnOff on endpoint 1
        "2/6/0": False, "2/29/0": [{"0": 266}],   # OnOff on endpoint 2
    },
}


def test_multi_endpoint_authoritative_rename_after_race(ds, indigo_env):
    # node_added creates both endpoints' devices with bare suffixed names; the
    # commission pass must rename + re-folder BOTH, with no duplicates.
    _indigo, devices = indigo_env
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_ADDED, node_id=92,
        raw={"event": "node_added", "data": MULTI_NODE},
    ))
    id1, id2 = ds.lookup(92, 1), ds.lookup(92, 2)
    assert id1 and id2 and id1 != id2
    assert "(endpoint 1)" in devices[id1].name and "Patio" not in devices[id1].name

    result = ds.create_from_raw(MULTI_NODE, "Patio", "Outside")
    assert sorted(result["indigoDeviceIds"]) == sorted([id1, id2])   # no dupes
    assert devices[id1].name == "Patio (endpoint 1)"
    assert devices[id2].name == "Patio (endpoint 2)"
    outside = next(f.id for f in _indigo.devices.folders if f.name == "Outside")
    assert devices[id1].folderId == outside and devices[id2].folderId == outside


def test_folder_resolution_failure_falls_back_to_folder_0(ds, indigo_env):
    # a folder API failure must not sink device creation
    _indigo, devices = indigo_env

    def boom(_name):
        raise RuntimeError("folder API down")
    _indigo.devices.folder.create = boom

    result = ds.create_from_raw(RELAY_NODE, "Office Plug", "Office")
    dev = devices[result["primaryDeviceId"]]
    assert dev.name == "Office Plug"   # still created
    assert dev.folderId == 0           # degraded gracefully


def test_existing_room_folder_is_reused(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug", "Office")
    node2 = {"node_id": 93, "attributes": {"1/6/0": False, "1/29/0": [{"0": 266}]}}
    ds.create_from_raw(node2, "Other Plug", "Office")
    # second device into the same room reuses the folder, doesn't duplicate it
    assert [f.name for f in _indigo.devices.folders] == ["Office"]


def test_bad_attribute_value_does_not_update_or_raise(ds, indigo_env, mock_logger):
    # a malformed value from the device must not raise out of the handler nor
    # leave the device frozen-without-error — it's caught and logged.
    _indigo, devices = indigo_env
    node = {"node_id": 52, "attributes": {"1/1026/0": -2800, "1/29/0": [{"0": 770}]}}
    ds.create_from_raw(node, "Temp")
    dev_id = ds.lookup(52, 1)
    before = dict(devices[dev_id].states)

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=52, endpoint=1, cluster=1026, attribute=0,
        value="not-a-number",
        raw={"event": "attribute_updated", "data": [52, "1/1026/0", "not-a-number"]},
    ))
    assert devices[dev_id].states == before   # unchanged, no exception
    assert mock_logger.warning.called


def test_malformed_attribute_event_is_logged(ds, mock_logger):
    # truncated path → node_id None → warned + dropped, never applied
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=None,
        raw={"event": "attribute_updated", "data": [1, "1/6"]},
    ))
    assert mock_logger.warning.called


def test_apply_states_clears_stale_error(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    devices[dev_id].setErrorStateOnServer("cmd failed")  # stale error
    # a fresh state update means the device is reachable → error self-heals
    ds.apply_states(dev_id, [{"key": "onOffState", "value": True}])
    assert devices[dev_id].errorState == ""


def test_build_command_dispatches_to_handler(ds, mock_indigo_base):
    import indigo

    class Dev:
        deviceTypeId = "matterRelay"
        pluginProps = {"nodeId": "42", "endpointId": "1"}

    class Action:
        deviceAction = indigo.kDeviceAction.TurnOn

    cmd = ds.build_command(Dev(), Action())
    assert cmd is not None
    assert cmd.command == "On" and cmd.node_id == 42


# ===========================================================================
# fix/#44 — additive endpoint collapse
# ===========================================================================

# Air Quality node: AirQuality + CO2 + PM2.5 + TVOC all on endpoint 1.
# Cluster ids: AirQuality=0x005B(91), CO2=0x040D(1037),
#              PM25=0x042A(1066), TVOC=0x042E(1070)
AQ_NODE = {
    "node_id": 30,
    "attributes": {
        "1/91/0": 2,       # AirQuality MeasuredValue (enum: fair)
        "1/1037/0": 480.0, # CO2 MeasuredValue ppm (float)
        "1/1066/0": 12.5,  # PM2.5 MeasuredValue µg/m³ (float)
        "1/1070/0": 95.0,  # TVOC MeasuredValue (float)
        "1/29/0": [
            {"0": 0x0073},  # AirQuality device type
        ],
    },
}

# Pressure + Flow combo sensor on endpoint 1.
# PressureMeasurement=0x0403(1027), FlowMeasurement=0x0404(1028)
PRESSURE_FLOW_NODE = {
    "node_id": 31,
    "attributes": {
        "1/1027/0": 10130, # Pressure MeasuredValue (int16 × 0.1 hPa = 1013.0 hPa)
        "1/1028/0": 25,    # Flow MeasuredValue (uint16 × 0.1 m³/h = 2.5 m³/h)
        "1/29/0": [{"0": 1027}],
    },
}

# Thermostat + FanControl on endpoint 1 (fan merged in, not a separate device).
THERMOSTAT_FAN_NODE = {
    "node_id": 32,
    "attributes": {
        "1/513/0": 2100,   # Thermostat LocalTemperature (21.00 °C)
        "1/514/0": 1,      # FanControl FanMode
        "1/29/0": [{"0": 769}],  # Thermostat device type
    },
}


def test_aq_node_creates_four_devices(ds, indigo_env):
    """An AQ endpoint with 4 additive clusters produces 4 separate Indigo devices (fix/#44)."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(AQ_NODE, "Air Sensor")
    assert len(result["indigoDeviceIds"]) == 4, (
        f"expected 4 devices, got {len(result['indigoDeviceIds'])}: "
        f"{[devices[d].deviceTypeId for d in result['indigoDeviceIds']]}"
    )
    type_ids = {devices[d].deviceTypeId for d in result["indigoDeviceIds"]}
    assert type_ids == {"matterAirQualitySensor", "matterCO2Sensor", "matterPM25Sensor", "matterTVOCSensor"}


def test_aq_node_lookup_by_type(ds, indigo_env):
    """lookup(node, ep, type_id) returns the correct device for each AQ type."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    aq_id  = ds.lookup(30, 1, "matterAirQualitySensor")
    co2_id = ds.lookup(30, 1, "matterCO2Sensor")
    pm_id  = ds.lookup(30, 1, "matterPM25Sensor")
    tvoc_id = ds.lookup(30, 1, "matterTVOCSensor")
    assert None not in (aq_id, co2_id, pm_id, tvoc_id)
    assert len({aq_id, co2_id, pm_id, tvoc_id}) == 4  # all distinct


def test_aq_cluster_update_routes_to_correct_device(ds, indigo_env):
    """A CO2 attribute update goes to the CO2 device, NOT the AirQuality device (fix/#44)."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    aq_id  = ds.lookup(30, 1, "matterAirQualitySensor")
    co2_id = ds.lookup(30, 1, "matterCO2Sensor")

    # CO2 cluster (0x040D) attribute update
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=30, endpoint=1, cluster=0x040D, attribute=0x0000, value=600.0,
    ))
    # CO2 device updated
    assert devices[co2_id].states.get("sensorValue") == 600.0
    # AQ device's sensorValue was primed as 2 (fair); must NOT have been overwritten by CO2 value
    # (If routing is wrong the AQ device's sensorValue would be 600.0)
    assert devices[aq_id].states.get("sensorValue") != 600.0


def test_aq_pm25_update_routes_to_pm25_device(ds, indigo_env):
    """A PM2.5 cluster update goes to the PM25 device."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    pm_id   = ds.lookup(30, 1, "matterPM25Sensor")
    tvoc_id = ds.lookup(30, 1, "matterTVOCSensor")

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=30, endpoint=1, cluster=0x042A, attribute=0x0000, value=22.3,
    ))
    assert devices[pm_id].states.get("sensorValue") == 22.3
    # TVOC device not affected
    assert devices[tvoc_id].states.get("sensorValue") != 22.3


def test_aq_priming_does_not_cross_contaminate(ds, indigo_env):
    """Priming the AQ device does not write CO2/PM25/TVOC values into it (fix/#44)."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    aq_id = ds.lookup(30, 1, "matterAirQualitySensor")
    # The AQ node snapshot has CO2=480.0 — this must NOT have been primed into the AQ device
    # (480.0 is not a valid airQuality enum integer in the 0-6 range, so its presence would
    # expose the contamination clearly)
    aq_dev = devices[aq_id]
    assert aq_dev.states.get("sensorValue") != 480.0, (
        "AirQuality device was primed with CO2 value — cross-endpoint contamination"
    )


def test_pressure_flow_creates_two_devices(ds, indigo_env):
    """A Pressure+Flow endpoint creates two separate Indigo devices (fix/#44)."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(PRESSURE_FLOW_NODE, "Combo Sensor")
    assert len(result["indigoDeviceIds"]) == 2
    type_ids = {devices[d].deviceTypeId for d in result["indigoDeviceIds"]}
    assert type_ids == {"matterPressureSensor", "matterFlowSensor"}


def test_flow_update_goes_to_flow_device_not_pressure(ds, indigo_env):
    """Flow cluster update goes to the Flow device, not the Pressure device (fix/#44)."""
    _indigo, devices = indigo_env
    ds.create_from_raw(PRESSURE_FLOW_NODE, "Combo Sensor")
    pressure_id = ds.lookup(31, 1, "matterPressureSensor")
    flow_id     = ds.lookup(31, 1, "matterFlowSensor")

    # Flow update: raw 25 → 2.5 m³/h
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=31, endpoint=1, cluster=0x0404, attribute=0x0000, value=25,
    ))
    assert devices[flow_id].states.get("sensorValue") == 2.5
    # Pressure device must not have been updated with the flow value
    assert devices[pressure_id].states.get("sensorValue") != 2.5


def test_pressure_update_goes_to_pressure_device_not_flow(ds, indigo_env):
    """Pressure cluster update goes to Pressure device."""
    _indigo, devices = indigo_env
    ds.create_from_raw(PRESSURE_FLOW_NODE, "Combo Sensor")
    pressure_id = ds.lookup(31, 1, "matterPressureSensor")
    flow_id     = ds.lookup(31, 1, "matterFlowSensor")

    # Pressure update: raw 10200 → 10200.0 hPa
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=31, endpoint=1, cluster=0x0403, attribute=0x0000, value=10200,
    ))
    assert devices[pressure_id].states.get("sensorValue") == 10200.0
    assert devices[flow_id].states.get("sensorValue") != 10200.0


def test_thermostat_fan_endpoint_creates_exactly_one_device(ds, indigo_env):
    """Thermostat+FanControl endpoint creates exactly 1 device (fan merged into thermostat)."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(THERMOSTAT_FAN_NODE, "Thermostat")
    assert len(result["indigoDeviceIds"]) == 1
    dev_id = result["primaryDeviceId"]
    assert devices[dev_id].deviceTypeId == "matterThermostat"


def test_thermostat_fan_mode_is_primed_from_snapshot(ds, indigo_env):
    """FanControl attr in the node snapshot must prime hvacFanMode into the thermostat.

    Regression test for the merge-into sibling-skip bug: FanControlHandler carries
    device_type_id='matterFan' (its standalone role), but when co-located with a
    Thermostat it shares the matterThermostat device.  The skip must NOT fire because
    'matterFan' is absent from the endpoint's type-map — only 'matterThermostat' is
    there.  Without the fix, _prime_states skips FanControl unconditionally on type
    mismatch, leaving hvacFanMode unset after device creation.
    """
    _indigo, devices = indigo_env
    # THERMOSTAT_FAN_NODE has "1/514/0": 1 = FanMode=1 in its snapshot
    result = ds.create_from_raw(THERMOSTAT_FAN_NODE, "Thermostat")
    dev_id = result["primaryDeviceId"]
    assert devices[dev_id].deviceTypeId == "matterThermostat"
    assert "hvacFanMode" in devices[dev_id].states, (
        "hvacFanMode was not primed from the node snapshot — "
        "merge-into FanControl was incorrectly skipped during _prime_states"
    )


def test_aq_priming_isolation_skips_sibling_types(ds, indigo_env):
    """Priming one AQ sub-device does NOT apply sibling-type values to it.

    Confirms the pressure-vs-flow / AQ cross-contamination guard still fires when
    the sibling type DOES exist as a separate device on the endpoint (the case where
    the skip IS correct, unlike the merge-into thermostat+fan case).
    """
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    aq_id  = ds.lookup(30, 1, "matterAirQualitySensor")
    co2_id = ds.lookup(30, 1, "matterCO2Sensor")
    assert aq_id is not None and co2_id is not None and aq_id != co2_id
    # AQ_NODE snapshot has CO2=480.0; the AQ device must NOT have been primed with it
    assert devices[aq_id].states.get("sensorValue") != 480.0, (
        "AirQuality device was primed with CO2 value — sibling-type skip not firing"
    )
    # CO2 device must have been primed with its own value
    assert devices[co2_id].states.get("sensorValue") == 480.0


def test_thermostat_fan_mode_update_goes_to_thermostat(ds, indigo_env):
    """FanControl cluster update on a thermostat endpoint routes to the thermostat device."""
    _indigo, devices = indigo_env
    import indigo as _indigo_mod
    ds.create_from_raw(THERMOSTAT_FAN_NODE, "Thermostat")
    dev_id = ds.lookup(32, 1)  # only one device, no type needed
    assert dev_id is not None

    # FanMode 5 = Auto
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=32, endpoint=1, cluster=0x0202, attribute=0x0000, value=5,
    ))
    # The thermostat device should have received hvacFanMode
    assert "hvacFanMode" in devices[dev_id].states


def test_aq_node_idempotent_second_create(ds, indigo_env):
    """A second create_from_raw on the same AQ node is idempotent — no new devices."""
    _indigo, devices = indigo_env
    result1 = ds.create_from_raw(AQ_NODE, "Air Sensor")
    ids1 = sorted(result1["indigoDeviceIds"])
    result2 = ds.create_from_raw(AQ_NODE, "Air Sensor")
    ids2 = sorted(result2["indigoDeviceIds"])
    assert ids1 == ids2


def test_aq_node_delete_node_removes_all_four(ds, indigo_env):
    """delete_node removes all 4 AQ devices (fix/#44 — nested map iteration)."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(AQ_NODE, "Air Sensor")
    deleted = ds.delete_node(30)
    assert len(deleted) == 4
    assert sorted(deleted) == sorted(result["indigoDeviceIds"])
    # None should be in the index any more
    assert ds.lookup(30, 1) is None


def test_aq_node_mark_unreachable_marks_all(ds, indigo_env):
    """mark_unreachable(node_id) marks all 4 AQ devices unreachable."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    ds.mark_unreachable(30)
    for type_id in ("matterAirQualitySensor", "matterCO2Sensor", "matterPM25Sensor", "matterTVOCSensor"):
        dev_id = ds.lookup(30, 1, type_id)
        assert devices[dev_id].errorState == "unreachable", f"{type_id} not marked unreachable"


def test_aq_node_mark_endpoint_unreachable_marks_all(ds, indigo_env):
    """mark_endpoint_unreachable marks ALL devices on the additive endpoint."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    ds.mark_endpoint_unreachable(30, 1)
    for type_id in ("matterAirQualitySensor", "matterCO2Sensor", "matterPM25Sensor", "matterTVOCSensor"):
        dev_id = ds.lookup(30, 1, type_id)
        assert devices[dev_id].errorState == "unreachable", f"{type_id} not marked unreachable"


def test_aq_node_endpoint_removed_event_marks_all(ds, indigo_env):
    """endpoint_removed event marks all additive devices on that endpoint unreachable."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AQ_NODE, "Air Sensor")
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ENDPOINT_REMOVED,
        node_id=30, endpoint=1,
        raw={"event": "endpoint_removed", "data": {"node_id": 30, "endpoint_id": 1}},
    ))
    for type_id in ("matterAirQualitySensor", "matterCO2Sensor", "matterPM25Sensor", "matterTVOCSensor"):
        dev_id = ds.lookup(30, 1, type_id)
        assert devices[dev_id].errorState == "unreachable"


def test_battery_fanout_reaches_all_devices_on_aq_endpoint(ds, indigo_env):
    """Node-scoped PowerSource fan-out reaches all additive devices on an endpoint (fix/#44)."""
    _indigo, devices = indigo_env

    # AQ node + PowerSource on endpoint 0
    aq_battery_node = {
        "node_id": 33,
        "attributes": {
            "0/47/12": 120,        # BatPercentRemaining = 60 %
            "1/91/0": 1,           # AirQuality: good
            "1/1037/0": 400.0,     # CO2
            "1/1066/0": 5.0,       # PM2.5
            "1/1070/0": 50.0,      # TVOC
            "1/29/0": [{"0": 0x0073}],
        },
    }

    original_create = _indigo.device.create

    def create_with_battery(**kw):
        dev = original_create(**kw)
        dev.states["batteryLevel"] = 0
        return dev

    _indigo.device.create = create_with_battery
    ds.create_from_raw(aq_battery_node, "AQ Sensor")

    # Send a battery update
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=33, endpoint=0, cluster=0x002F, attribute=0x000C, value=160,
    ))

    # All 4 devices on endpoint 1 should receive batteryLevel = 80
    for type_id in ("matterAirQualitySensor", "matterCO2Sensor", "matterPM25Sensor", "matterTVOCSensor"):
        dev_id = ds.lookup(33, 1, type_id)
        assert dev_id is not None
        assert devices[dev_id].states.get("batteryLevel") == 80, (
            f"{type_id}: expected batteryLevel=80, got {devices[dev_id].states.get('batteryLevel')}"
        )
