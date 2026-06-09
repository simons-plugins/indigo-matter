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
