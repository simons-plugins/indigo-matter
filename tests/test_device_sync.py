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

# Real Indigo auto-derives these built-in states from Supports* props, both at
# device CREATION and at any later pluginProps replace. FakeDev seeds them in
# both places so handler update guards (e.g. ElectricalPowerHandler's
# `"curEnergyLevel" not in indigo_dev.states`) behave like the real server —
# issue #79's priming/live-routing tests need this true immediately after
# creation, not only after a reconcile-triggered replacePluginPropsOnServer.
_SUPPORTS_TO_STATE = {
    "SupportsPowerMeter": "curEnergyLevel",
    "SupportsEnergyMeter": "accumEnergyTotal",
    "SupportsBatteryLevel": "batteryLevel",
    "SupportsSensorValue": "sensorValue",
}


class FakeDev:
    def __init__(self, dev_id, name, device_type_id, props):
        self.id = dev_id
        self.name = name
        self.deviceTypeId = device_type_id
        self.pluginProps = props
        self.states = {}
        for prop_key, state_key in _SUPPORTS_TO_STATE.items():
            if props.get(prop_key):
                self.states[state_key] = 0  # Indigo-style initial value
        self.error = None
        self.errorState = ""
        self.folderId = 0
        # Real Indigo derives the list display from Supports* props at CREATION
        # and caches it (issue #56) — approximate the precedence rule verified
        # live on jarvis: a True Supports* wins; with BOTH explicitly False the
        # Devices.xml UiDisplayStateId applies ("uiDisplayState" stands in for
        # it here). Deliberately do NOT re-derive in replacePluginPropsOnServer
        # below (models the pessimistic cached case the warn path exists for).
        if props.get("SupportsSensorValue"):
            self.displayStateId = "sensorValue"
        elif "SupportsOnState" in props and not props.get("SupportsOnState"):
            self.displayStateId = "uiDisplayState"
        else:
            self.displayStateId = "onOffState"

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

    def replacePluginPropsOnServer(self, new_props):
        # Real Indigo updates pluginProps and rebuilds device states from Supports*
        # entries.  The fake merges the new props and, for each Supports* key that
        # transitions to True, seeds the corresponding state so handler guards pass.
        self.pluginProps = dict(new_props)
        self.replaced_props = True
        # Simulate Indigo auto-creating states for Supports* props.
        for prop_key, state_key in _SUPPORTS_TO_STATE.items():
            if new_props.get(prop_key) and state_key not in self.states:
                self.states[state_key] = 0  # Indigo-style initial value


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
    # Two identical switches → role-numbered suffix, not the bare product name.
    assert "- Switch 1" in devices[id1].name and "Patio" not in devices[id1].name

    result = ds.create_from_raw(MULTI_NODE, "Patio", "Outside")
    assert sorted(result["indigoDeviceIds"]) == sorted([id1, id2])   # no dupes
    assert devices[id1].name == "Patio - Switch 1"
    assert devices[id2].name == "Patio - Switch 2"
    outside = next(f.id for f in _indigo.devices.folders if f.name == "Outside")
    assert devices[id1].folderId == outside and devices[id2].folderId == outside


MULTI_FUNCTION_NODE = {
    "node_id": 77,
    "attributes": {
        "0/40/3": "HomePod mini",   # BasicInformation ProductName
        "1/1026/0": 2150,           # TemperatureMeasurement MeasuredValue = 21.5 °C
        "1/1029/0": 4750,           # RelativeHumidity MeasuredValue = 47.5 %
    },
}


def test_multi_function_node_names_by_role_not_endpoint(ds, indigo_env):
    # A HomePod exposing temperature + humidity must read "HomePod - Temperature"
    # / "HomePod - Humidity", never "HomePod (endpoint 1)".
    _indigo, devices = indigo_env
    ds.create_from_raw(MULTI_FUNCTION_NODE, "HomePod")
    temp_id = ds.lookup(77, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(77, 1, "matterHumiditySensor")
    assert temp_id and hum_id and temp_id != hum_id
    assert devices[temp_id].name == "HomePod - Temperature"
    assert devices[hum_id].name == "HomePod - Humidity"
    # The reading is primed in (formatted) and the product stamped as the Model
    # so the two devices share a Model column entry.
    assert devices[temp_id].states["sensorValue"] == 21.5
    assert devices[temp_id].model == "HomePod mini"
    assert devices[hum_id].model == "HomePod mini"


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


# ===========================================================================
# fix/#45 — re-assert capability props at reconcile (mid-interview creation)
# ===========================================================================

# Energy Plug node: OnOff + ElectricalPowerMeasurement (0x0090) + ElectricalEnergyMeasurement (0x0091)
# This represents node 0x20 (32) from the live-evidence in issue #45.
ENERGY_PLUG_NODE = {
    "node_id": 0x20,
    "available": True,
    "attributes": {
        "0/40/1": "ACME",
        "0/40/3": "Energy Plug",
        "1/29/0": [{"0": 266}],          # OnOffPlugInUnit
        "1/6/0": False,                  # OnOff
        "1/144/8": 1200,                 # ElectricalPowerMeasurement.ActivePower = 1200 mW
        "1/145/1": {"energy": 3_600_000},  # ElectricalEnergyMeasurement.CumulativeEnergyImported
    },
}


def test_reconcile_reasserts_missing_meter_props_and_logs_info(ds, indigo_env, mock_logger):
    """Device created mid-interview (without meter props) gains them at reconcile.

    Reproduces the live evidence from issue #45: a relay device created via
    node_added before the electrical clusters were visible lacks SupportsPowerMeter /
    SupportsEnergyMeter, so curEnergyLevel / accumEnergyTotal never exist.
    After reconcile the props must be present and the INFO log line must be emitted.
    """
    _indigo, devices = indigo_env
    # Simulate a mid-interview creation: relay created WITHOUT meter props.
    bare_node = {
        "node_id": 0x20,
        "available": True,
        "attributes": {
            "1/29/0": [{"0": 266}],
            "1/6/0": False,
        },
    }
    ds.create_from_raw(bare_node, "Energy Plug")
    dev_id = ds.lookup(0x20, 1)
    assert dev_id is not None
    dev = devices[dev_id]
    # Verify props are absent (the mid-interview state)
    assert not dev.pluginProps.get("SupportsPowerMeter")
    assert not dev.pluginProps.get("SupportsEnergyMeter")

    # Reconcile with the full cluster snapshot — props must be added.
    mock_logger.info.reset_mock()
    ds.reconcile_all([ENERGY_PLUG_NODE])

    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    # States must now exist so handler updates flow through.
    assert "curEnergyLevel" in dev.states
    assert "accumEnergyTotal" in dev.states
    # INFO line must be emitted — the only visible evidence of the self-heal.
    assert mock_logger.info.called
    info_calls = [mock_logger.info.call_args_list[i][0] for i in range(len(mock_logger.info.call_args_list))]
    reassert_msgs = [fmt % tuple(args) for fmt, *args in info_calls if "re-asserted" in fmt]
    assert reassert_msgs, "expected at least one INFO 're-asserted capability props' log line"


def test_reconcile_reassert_is_idempotent(ds, indigo_env, mock_logger):
    """A second reconcile pass finds nothing missing and does not call replacePluginPropsOnServer."""
    _indigo, devices = indigo_env
    bare_node = {
        "node_id": 0x20, "available": True,
        "attributes": {"1/29/0": [{"0": 266}], "1/6/0": False},
    }
    ds.create_from_raw(bare_node, "Energy Plug")
    dev_id = ds.lookup(0x20, 1)

    # First reconcile — props gain added, replaced_props set.
    ds.reconcile_all([ENERGY_PLUG_NODE])
    assert devices[dev_id].pluginProps.get("SupportsPowerMeter") is True
    devices[dev_id].replaced_props = False  # reset sentinel

    # Second reconcile — props already present, no further replacePluginPropsOnServer.
    mock_logger.info.reset_mock()
    ds.reconcile_all([ENERGY_PLUG_NODE])
    assert not getattr(devices[dev_id], "replaced_props", False), (
        "replacePluginPropsOnServer called again on second pass — not idempotent"
    )
    # No 're-asserted' INFO line on the second pass.
    info_calls = [mock_logger.info.call_args_list[i][0] for i in range(len(mock_logger.info.call_args_list))]
    reassert_msgs = [fmt % tuple(args) for fmt, *args in info_calls if "re-asserted" in fmt]
    assert not reassert_msgs, "unexpected 're-asserted' log on idempotent second pass"


def test_reconcile_reassert_device_with_props_already_set_is_untouched(ds, indigo_env):
    """A device created with correct props already is not modified by reassert."""
    _indigo, devices = indigo_env
    # Create with full cluster snapshot (fast path — props already set at creation).
    ds.create_from_raw(ENERGY_PLUG_NODE, "Energy Plug")
    dev_id = ds.lookup(0x20, 1)
    dev = devices[dev_id]
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    dev.replaced_props = False

    ds.reconcile_all([ENERGY_PLUG_NODE])
    assert not getattr(dev, "replaced_props", False), (
        "replacePluginPropsOnServer was called on a device that already had props set"
    )


def test_reconcile_reassert_prop_failure_continues_to_next_device(ds, indigo_env, mock_logger):
    """A replacePluginPropsOnServer failure must not abort reconcile for other devices."""
    _indigo, devices = indigo_env
    # Two relay nodes, both created mid-interview without meter props.
    bare_node_20 = {
        "node_id": 0x20, "available": True,
        "attributes": {"1/29/0": [{"0": 266}], "1/6/0": False},
    }
    bare_node_21 = {
        "node_id": 0x21, "available": True,
        "attributes": {"1/29/0": [{"0": 266}], "1/6/0": False},
    }
    full_node_21 = {
        "node_id": 0x21, "available": True,
        "attributes": {
            "1/29/0": [{"0": 266}], "1/6/0": False,
            "1/144/8": 800, "1/145/1": {"energy": 1_000_000},
        },
    }
    ds.create_from_raw(bare_node_20, "Plug A")
    ds.create_from_raw(bare_node_21, "Plug B")
    dev_id_20 = ds.lookup(0x20, 1)
    dev_id_21 = ds.lookup(0x21, 1)

    # Make the first device's replacePluginPropsOnServer raise.
    def boom(props):
        raise RuntimeError("props API down")
    devices[dev_id_20].replacePluginPropsOnServer = boom

    # Reconcile both nodes — first device's props fail, second must still be healed.
    ds.reconcile_all([ENERGY_PLUG_NODE, full_node_21])

    # The failure must have been logged.
    assert mock_logger.warning.called

    # The second device must still have been healed.
    assert devices[dev_id_21].pluginProps.get("SupportsPowerMeter") is True


def test_reconcile_reasserts_battery_prop_from_power_source_on_other_endpoint(ds, indigo_env, mock_logger):
    """SupportsBatteryLevel is added when PowerSource is on a DIFFERENT endpoint.

    Mirrors create_devices' node-level central setdefault: any endpoint carrying
    cluster 0x002F causes SupportsBatteryLevel=True on all devices of that node,
    regardless of which endpoint the device lives on.
    """
    _indigo, devices = indigo_env
    # Create a relay without SupportsBatteryLevel (PowerSource absent at creation time).
    bare_node = {
        "node_id": 0x30, "available": True,
        "attributes": {"1/29/0": [{"0": 266}], "1/6/0": False},
    }
    ds.create_from_raw(bare_node, "Battery Relay")
    dev_id = ds.lookup(0x30, 1)
    dev = devices[dev_id]
    assert not dev.pluginProps.get("SupportsBatteryLevel")

    # Reconcile with a node that now has PowerSource on endpoint 0.
    full_node = {
        "node_id": 0x30, "available": True,
        "attributes": {
            "0/47/12": 200,               # PowerSource.BatPercentRemaining = 100 %
            "1/29/0": [{"0": 266}], "1/6/0": False,
        },
    }
    ds.reconcile_all([full_node])
    assert dev.pluginProps.get("SupportsBatteryLevel") is True
    assert "batteryLevel" in dev.states


def test_reassert_does_not_add_meter_props_to_non_relay_types(ds, indigo_env):
    """SupportsPowerMeter/SupportsEnergyMeter must NOT be injected onto non-relay types.

    A sensor device (e.g. matterAirQualitySensor) on an endpoint that happens to
    also advertise the electrical clusters must NOT receive meter props — those props
    only make sense for matterRelay/matterDimmer/matterColorDimmer.
    """
    _indigo, devices = indigo_env
    # Simulate an AQ sensor created mid-interview, endpoint also happens to carry
    # electrical clusters (contrived but defensive).
    aq_node_bare = {
        "node_id": 0x40, "available": True,
        "attributes": {
            "1/29/0": [{"0": 0x0073}],
            "1/91/0": 1,    # AirQuality: good
        },
    }
    ds.create_from_raw(aq_node_bare, "AQ Sensor")
    aq_id = ds.lookup(0x40, 1, "matterAirQualitySensor")
    assert aq_id is not None

    aq_node_full = {
        "node_id": 0x40, "available": True,
        "attributes": {
            "1/29/0": [{"0": 0x0073}],
            "1/91/0": 1,
            "1/144/8": 500,              # ElectricalPowerMeasurement (0x0090)
            "1/145/1": {"energy": 100},  # ElectricalEnergyMeasurement (0x0091)
        },
    }
    devices[aq_id].replaced_props = False
    ds.reconcile_all([aq_node_full])

    # Meter props must NOT have been injected.
    assert not devices[aq_id].pluginProps.get("SupportsPowerMeter"), (
        "SupportsPowerMeter must not be added to matterAirQualitySensor"
    )
    assert not devices[aq_id].pluginProps.get("SupportsEnergyMeter"), (
        "SupportsEnergyMeter must not be added to matterAirQualitySensor"
    )
    # replacePluginPropsOnServer must not have been called at all for this device.
    assert not getattr(devices[aq_id], "replaced_props", False), (
        "replacePluginPropsOnServer was unexpectedly called on a non-relay type"
    )


def test_list_nodes_groups_devices_per_node(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    nodes = ds.list_nodes()
    assert len(nodes) == 1
    node_id, names = nodes[0]
    assert node_id == 42
    assert names == ["Office Plug"]


def test_list_nodes_survives_out_of_band_delete(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    devices._by_id.pop(dev_id)  # deleted out-of-band; index still has it
    nodes = ds.list_nodes()
    assert nodes[0][0] == 42
    assert nodes[0][1] == [f"device {dev_id}"]


def test_list_nodes_orders_multiple_nodes_and_groups_by_node(ds, indigo_env):
    # Grouping must be by node id alone — not by (node, endpoint) key — and
    # ordering must hold with more than one node.
    import copy
    node7 = copy.deepcopy(RELAY_NODE)
    node7["node_id"] = 7
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    ds.create_from_raw(node7, "Lamp Plug")
    nodes = ds.list_nodes()
    assert [nid for nid, _names in nodes] == [7, 42]
    assert nodes[0][1] == ["Lamp Plug"]
    assert nodes[1][1] == ["Office Plug"]


def test_list_nodes_merges_multiple_devices_into_one_node_entry(ds, indigo_env):
    # A node whose endpoint creates several Indigo devices (pressure + flow)
    # must yield ONE picker entry listing all of them, never one per device.
    ds.create_from_raw(PRESSURE_FLOW_NODE, "Combo Sensor")
    nodes = ds.list_nodes()
    assert len(nodes) == 1
    node_id, names = nodes[0]
    assert node_id == 31
    assert len(names) == 2


# ===========================================================================
# fix/#56 — value sensors must display sensorValue, not on/off
# ===========================================================================

# Aqara FP300-style endpoint: a temperature sensor (the live evidence in
# issue #56 — created with no Supports* props, Indigo defaulted the display
# to onOffState and the device list showed "off" instead of the reading).
TEMP_SENSOR_NODE = {
    "node_id": 0x40,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "1/29/0": [{"0": 770}],   # TemperatureSensor
        "1/1026/0": 2400,         # 24.0 °C
    },
}

OCCUPANCY_NODE = {
    "node_id": 0x41,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "1/29/0": [{"0": 263}],   # OccupancySensor
        "1/1030/0": 1,            # occupied
    },
}


def _strip_display_props(dev):
    """Rewind a device to its pre-#56 creation shape."""
    props = dict(dev.pluginProps)
    props.pop("SupportsSensorValue", None)
    props.pop("SupportsOnState", None)
    dev.pluginProps = props
    dev.displayStateId = "onOffState"
    dev.replaced_props = False


def test_value_sensor_created_with_sensor_value_display_props(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    assert dev.pluginProps.get("SupportsSensorValue") is True
    assert dev.pluginProps.get("SupportsOnState") is False
    assert dev.displayStateId == "sensorValue"


def test_binary_sensor_created_with_on_state_display_props(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(OCCUPANCY_NODE, "Landing Presence")
    dev = devices[ds.lookup(0x41, 1)]
    assert dev.pluginProps.get("SupportsOnState") is True
    assert dev.pluginProps.get("SupportsSensorValue") is False
    assert dev.displayStateId == "onOffState"


def test_reconcile_reasserts_display_props_on_pre_fix_sensor(ds, indigo_env, mock_logger):
    """A sensor created before the fix gains explicit display props at reconcile.

    Absence of SupportsOnState means Indigo's sensor default (True), so the
    re-assert must write BOTH keys explicitly, not only the truthy one.
    No stale-display warning may fire on this pass — Indigo may legitimately
    still be applying the props replace.
    """
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    _strip_display_props(dev)

    mock_logger.warning.reset_mock()
    ds.reconcile_all([TEMP_SENSOR_NODE])

    assert dev.replaced_props is True
    assert dev.pluginProps.get("SupportsSensorValue") is True
    assert dev.pluginProps.get("SupportsOnState") is False
    display_warnings = [
        c for c in mock_logger.warning.call_args_list if "display" in c[0][0]
    ]
    assert not display_warnings, "stale-display warning fired on the same pass as the props fix"


def test_second_reconcile_warns_when_display_state_stays_cached(ds, indigo_env, mock_logger):
    """Indigo caches displayStateId at creation; if a props replace does not
    re-derive it, the NEXT reconcile must warn, naming the device and the
    delete-and-reload remedy."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    _strip_display_props(dev)

    ds.reconcile_all([TEMP_SENSOR_NODE])   # heals props; fake keeps display cached
    assert dev.displayStateId == "onOffState"

    mock_logger.warning.reset_mock()
    ds.reconcile_all([TEMP_SENSOR_NODE])
    msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.warning.call_args_list]
    stale = [m for m in msgs if "delete the Indigo device" in m]
    assert stale, f"expected stale-display warning, got: {msgs}"
    assert "Landing Sensor" in stale[0]


def test_healthy_value_sensor_reconcile_is_quiet(ds, indigo_env, mock_logger):
    """A post-fix device (correct props, sensorValue display) must not warn."""
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    mock_logger.warning.reset_mock()
    ds.reconcile_all([TEMP_SENSOR_NODE])
    msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("display" in m for m in msgs)


def test_binary_sensor_on_off_display_never_warns(ds, indigo_env, mock_logger):
    """on/off IS the correct display for binary sensors — no nag."""
    ds.create_from_raw(OCCUPANCY_NODE, "Landing Presence")
    mock_logger.warning.reset_mock()
    ds.reconcile_all([OCCUPANCY_NODE])
    msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("display" in m for m in msgs)


# FP300-like: temperature sensor + PowerSource (battery) on the same node —
# the real-world configuration behind issue #56.
TEMP_BATTERY_NODE = {
    "node_id": 0x42,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "0/47/12": 200,           # PowerSource.BatPercentRemaining (node-scoped)
        "1/29/0": [{"0": 770}],   # TemperatureSensor
        "1/1026/0": 2400,
    },
}


def test_pre_fix_battery_sensor_heals_battery_and_display_in_one_replace(ds, indigo_env, mock_logger):
    """The flagship issue #56 case: a battery-powered pre-fix value sensor must
    gain SupportsBatteryLevel (add-only cluster cap) AND both display props
    (exact assertion) in a single replacePluginPropsOnServer call, with no
    stale-display warning on the pass that healed it."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_BATTERY_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x42, 1)]
    _strip_display_props(dev)
    props = dict(dev.pluginProps)
    props.pop("SupportsBatteryLevel", None)
    dev.pluginProps = props

    calls = []
    real_replace = dev.replacePluginPropsOnServer
    dev.replacePluginPropsOnServer = lambda p: (calls.append(dict(p)), real_replace(p))

    mock_logger.warning.reset_mock()
    ds.reconcile_all([TEMP_BATTERY_NODE])

    assert len(calls) == 1, f"expected one combined replace, got {len(calls)}"
    assert dev.pluginProps.get("SupportsBatteryLevel") is True
    assert dev.pluginProps.get("SupportsSensorValue") is True
    assert dev.pluginProps.get("SupportsOnState") is False
    msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("display" in m for m in msgs)


def test_reassert_corrects_present_but_wrong_display_props(ds, indigo_env):
    """The exact-assertion contract: a value sensor whose props are PRESENT but
    wrong (not merely absent) must be corrected."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    props = dict(dev.pluginProps)
    props["SupportsSensorValue"] = False
    props["SupportsOnState"] = True
    dev.pluginProps = props
    dev.replaced_props = False

    ds.reconcile_all([TEMP_SENSOR_NODE])
    assert dev.replaced_props is True
    assert dev.pluginProps.get("SupportsSensorValue") is True
    assert dev.pluginProps.get("SupportsOnState") is False


def test_unknown_device_type_in_index_is_processed_quietly(ds, indigo_env, mock_logger):
    """A stale index entry whose deviceTypeId no handler owns (e.g. after a type
    rename) must neither crash the re-assert loop nor log a could-not-re-assert
    warning — and must not block healing of other devices. The only change it
    may receive is the createdTypeId stamp (the #58 type-edit guard heal)."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    ghost = FakeDev(devices.next_id(), "Ghost", "matterRetiredType",
                    {"nodeId": str(0x40), "endpointId": "1"})
    devices.add(ghost)

    mock_logger.warning.reset_mock()
    ds.reconcile_all([TEMP_SENSOR_NODE])

    msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("could not re-assert" in m for m in msgs), msgs
    # The stamp heal is the one legitimate write; no other props may change.
    assert ghost.pluginProps.get("createdTypeId") == "matterRetiredType"
    assert "SupportsSensorValue" not in ghost.pluginProps


def test_props_replace_that_does_not_persist_warns_instead_of_claiming_success(ds, indigo_env, mock_logger):
    """Never report success when the op only half-happened: if Indigo silently
    drops the replaced props, the INFO success line must NOT be logged and a
    did-not-persist warning must name the keys."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    _strip_display_props(dev)
    dev.replacePluginPropsOnServer = lambda p: None  # accepted, never applied

    mock_logger.info.reset_mock()
    mock_logger.warning.reset_mock()
    ds.reconcile_all([TEMP_SENSOR_NODE])

    info_msgs = [c[0][0] for c in mock_logger.info.call_args_list]
    assert not any("re-asserted" in m for m in info_msgs), info_msgs
    persist_warnings = [
        c[0] for c in mock_logger.warning.call_args_list if "did not persist" in c[0][0]
    ]
    assert persist_warnings, "expected a did-not-persist warning"
    formatted = persist_warnings[0][0] % tuple(persist_warnings[0][1:])
    assert "SupportsSensorValue" in formatted and "SupportsOnState" in formatted


# ===========================================================================
# fix/#58 — matterUnknown fallback + createdTypeId stamp
# ===========================================================================

# A node whose only device-bearing endpoint exposes clusters we don't handle
# (RVC RunMode 0x0054 = 84). Must surface as a placeholder, not vanish.
UNKNOWN_NODE = {
    "node_id": 0x50,
    "available": True,
    "attributes": {
        "0/40/1": "Roborock",
        "0/40/3": "RoboVac",
        "1/29/0": [{"0": 116}],
        "1/84/0": 0,
    },
}

# Root endpoint only — nothing device-bearing anywhere. Must create nothing.
ROOT_ONLY_NODE = {
    "node_id": 0x51,
    "available": True,
    "attributes": {"0/40/1": "ACME", "0/29/0": [{"0": 22}]},
}

# Endpoint whose clusters are all merge-only/utility (electrical measurement)
# and no actuator exists anywhere on the node: no matterUnknown placeholder
# (nothing device-shaped for the unmapped-cluster path to surface) — but
# since issue #79, attribution fails (no actuator to link to) so it gets the
# fallback matterEnergyMeter device instead of no device at all.
ELECTRICAL_ONLY_NODE = {
    "node_id": 0x52,
    "available": True,
    "attributes": {
        "0/40/1": "ACME",
        "1/29/0": [{"0": 1296}],
        "1/144/8": 1200,
        "1/145/1": {"energy": 1},
    },
}


def test_unknown_endpoint_creates_placeholder_not_nothing(ds, indigo_env, mock_logger):
    _indigo, devices = indigo_env
    result = ds.create_from_raw(UNKNOWN_NODE, "Vacuum")
    assert result["indigoDeviceIds"], "unknown device vanished — commission would claim success with no devices"
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterUnknown"
    assert "0x0054" in dev.pluginProps["supportedClusters"]
    assert dev.states.get("reachable") is True
    assert dev.pluginProps["createdTypeId"] == "matterUnknown"
    # The report-invite must be logged.
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert any("please report" in m for m in info_msgs)


def test_unknown_placeholder_is_idempotent(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(UNKNOWN_NODE, "Vacuum")
    first = ds.lookup(0x50, 1)
    result = ds.create_from_raw(UNKNOWN_NODE, "Vacuum")
    assert result["indigoDeviceIds"] == [first]


def test_root_only_node_creates_nothing(ds, indigo_env):
    _indigo, devices = indigo_env
    result = ds.create_from_raw(ROOT_ONLY_NODE, "Mystery")
    assert result["indigoDeviceIds"] == []
    assert len(list(devices)) == 0


def test_merge_only_endpoint_gets_no_placeholder(ds, indigo_env):
    """No matterUnknown placeholder for an all-merge-only endpoint — but since
    issue #79, an electrical-only node (no actuator anywhere to attribute the
    reading to) gets the matterEnergyMeter fallback device, not nothing."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(ELECTRICAL_ONLY_NODE, "Meter")
    assert result["indigoDeviceIds"], "electrical-only node should get the matterEnergyMeter fallback"
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterEnergyMeter"
    assert not any(d.deviceTypeId == "matterUnknown" for d in devices)


def test_placeholder_obsolete_when_endpoint_becomes_supported(ds, indigo_env, mock_logger):
    """Firmware update adds a supported cluster: the real device is created and
    the user is told the placeholder can be deleted — never auto-deleted."""
    _indigo, devices = indigo_env
    ds.create_from_raw(UNKNOWN_NODE, "Vacuum")
    placeholder_id = ds.lookup(0x50, 1, "matterUnknown")

    upgraded = {
        "node_id": 0x50,
        "available": True,
        "attributes": dict(UNKNOWN_NODE["attributes"], **{"1/6/0": False}),
    }
    mock_logger.info.reset_mock()
    ds.reconcile_all([upgraded])

    assert ds.lookup(0x50, 1, "matterRelay") is not None
    assert ds.lookup(0x50, 1, "matterUnknown") == placeholder_id  # untouched
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert any("can be deleted" in m for m in info_msgs)


def test_created_devices_carry_created_type_id(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    assert dev.pluginProps["createdTypeId"] == "matterTemperatureSensor"


def test_reassert_stamps_created_type_id_on_legacy_devices(ds, indigo_env):
    """Devices created before the #58 guard gain the stamp at reconcile."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    props = dict(dev.pluginProps)
    props.pop("createdTypeId", None)
    dev.pluginProps = props

    ds.reconcile_all([TEMP_SENSOR_NODE])
    assert dev.pluginProps.get("createdTypeId") == "matterTemperatureSensor"


BUTTON_NODE = {
    "node_id": 0x43,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "Mini Switch",
        "1/29/0": [{"0": 15}],
        "1/59/1": 0,   # GenericSwitch CurrentPosition
    },
}


def test_stale_display_warning_covers_ui_display_fallback_types(ds, indigo_env, mock_logger):
    """A pre-fix button stuck on the on/off display gets the same nag as a
    value sensor — any type whose display_props disclaim SupportsOnState."""
    _indigo, devices = indigo_env
    ds.create_from_raw(BUTTON_NODE, "Hall Button")
    dev = devices[ds.lookup(0x43, 1)]
    _strip_display_props(dev)

    ds.reconcile_all([BUTTON_NODE])   # heals props; fake keeps display cached
    mock_logger.warning.reset_mock()
    ds.reconcile_all([BUTTON_NODE])

    msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.warning.call_args_list]
    assert any("delete the Indigo device" in m and "Hall Button" in m for m in msgs), msgs


def test_healthy_button_reconcile_is_quiet(ds, indigo_env, mock_logger):
    ds.create_from_raw(BUTTON_NODE, "Hall Button")
    mock_logger.warning.reset_mock()
    ds.reconcile_all([BUTTON_NODE])
    msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("display" in m for m in msgs)


def test_reassert_backfills_address_on_legacy_devices(ds, indigo_env):
    """Devices created before the issue #18 address stamping gain the node-id
    address at reconcile — it's what a user needs for decommission."""
    _indigo, devices = indigo_env
    ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    dev = devices[ds.lookup(0x40, 1)]
    props = dict(dev.pluginProps)
    props.pop("address", None)
    dev.pluginProps = props

    ds.reconcile_all([TEMP_SENSOR_NODE])
    assert dev.pluginProps.get("address") == "0x40"


def test_name_collision_logs_remedy_not_traceback(ds, indigo_env, mock_logger):
    """A NameNotUniqueError on create (issue #62: type-edited device invisible
    to iter('self') still holds the name server-side) must log the actionable
    remedy, not a raw traceback, and must not abort the batch."""
    indigo_mock, devices = indigo_env

    def explode(**kwargs):
        raise ValueError("NameNotUniqueError")
    indigo_mock.device.create = explode

    result = ds.create_from_raw(TEMP_SENSOR_NODE, "Landing Sensor")
    assert result.get("partial") is True
    msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.warning.call_args_list]
    assert any("delete that device and reload" in m for m in msgs), msgs
    assert not mock_logger.exception.called


# ===========================================================================
# issue #79 — split-endpoint energy measurement ("meter links")
#
# IKEA GRILLPLATS-shaped node: the relay lives on endpoint 1, ElectricalPower
# (0x0090) / ElectricalEnergy (0x0091) / PowerTopology (0x009C) live on a
# dedicated endpoint 2 (Matter 1.3 "Electrical Sensor" device type 0x0510).
# ===========================================================================

# NodeTopology (FeatureMap bit 0 — "measures the whole node", no endpoint
# list): the primary real-device path, resolved via the sole-actuator
# heuristic since there is nothing to read an endpoint list from.
GRILLPLATS_NODE = {
    "node_id": 0x34,
    "available": True,
    "attributes": {
        "0/40/1": "IKEA",
        "0/40/3": "GRILLPLATS",
        "1/29/0": [{"0": 266}],            # OnOffPlugInUnit
        "1/3/0": 0,                         # Identify
        "1/4/0": 0,                         # Groups
        "1/6/0": False,                     # OnOff
        "2/29/0": [{"0": 0x0510}],          # Descriptor: Electrical Sensor
        "2/144/8": 1200,                    # ElectricalPowerMeasurement.ActivePower = 1200 mW
        "2/145/1": {"energy": 3_600_000},   # ElectricalEnergyMeasurement.CumulativeEnergyImported
        "2/156/65532": 1,                   # PowerTopology.FeatureMap = NodeTopology (bit 0)
    },
}


def test_grillplats_split_energy_links_to_sole_relay(ds, indigo_env):
    """NodeTopology heuristic: ep2's readings attribute to ep1's relay — one
    device created (the relay, with meter props), nothing at all on ep2."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(GRILLPLATS_NODE, "Grill Plug")
    assert len(result["indigoDeviceIds"]) == 1
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    assert ds.lookup(0x34, 2) is None, "ep2 must get no device at all when the link succeeds"


def test_grillplats_priming_sets_relay_energy_states_from_linked_endpoint(ds, indigo_env):
    """Priming: node.attributes' ep2 electrical values reach the ep1 relay's
    states at creation — mW/mWh converted to W/kWh exactly like the co-located
    (Tapo) case."""
    _indigo, devices = indigo_env
    ds.create_from_raw(GRILLPLATS_NODE, "Grill Plug")
    relay = devices[ds.lookup(0x34, 1)]
    assert relay.states.get("curEnergyLevel") == 1.2       # 1200 mW → 1.2 W
    assert relay.states.get("accumEnergyTotal") == 3.6     # 3,600,000 mWh → 3.6 kWh


# Separate node/id for the live-routing test so the baseline reading is
# distinguishable from the value pushed by the live event below.
GRILLPLATS_LIVE_NODE = {
    "node_id": 0x3A,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 0x0510}],
        "2/144/8": 500,                     # baseline ActivePower = 500 mW
        "2/145/1": {"energy": 100_000},     # baseline CumulativeEnergyImported
        "2/156/65532": 1,                   # NodeTopology
    },
}


def test_grillplats_live_attribute_event_routes_to_linked_relay(ds, indigo_env):
    """Live routing: an attribute_updated event for ep2 (0x0090/0x0091) must
    update the ep1 relay's states via the same handle_event path production
    uses — not the (nonexistent) ep2 device."""
    _indigo, devices = indigo_env
    ds.create_from_raw(GRILLPLATS_LIVE_NODE, "Grill Plug")
    relay_id = ds.lookup(0x3A, 1)
    assert devices[relay_id].states.get("curEnergyLevel") == 0.5   # primed baseline

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x3A, endpoint=2, cluster=0x0090, attribute=0x0008, value=2500,
    ))
    assert devices[relay_id].states.get("curEnergyLevel") == 2.5   # 2500 mW → 2.5 W

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x3A, endpoint=2, cluster=0x0091, attribute=0x0001,
        value={"energy": 5_000_000},
    ))
    assert devices[relay_id].states.get("accumEnergyTotal") == 5.0  # 5,000,000 mWh → 5.0 kWh
    # No device was ever created on ep2 — the reading never had anywhere else to go.
    assert ds.lookup(0x3A, 2) is None


# SetTopology (FeatureMap bit 2): AvailableEndpoints lists the endpoint(s) the
# reading applies to explicitly — v1 keeps the link static after reconcile.
GRILLPLATS_SET_TOPOLOGY_NODE = {
    "node_id": 0x35,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 0x0510}],
        "2/144/8": 1200,
        "2/145/1": {"energy": 3_600_000},
        "2/156/65532": 4,     # FeatureMap = SetTopology (bit 2)
        "2/156/0": [1],       # AvailableEndpoints = [1]
    },
}


def test_grillplats_set_topology_links_to_available_endpoint(ds, indigo_env):
    _indigo, devices = indigo_env
    result = ds.create_from_raw(GRILLPLATS_SET_TOPOLOGY_NODE, "Grill Plug")
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    assert ds.lookup(0x35, 2) is None


# No 0x009C at all: the heuristic (sole actuator) still applies — a client
# doesn't have to expose Power Topology for the common single-actuator case.
GRILLPLATS_NO_TOPOLOGY_NODE = {
    "node_id": 0x36,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/144/8": 900,
        "2/145/1": {"energy": 500_000},
    },
}


def test_grillplats_no_power_topology_cluster_still_links_via_heuristic(ds, indigo_env):
    _indigo, devices = indigo_env
    result = ds.create_from_raw(GRILLPLATS_NO_TOPOLOGY_NODE, "Grill Plug")
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    assert ds.lookup(0x36, 2) is None


# Two actuator endpoints (ep1, ep3) + one electrical endpoint (ep2), no
# SetTopology list — attribution is ambiguous (a power strip without SET
# topology): never guess. ep2 falls back to the standalone matterEnergyMeter.
AMBIGUOUS_MULTI_RELAY_NODE = {
    "node_id": 0x37,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/144/8": 800,
        "2/145/1": {"energy": 200_000},
        "3/29/0": [{"0": 266}],
        "3/6/0": False,
    },
}


def test_ambiguous_multi_actuator_node_no_link_electrical_gets_fallback(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(AMBIGUOUS_MULTI_RELAY_NODE, "Power Strip")
    ep1_id = ds.lookup(0x37, 1)
    ep3_id = ds.lookup(0x37, 3)
    assert ep1_id is not None and ep3_id is not None
    assert not devices[ep1_id].pluginProps.get("SupportsPowerMeter")
    assert not devices[ep3_id].pluginProps.get("SupportsPowerMeter")
    ep2_id = ds.lookup(0x37, 2)
    assert ep2_id is not None, "attribution failure must fall back to a device, not vanish"
    assert devices[ep2_id].deviceTypeId == "matterEnergyMeter"


def test_electrical_only_node_fallback_device_states(ds, indigo_env):
    """Extends test_merge_only_endpoint_gets_no_placeholder: the fallback
    matterEnergyMeter's states are wired to what the electrical handlers
    actually write (curEnergyLevel/accumEnergyTotal — not the curPowerLevel
    typo an earlier draft of the plan flagged as a footgun), and — since the
    clusters live on the SAME endpoint as the fallback device — priming
    applies ELECTRICAL_ONLY_NODE's own readings (1200 mW / 1 mWh) at creation,
    same as any other device."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(ELECTRICAL_ONLY_NODE, "Meter")
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterEnergyMeter"
    assert dev.states.get("curEnergyLevel") == 1.2       # 1200 mW → 1.2 W, primed
    assert dev.states.get("accumEnergyTotal") == 0.0     # 1 mWh → 0.000001 kWh, rounds to 0.0
    assert dev.states.get("reachable") is True


def test_reconcile_self_heals_meter_props_via_link_without_recreation(ds, indigo_env):
    """A pre-existing relay (created before the split-endpoint fix landed, or
    before ep2's interview data arrived — mirrors the live jarvis relay
    1794293937 from the issue) gains SupportsPowerMeter/SupportsEnergyMeter
    through the linked ep2 without being deleted and recreated."""
    _indigo, devices = indigo_env
    bare_node = {
        "node_id": 0x34, "available": True,
        "attributes": {"1/29/0": [{"0": 266}], "1/6/0": False},
    }
    ds.create_from_raw(bare_node, "Grill Plug")
    dev_id = ds.lookup(0x34, 1)
    assert dev_id is not None
    assert not devices[dev_id].pluginProps.get("SupportsPowerMeter")
    assert not devices[dev_id].pluginProps.get("SupportsEnergyMeter")

    ds.reconcile_all([GRILLPLATS_NODE])

    assert devices[dev_id].pluginProps.get("SupportsPowerMeter") is True
    assert devices[dev_id].pluginProps.get("SupportsEnergyMeter") is True
    assert "curEnergyLevel" in devices[dev_id].states
    assert "accumEnergyTotal" in devices[dev_id].states
    assert ds.lookup(0x34, 1) == dev_id, "must be healed in place, not recreated"
    assert ds.lookup(0x34, 2) is None, "ep2 still gets no standalone device"


# A genuinely-unsupported cluster (RVC RunMode 0x0054, reused from UNKNOWN_NODE)
# co-occurring with the merge-only electrical clusters on the SAME endpoint.
UNSUPPORTED_PLUS_ELECTRICAL_NODE = {
    "node_id": 0x39,
    "available": True,
    "attributes": {
        "0/40/1": "ACME",
        "1/29/0": [{"0": 0x0510}],
        "1/84/0": 0,                      # RVC RunMode — genuinely unsupported
        "1/144/8": 500,                   # ElectricalPowerMeasurement (merge-only)
        "1/145/1": {"energy": 100},       # ElectricalEnergyMeasurement (merge-only)
    },
}


def test_unsupported_cluster_with_electrical_gets_placeholder_with_merge_annotation(ds, indigo_env):
    """An endpoint with a genuinely-unsupported cluster PLUS 0x90/0x91 must
    still surface the matterUnknown placeholder (issue #58's "no silent
    success" guarantee) — the electrical clusters are annotated as
    merge-only in the log/props, not silently absorbed into a meter link or
    matterEnergyMeter fallback (issue #79 point 3)."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(UNSUPPORTED_PLUS_ELECTRICAL_NODE, "Mystery Device")
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterUnknown"
    supported = dev.pluginProps["supportedClusters"]
    assert "0x0054" in supported
    assert "merge-only" in supported
    assert "0x0090" in supported and "0x0091" in supported


def test_tapo_colocated_energy_unchanged_no_meter_links(ds, indigo_env):
    """Regression: co-located energy (Tapo-style — OnOff + 0x90/0x91 on the
    SAME endpoint) must be untouched by issue #79 — props come from on_off.py's
    own same-endpoint check, and the meter-link maps stay empty throughout."""
    _indigo, devices = indigo_env
    ds.create_from_raw(ENERGY_PLUG_NODE, "Energy Plug")
    dev = devices[ds.lookup(0x20, 1)]
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    assert not ds._forward_links
    assert not ds._reverse_links


# ===========================================================================
# issue #80 review batch — PR #80 follow-up fixes/hardening on top of #79.
# ===========================================================================

# ep1: relay WITH its own co-located 0x0090/0x0091 (Tapo-style) — 500 mW /
# 250,000 mWh. ep2: an UNRELATED orphaned electrical-only endpoint with
# DIFFERENT readings — 900 mW / 700,000 mWh. ep2 must NOT link onto ep1 (that
# would overwrite ep1's own readings, both at priming and live routing); it
# must fall back to its own standalone matterEnergyMeter device instead.
COLOCATED_PLUS_ORPHAN_NODE = {
    "node_id": 0x53,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "1/144/8": 500,                    # ep1's OWN ActivePower
        "1/145/1": {"energy": 250_000},    # ep1's OWN CumulativeEnergyImported
        "2/144/8": 900,                    # ep2 orphan ActivePower (different!)
        "2/145/1": {"energy": 700_000},    # ep2 orphan CumulativeEnergyImported
    },
}


def test_colocated_target_excluded_from_meter_link_no_crosstalk(ds, indigo_env):
    """issue #80 review point A: an endpoint with its OWN co-located 0x90/0x91
    must never become a link TARGET for an unrelated orphan endpoint on the
    same node — that would let the orphan's readings silently overwrite the
    co-located device's own (both at priming and live routing)."""
    _indigo, devices = indigo_env
    ds.create_from_raw(COLOCATED_PLUS_ORPHAN_NODE, "Mixed Node")

    assert not ds._forward_links, "ep1 must never become a meter-link target here"
    assert not ds._reverse_links

    relay = devices[ds.lookup(0x53, 1)]
    assert relay.deviceTypeId == "matterRelay"
    assert relay.pluginProps.get("SupportsPowerMeter") is True
    assert relay.pluginProps.get("SupportsEnergyMeter") is True
    # ep1's OWN readings win, untouched by ep2's orphan values.
    assert relay.states.get("curEnergyLevel") == 0.5        # 500 mW → 0.5 W
    assert relay.states.get("accumEnergyTotal") == 0.25     # 250,000 mWh → 0.25 kWh

    ep2_dev_id = ds.lookup(0x53, 2)
    assert ep2_dev_id is not None, "orphan endpoint must fall back to its own device"
    ep2_dev = devices[ep2_dev_id]
    assert ep2_dev.deviceTypeId == "matterEnergyMeter"
    assert ep2_dev.states.get("curEnergyLevel") == 0.9      # 900 mW → 0.9 W (its own)
    assert ep2_dev.states.get("accumEnergyTotal") == 0.7    # 700,000 mWh → 0.7 kWh


# A node whose ep2 starts out with a genuinely-unsupported cluster (RVC
# RunMode) and later loses it in favour of split-endpoint electrical clusters
# — simulates the live jarvis scenario (device 1456954491): an existing
# matterUnknown placeholder that must become obsolete once ep2 reclassifies
# as a meter-linked SOURCE (which itself gets no device — specs stay empty).
GRILLPLATS_PRE_EP2_UNKNOWN_NODE = {
    "node_id": 0x54,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 0x0510}],
        "2/84/0": 0,   # RVC RunMode — genuinely unsupported, triggers a placeholder
    },
}

GRILLPLATS_EP2_HEALED_NODE = {
    "node_id": 0x54,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 0x0510}],
        "2/144/8": 1200,
        "2/145/1": {"energy": 3_600_000},
        "2/156/65532": 1,   # NodeTopology
    },
}


def test_obsolescence_log_fires_when_endpoint_becomes_meter_linked_source(ds, indigo_env, mock_logger):
    """issue #80 review point B: an endpoint reclassified as a meter-linked
    SOURCE (which itself gets no device — specs stay empty) must still
    surface the obsolescence log for its stale matterUnknown placeholder. The
    old `elif` only fired when the endpoint kept producing a real spec of its
    own, silently orphaning the placeholder in this case."""
    _indigo, devices = indigo_env
    ds.create_from_raw(GRILLPLATS_PRE_EP2_UNKNOWN_NODE, "Grill Plug")
    placeholder_id = ds.lookup(0x54, 2, "matterUnknown")
    assert placeholder_id is not None
    relay_id = ds.lookup(0x54, 1)
    assert not devices[relay_id].pluginProps.get("SupportsPowerMeter")

    mock_logger.info.reset_mock()
    ds.reconcile_all([GRILLPLATS_EP2_HEALED_NODE])

    # Relay healed via the existing reconcile self-heal path.
    assert devices[relay_id].pluginProps.get("SupportsPowerMeter") is True
    assert devices[relay_id].pluginProps.get("SupportsEnergyMeter") is True
    # ep2's stale placeholder is untouched (never auto-deleted) and no NEW
    # device was created on ep2 either (the link succeeded).
    assert ds.lookup(0x54, 2, "matterUnknown") == placeholder_id
    assert ds._all_dev_ids_for_endpoint(0x54, 2) == [placeholder_id]
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert any("can be deleted" in m for m in info_msgs)


# Same idea, but the node stays ambiguous (two actuators): ep2's electrical
# clusters can't be attributed, so a NEW matterEnergyMeter is created beside
# the stale matterUnknown placeholder — the log must connect the two.
AMBIGUOUS_PRE_EP2_UNKNOWN_NODE = {
    "node_id": 0x55,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}], "1/6/0": False,
        "2/29/0": [{"0": 1296}], "2/84/0": 0,   # RVC RunMode — placeholder trigger
        "3/29/0": [{"0": 266}], "3/6/0": False,
    },
}

AMBIGUOUS_EP2_BECOMES_ELECTRICAL_NODE = {
    "node_id": 0x55,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}], "1/6/0": False,
        "2/144/8": 800, "2/145/1": {"energy": 200_000},
        "3/29/0": [{"0": 266}], "3/6/0": False,
    },
}


def test_obsolescence_log_fires_alongside_new_meter_fallback_device(ds, indigo_env, mock_logger):
    """issue #80 review point B: the ambiguous-attribution case creates a NEW
    matterEnergyMeter beside a stale matterUnknown placeholder — the
    obsolescence log must fire so the two are connected, not silently orphaned."""
    _indigo, devices = indigo_env
    ds.create_from_raw(AMBIGUOUS_PRE_EP2_UNKNOWN_NODE, "Power Strip")
    placeholder_id = ds.lookup(0x55, 2, "matterUnknown")
    assert placeholder_id is not None

    mock_logger.info.reset_mock()
    ds.reconcile_all([AMBIGUOUS_EP2_BECOMES_ELECTRICAL_NODE])

    new_meter_id = ds.lookup(0x55, 2, "matterEnergyMeter")
    assert new_meter_id is not None
    assert new_meter_id != placeholder_id
    assert ds.lookup(0x55, 2, "matterUnknown") == placeholder_id  # untouched
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert any("can be deleted" in m for m in info_msgs)


# SetTopology bit set but AvailableEndpoints contains malformed elements
# (None, non-numeric string) — must degrade to the sole-actuator heuristic
# rather than raise (which would abort the whole node's device creation,
# since link resolution runs inside create_devices' lock-held body).
GRILLPLATS_MALFORMED_SET_TOPOLOGY_NODE = {
    "node_id": 0x38,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 0x0510}],
        "2/144/8": 1200,
        "2/145/1": {"energy": 3_600_000},
        "2/156/65532": 4,          # FeatureMap = SetTopology (bit 2)
        "2/156/0": [None, "x"],    # malformed AvailableEndpoints elements
    },
}


def test_set_topology_malformed_endpoint_list_falls_back_to_heuristic(ds, indigo_env):
    """issue #80 review point C: a malformed SetTopology endpoint-list
    element must not raise and abort node creation — it degrades to the
    sole-actuator heuristic, which still links since there's exactly one
    meter-capable endpoint."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(GRILLPLATS_MALFORMED_SET_TOPOLOGY_NODE, "Grill Plug")
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert ds.lookup(0x38, 2) is None


# A pump-shaped node: OnOff (recognized, relay created) + an extra cluster
# (0x0200) this plugin has no handler for at all — must be surfaced as a
# diagnostic, not silently dropped (issue #81), without creating a second device.
PUMP_MIXED_CLUSTER_NODE = {
    "node_id": 0x56,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "1/512/0": 0,   # 0x0200 — recognized by no registered handler
    },
}


def test_unmapped_cluster_alongside_real_device_logs_diagnostic(ds, indigo_env, mock_logger):
    """issue #81: a cluster this plugin doesn't recognize at all, co-occurring
    with a real handled cluster (a pump's OnOff + an extra cluster), must not
    be silently dropped once the endpoint's relay is created — an INFO log
    must name it, with no second device."""
    _indigo, devices = indigo_env
    ds.create_from_raw(PUMP_MIXED_CLUSTER_NODE, "Pump")
    dev = devices[ds.lookup(0x56, 1)]
    assert dev.deviceTypeId == "matterRelay"
    assert ds._all_dev_ids_for_endpoint(0x56, 1) == [dev.id], "no second device created"
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert any("0x0200" in m and "does not support yet" in m for m in info_msgs)


def test_pure_relay_endpoint_has_no_unmapped_cluster_log(ds, indigo_env, mock_logger):
    """Regression: a plain relay with nothing left over must NOT get the
    issue #81 diagnostic log."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    info_msgs = [c[0][0] for c in mock_logger.info.call_args_list]
    assert not any("does not support yet" in m for m in info_msgs)


# DynamicPowerFlow (bit 3) gates ActiveEndpoints (attr 0x0001) over
# AvailableEndpoints (attr 0x0000) — proven via a decoy endpoint (99, which
# doesn't exist on the node) in AvailableEndpoints, while ActiveEndpoints
# correctly lists the real actuator endpoint 1.
GRILLPLATS_DYNAMIC_POWER_FLOW_DECOY_NODE = {
    "node_id": 0x57,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 0x0510}],
        "2/144/8": 1200,
        "2/145/1": {"energy": 3_600_000},
        "2/156/65532": 12,   # FeatureMap = SetTopology(bit2) | DynamicPowerFlow(bit3)
        "2/156/0": [99],     # AvailableEndpoints — decoy, must be ignored
        "2/156/1": [1],      # ActiveEndpoints — the real list when DYPF is set
    },
}


def test_dynamic_power_flow_prefers_active_endpoints_over_available_decoy(ds, indigo_env):
    """issue #80 review point G1: with DynamicPowerFlow set, ActiveEndpoints
    (not AvailableEndpoints) must be consulted."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(GRILLPLATS_DYNAMIC_POWER_FLOW_DECOY_NODE, "Grill Plug")
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert ds.lookup(0x57, 2) is None


def test_electrical_attribute_event_before_any_create_is_silent_noop(ds, indigo_env):
    """issue #80 review point G2: an electrical attribute event for a node
    that has never been created/reconciled must not raise and must create
    nothing — the reading is silently dropped (there's nowhere for it to go
    yet)."""
    _indigo, devices = indigo_env
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x62, endpoint=2, cluster=0x0090, attribute=0x0008, value=1000,
    ))  # must not raise
    assert len(list(devices)) == 0


# ===========================================================================
# issue #82 — bridge battery cross-contamination
# ===========================================================================

BRIDGE_MULTI_BATTERY_NODE = {
    "node_id": 0x61,
    "available": True,
    "attributes": {
        "0/40/1": "ACME",
        "0/40/3": "Multi Sensor Bridge",
        # Child 1: temperature sensor + its OWN PowerSource (battery = 80%)
        "1/29/0": [{"0": 770}],
        "1/1026/0": 2100,
        "1/47/12": 160,             # BatPercentRemaining = 160/2 = 80%
        # Child 2: humidity sensor + its OWN PowerSource (battery = 40%, distinct)
        "2/29/0": [{"0": 775}],
        "2/1029/0": 5500,
        "2/47/12": 80,              # BatPercentRemaining = 80/2 = 40%
        # Child 3: mains relay — no PowerSource at all
        "3/29/0": [{"0": 266}],
        "3/6/0": False,
    },
}


def test_bridge_multi_power_source_battery_isolated_per_endpoint(ds, indigo_env):
    """issue #82: a bridge with MORE THAN ONE PowerSource-bearing endpoint
    must not fan battery updates node-wide — each child gets ONLY its own
    battery level, and a mains child (no PowerSource) gets no
    SupportsBatteryLevel at all."""
    _indigo, devices = indigo_env
    ds.create_from_raw(BRIDGE_MULTI_BATTERY_NODE, "Multi Sensor Bridge")
    temp_id = ds.lookup(0x61, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x61, 2, "matterHumiditySensor")
    relay_id = ds.lookup(0x61, 3, "matterRelay")
    assert temp_id and hum_id and relay_id

    assert devices[temp_id].pluginProps.get("SupportsBatteryLevel") is True
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True
    assert not devices[relay_id].pluginProps.get("SupportsBatteryLevel")

    assert devices[temp_id].states.get("batteryLevel") == 80
    assert devices[hum_id].states.get("batteryLevel") == 40
    assert "batteryLevel" not in devices[relay_id].states

    # Live routing: an update on ep1's PowerSource must not leak to ep2/ep3.
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x61, endpoint=1, cluster=0x002F, attribute=0x000C, value=200,
    ))
    assert devices[temp_id].states.get("batteryLevel") == 100
    assert devices[hum_id].states.get("batteryLevel") == 40   # untouched, no leak
