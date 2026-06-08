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

    def updateStatesOnServer(self, kvlist):
        for kv in kvlist:
            self.states[kv["key"]] = kv["value"]

    def setErrorStateOnServer(self, value):
        self.error = value


class FakeDevices:
    def __init__(self):
        self._by_id = {}
        self._counter = 1000

    def next_id(self):
        self._counter += 1
        return self._counter

    def add(self, dev):
        self._by_id[dev.id] = dev

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

    def create(self, protocol=None, deviceTypeId="", name="", props=None, folder="", **kwargs):
        dev = FakeDev(self.devices.next_id(), name, deviceTypeId, dict(props or {}))
        self.devices.add(dev)
        self.created.append(dev)
        return dev


@pytest.fixture
def indigo_env(mock_indigo_base):
    indigo = mock_indigo_base
    devices = FakeDevices()
    indigo.devices = devices
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
