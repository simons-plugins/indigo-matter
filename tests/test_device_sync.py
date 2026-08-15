"""M4: reconciliation + the asyncio→Indigo write/command seams.

Uses a small fake Indigo (devices collection + device.create) so the real
state-update and device-creation paths run without a live Indigo server.
"""
from __future__ import annotations

import copy
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

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

# Devices.xml declares these as plain custom states (no Supports* prop gates
# them) — real Indigo instantiates a device's declared <States> at CREATION
# regardless of props, unlike the Supports*-driven built-ins above. Only
# BooleanStateConfigHandler's guard (issue #85 — "sensitivityLevel" not in
# indigo_dev.states) needs this modelled; matterLock's lockState needs no
# guard (door_lock.py writes it unconditionally) so it needs no seeding here.
_STATIC_DEVICE_TYPE_STATES = {
    # Mirrors the <States> each type declares in Devices.xml — real Indigo
    # creates a device with every declared state present, and the handlers'
    # "is this state on this device?" guards depend on that being true.
    "matterMotionSensor": {"sensitivityLevel", "holdTime"},
    "matterContactSensor": {"sensitivityLevel"},
    "matterRelay": {"startUpOnOff"},
    # issue #204 / ADR-0008 — declared unconditionally in Devices.xml (no
    # Supports* prop gates any of them), same custom-type discipline as
    # matterEnergyMeter/matterUnknown's `reachable`. curEnergyLevel/
    # accumEnergyTotal joined the list once ep-0 energy attribution shipped
    # (issue #204's final stage) — same ids matterEnergyMeter already
    # declares, also unconditionally.
    "matterNode": {"nodeLabel", "softwareVersion", "batteryLevel", "reachable",
                   "curEnergyLevel", "accumEnergyTotal"},
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
        for state_key in _STATIC_DEVICE_TYPE_STATES.get(device_type_id, ()):
            self.states.setdefault(state_key, 0)
        self.error = None
        self.errorState = ""
        self.folderId = 0
        # See replaceOnServer: fail_replace models real Indigo's discard-on-
        # failure semantics; _name_on_server is the last name Indigo accepted.
        self.fail_replace = False
        self._name_on_server = name
        # Issue #190: how many times Indigo was asked to rebuild this device's
        # state list. The question the tests care about is WHEN that is asked
        # for, not what Indigo does next, so the fake only counts.
        self.state_list_rebuilds = 0
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
            # Real Indigo's states dict answers a "key.ui" lookup with the
            # display value from a uiValue-bearing write (used by both fix A's
            # evidence check and fix D's reachable/unreachable column text).
            if "uiValue" in kv:
                self.states[f"{kv['key']}.ui"] = kv["uiValue"]

    def stateListOrDisplayStateIdChanged(self):
        self.state_list_rebuilds += 1

    def setErrorStateOnServer(self, value):
        self.error = value
        self.errorState = value

    def replaceOnServer(self):
        # Real Indigo persists the in-memory edits (name etc.). Production
        # writes dev.name BEFORE calling this, and real Indigo discards the
        # in-memory edit on failure — so a failing fake must roll the name
        # back, or later passes vote on a name Indigo never had (issue #204
        # verification round). Set fail_replace=True to model failure; do NOT
        # override this method with a bare raiser.
        if self.fail_replace:
            self.name = self._name_on_server
            raise ValueError("replaceOnServer refused (fail_replace)")
        self.replaced = True
        self._name_on_server = self.name

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
    """Stands in for the ``indigo.device`` command namespace.

    Since issue #204 stage 2 this carries a REAL device-group model rather than
    recording calls, and since ADR-0009 that model is the one the CONTROLLED
    EXPERIMENT on jarvis (2026-08-12) established rather than the one the docs
    imply:

    * Indigo orders a group's members by device AGE (creation order), and the
      OLDEST member is the root — ``getGroupList``'s first element, identical
      whichever member is asked.
    * ``groupWithDevice(a, b)`` and ``groupWithDevice(b, a)`` produce the
      SAME group. **The argument order does nothing.** A fake that honoured
      arg order would let a plugin that (wrongly) depends on it pass.
    * ``indigo.device.delete`` REFUSES to delete the root of a non-empty
      group — whichever device that turns out to be, which is what makes
      ``delete_node``'s dissolve-first shape load-bearing rather than
      decorative.
    """

    def __init__(self, devices):
        self.devices = devices
        self.created = []
        #: dev_id → the group's member list, SHARED by every member, ordered
        #: OLDEST FIRST; [0] is therefore the root. Absent means ungrouped.
        self.groups = {}
        #: (dev_1, dev_2) per groupWithDevice call — the idempotence assertion
        #: is "a second reconcile pass adds none of these". The order inside
        #: the tuple is what the plugin passed and means nothing to Indigo.
        self.group_calls = []
        self.ungroup_calls = []

    @staticmethod
    def _id_of(dev_or_id):
        return dev_or_id.id if hasattr(dev_or_id, "id") else int(dev_or_id)

    def _age_of(self, dev_id):
        """Creation sequence of a device — lower is older.

        ``FakeDevices.next_id`` is a monotonic counter, so an id IS its
        creation rank for every device these tests make; devices built by hand
        with an explicit id (the orphan/ghost fixtures) sort by that id, which
        is all the ordering they need.
        """
        return dev_id

    def create(self, protocol=None, deviceTypeId="", name="", props=None, folder=0, **kwargs):
        dev = FakeDev(self.devices.next_id(), name, deviceTypeId, dict(props or {}))
        if isinstance(folder, int) and folder:
            dev.folderId = folder
        self.devices.add(dev)
        self.created.append(dev)
        return dev

    def delete(self, dev):
        dev_id = self._id_of(dev)
        members = self.groups.get(dev_id)
        if members and len(members) > 1 and members[0] == dev_id:
            raise ValueError(
                "cannot delete device %s: it is the root of a non-empty device group" % dev_id)
        self._drop_from_group(dev_id)
        self.devices._by_id.pop(dev_id, None)

    def moveToFolder(self, dev_or_id, value=None):
        dev = dev_or_id if hasattr(dev_or_id, "folderId") else self.devices[dev_or_id]
        dev.folderId = value

    # -- the device-group model -----------------------------------------
    def getGroupList(self, dev_or_id):
        dev_id = self._id_of(dev_or_id)
        if dev_id not in self.devices._by_id:
            # Real Indigo cannot answer for an id that is not a device, and the
            # plugin's index CAN carry one: plugin.deviceDeleted only prunes
            # matterNode ids, so a hand-deleted endpoint device leaves a dead
            # id behind (issue #204 review, fix D). Without this tooth the
            # grouping sweep looks harmless against a fake that answers anyway.
            raise ValueError("device %s does not exist" % dev_id)
        members = self.groups.get(dev_id)
        # An ungrouped device answers with just itself — the plugin tolerates an
        # empty list too (both shapes are undocumented; see _ensure_grouped).
        return list(members) if members else [dev_id]

    def groupWithDevice(self, dev_1, dev_2):
        """The experiment's semantics: the two devices' groups are UNIONED and
        the result is ordered by device age, oldest first.

        ``groupWithDevice(motion, node)`` and ``groupWithDevice(node, motion)``
        returned byte-identical member lists on the live rig, and adding a
        third device to an existing pair left the root untouched — so this
        models a symmetric union with an age sort and no notion of a joiner.

        What is and isn't experiment-backed, honestly: SINGLETON+SINGLETON
        (both argument orders) and SINGLETON-JOINS-EXISTING-GROUP (the
        experiment's third line) are what the live rig actually exercised.
        A general GROUP+GROUP union — two already-multi-member groups
        merged in one call — is EXTRAPOLATED from those, never run on
        jarvis. Production's own guard (`_ensure_grouped`'s family check
        only ever passes a SINGLETON node device as one side) means only
        the backed directions are exercised today; this fake unions
        unconditionally because nothing here currently calls it any other
        way. Anyone relaxing that guard to call this with two genuine
        multi-member groups must extend the live experiment first, not
        just trust this model to still be right.
        """
        first, second = self._id_of(dev_1), self._id_of(dev_2)
        self.group_calls.append((first, second))
        members = set(self.groups.get(first) or [first])
        members |= set(self.groups.get(second) or [second])
        ordered = sorted(members, key=self._age_of)
        for member in ordered:
            self.groups[member] = ordered

    def ungroupDevice(self, dev_or_id):
        dev_id = self._id_of(dev_or_id)
        self.ungroup_calls.append(dev_id)
        self._drop_from_group(dev_id)

    def _drop_from_group(self, dev_id):
        members = self.groups.pop(dev_id, None)
        if not members:
            return
        remaining = [member for member in members if member != dev_id]
        for member in remaining:
            # A group of one is no group at all.
            if len(remaining) > 1:
                self.groups[member] = remaining
            else:
                self.groups.pop(member, None)


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
    # Since #204 stage 2 every endpoint device carries its role suffix — the
    # bare base is the node device's name now (ADR-0008 option B).
    assert dev.name == "Office Plug - Switch"
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
    assert dev.name == "Office Plug - Switch"
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
    assert dev.name == "Hallway Lamp - Switch"
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
    assert dev.name == "Office Plug - Switch"   # still created
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
    assert names == ["Office Plug - Switch"]


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
    assert nodes[0][1] == ["Lamp Plug - Switch"]
    assert nodes[1][1] == ["Office Plug - Switch"]


# ---------------------------------------------------------------------------
# A node that produced NO devices must still be listed (issue #105 follow-on):
# the decommission picker reads list_nodes(), so an empty bridge that isn't
# listed can never be decommissioned — the one node most in need of it.
# ---------------------------------------------------------------------------

# The real shape from jarvis: a Homebridge child bridge publishing a bare
# Aggregator (0x000E) on ep1 with descriptor only and an empty PartsList.
EMPTY_BRIDGE_NODE = {
    "node_id": 53,                      # 0x35
    "available": True,
    "attributes": {
        "0/40/1": "Homebridge",
        "0/40/3": "Homebridge iCloud",
        "1/29/0": [{"0": 14}],          # Descriptor -> DeviceType 0x000E Aggregator
    },
}


def test_list_nodes_includes_a_node_that_created_no_devices(ds, indigo_env):
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    nodes = ds.list_nodes()
    assert nodes == [(53, [])]          # listed, with no device names


def test_list_nodes_includes_empty_bridge_alongside_real_nodes(ds, indigo_env):
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    nodes = dict(ds.list_nodes())
    assert nodes[42] == ["Office Plug - Switch"]
    assert nodes[53] == []              # the empty bridge is selectable too


def test_reconcile_lists_an_empty_bridge_it_has_seen(ds, indigo_env):
    # The path that actually matters: a bridge picked up by reconcile (not by a
    # user-driven commission) must reach the picker.
    ds.reconcile_all([EMPTY_BRIDGE_NODE])
    assert ds.list_nodes() == [(53, [])]


def test_decommission_removes_an_empty_bridge_from_the_picker_immediately(ds, indigo_env):
    """Regression: a decommissioned node must not linger in the picker.

    Pruning _index alone used to be enough — a node with no index entries simply
    vanished from list_nodes(). Once list_nodes() also drew on _known_nodes, a
    node with no devices survived its own decommission and could be selected a
    second time, which is exactly what happened on jarvis with 0x35.
    """
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    assert ds.list_nodes() == [(53, [])]
    ds.delete_node(53)                    # what decommission calls
    assert ds.list_nodes() == []          # gone at once, without waiting for a reconcile


def test_failed_fabric_removal_keeps_an_empty_bridge_listed_for_retry(ds, indigo_env):
    """forget=False must leave the node offered — otherwise the retry is impossible.

    An empty bridge has no _index entries, so _known_nodes is the ONLY thing
    keeping it in the picker. Forgetting it when the fabric removal failed left
    a still-commissioned node unreachable from the UI (issue #111 review).
    """
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    ds.delete_node(53, forget=False)          # fabric removal failed
    assert ds.list_nodes() == [(53, [])]      # still offered, still retryable


def test_knows_node_covers_device_less_and_device_bearing_nodes(ds, indigo_env):
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert ds.knows_node(53) is True          # tracked only via _known_nodes
    assert ds.knows_node(42) is True
    assert ds.knows_node(999) is False        # genuinely unknown -> 404 path


def test_node_count_includes_a_node_with_no_devices(ds, indigo_env):
    # node_count feeds /status's nodeCount; deriving it from _index alone
    # under-reported a commissioned empty bridge relative to the picker.
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    assert ds.node_count() == 2
    assert len(ds.list_nodes()) == ds.node_count()


def test_unparseable_node_is_still_offered_for_decommission(ds, indigo_env, mock_logger):
    """Parse failure is a property of the node's SHAPE, so it recurs every pass.

    Dropping it would make a malformed node permanently undecommissionable —
    and a malformed node is a prime candidate for removal. The id is all
    _decommission needs.
    """
    bad = {"node_id": 53, "attributes": {"1/29/0": [{"0": "not-an-int"}]}}
    ds.reconcile_all([bad])
    assert ds.list_nodes() == [(53, [])]
    assert ds.knows_node(53) is True
    warning = " ".join(str(c) for c in ds.logger.warning.call_args_list)
    assert "0x35" in warning                  # the id survives into the log


def test_unparseable_node_with_no_usable_id_is_skipped_quietly(ds, indigo_env):
    ds.reconcile_all([{"attributes": {"1/29/0": [{"0": "nope"}]}}])
    assert ds.list_nodes() == []
    warning = " ".join(str(c) for c in ds.logger.warning.call_args_list)
    assert "id unreadable" in warning


def test_reconcile_warns_when_a_known_node_stops_being_reported(ds, indigo_env):
    # A device-less node leaves NO other trace when it vanishes (no device to
    # mark unreachable), so a transient short get_nodes() would silently retract
    # it from the picker.
    ds.reconcile_all([EMPTY_BRIDGE_NODE])
    ds.logger.warning.reset_mock()
    ds.reconcile_all([])
    warning = " ".join(str(c) for c in ds.logger.warning.call_args_list)
    assert "0x35" in warning and "no longer reported" in warning


def test_decommission_removes_a_device_bearing_node_from_the_picker(ds, indigo_env):
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert [n for n, _ in ds.list_nodes()] == [42]
    ds.delete_node(42)
    assert ds.list_nodes() == []


def test_decommission_leaves_other_nodes_in_the_picker(ds, indigo_env):
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    ds.create_from_raw(EMPTY_BRIDGE_NODE, "")
    ds.delete_node(53)                    # remove only the empty bridge
    assert [n for n, _ in ds.list_nodes()] == [42]


def test_reconcile_drops_a_node_that_is_no_longer_commissioned(ds, indigo_env):
    # raw_nodes is authoritative: a node gone from it must stop being offered,
    # otherwise the picker invites a decommission that would 404.
    ds.reconcile_all([EMPTY_BRIDGE_NODE])
    assert ds.list_nodes() == [(53, [])]
    ds.reconcile_all([])
    assert ds.list_nodes() == []


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


# ===========================================================================
# issue #205 — PowerSource.EndpointList (0x001F, core §11.7.7.32) is the
# authority for what a battery powers; the #82 heuristic is now only the
# fallback for rev-1 firmware that predates the attribute.
#
# "0/47/31" = endpoint 0, cluster 0x002F (PowerSource), attr 0x001F (EndpointList).
# ===========================================================================

#: The FP300 shape stated explicitly: one source on ep0 whose EMPTY list means
#: "the entire node". The heuristic already reached this answer — here it is
#: reached from evidence.
POWER_SOURCE_NODE_WIDE_NODE = {
    "node_id": 0x63,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "0/47/12": 160,             # BatPercentRemaining = 160/2 = 80%
        "0/47/31": [],              # EndpointList: empty = the whole node
        "1/29/0": [{"0": 770}],     # TemperatureSensor
        "1/1026/0": 2100,
        "2/29/0": [{"0": 775}],     # HumiditySensor
        "2/1029/0": 5500,
    },
}

#: THE new-behaviour proof, and the case the old heuristic gets WRONG: a single
#: PowerSource endpoint (so the heuristic says "node-wide") that names exactly
#: one application endpoint. ep1 is battery-powered, ep2 is not — a
#: mains-powered child sharing a node with a battery one.
POWER_SOURCE_CONFINED_NODE = {
    "node_id": 0x64,
    "available": True,
    "attributes": {
        "0/40/1": "ACME",
        "0/40/3": "Half Battery Hub",
        "0/47/12": 120,             # BatPercentRemaining = 120/2 = 60%
        "0/47/31": [1],             # EndpointList: ep1 only (ep0 added defensively)
        "1/29/0": [{"0": 770}],     # TemperatureSensor — powered by the source
        "1/1026/0": 2100,
        "2/29/0": [{"0": 775}],     # HumiditySensor — NOT powered by it
        "2/1029/0": 5500,
    },
}

#: The issue #82 bridge, rev-2: each child's own source states its own endpoint.
#: Same outcome as BRIDGE_MULTI_BATTERY_NODE's heuristic — from evidence now.
BRIDGE_ENDPOINT_LIST_NODE = {
    "node_id": 0x65,
    "available": True,
    "attributes": {
        "0/40/1": "ACME",
        "0/40/3": "Multi Sensor Bridge",
        "1/29/0": [{"0": 770}],
        "1/1026/0": 2100,
        "1/47/12": 160,             # 80%
        "1/47/31": [1],
        "2/29/0": [{"0": 775}],
        "2/1029/0": 5500,
        "2/47/12": 80,              # 40%
        "2/47/31": [2],
        "3/29/0": [{"0": 266}],     # mains relay — no PowerSource at all
        "3/6/0": False,
    },
}


def test_empty_endpoint_list_gives_every_device_on_the_node_battery(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_NODE_WIDE_NODE, "Landing Presence")
    temp_id = ds.lookup(0x63, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x63, 2, "matterHumiditySensor")
    assert temp_id and hum_id

    assert devices[temp_id].pluginProps.get("SupportsBatteryLevel") is True
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True
    assert devices[temp_id].states.get("batteryLevel") == 80
    assert devices[hum_id].states.get("batteryLevel") == 80


def test_a_named_endpoint_list_confines_battery_to_the_endpoints_it_names(ds, indigo_env):
    """The behaviour change #205 buys: ONE PowerSource endpoint, so the old
    heuristic fanned out node-wide and gave the mains-powered child a battery
    level it does not have. Its own EndpointList says otherwise."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_CONFINED_NODE, "Half Battery Hub")
    temp_id = ds.lookup(0x64, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x64, 2, "matterHumiditySensor")
    assert temp_id and hum_id

    assert devices[temp_id].pluginProps.get("SupportsBatteryLevel") is True
    assert devices[temp_id].states.get("batteryLevel") == 60
    assert not devices[hum_id].pluginProps.get("SupportsBatteryLevel")
    assert "batteryLevel" not in devices[hum_id].states


def test_a_named_endpoint_list_confines_the_LIVE_battery_update_too(ds, indigo_env):
    """Routing, not just the handler's missing-state guard: the uncovered
    device is given a batteryLevel state by hand, so if the event reached it at
    all the value would land."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_CONFINED_NODE, "Half Battery Hub")
    temp_id = ds.lookup(0x64, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x64, 2, "matterHumiditySensor")
    devices[hum_id].states["batteryLevel"] = 0

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x64, endpoint=0, cluster=0x002F, attribute=0x000C, value=200,
    ))
    assert devices[temp_id].states.get("batteryLevel") == 100
    assert devices[hum_id].states.get("batteryLevel") == 0, "ep2 is not powered by this source"


def test_capability_self_heal_only_adds_battery_where_the_source_says(ds, indigo_env):
    """_capability_props reads the same coverage the creation prop did, so the
    issue #45 reconcile self-heal cannot re-introduce the prop on an endpoint
    the EndpointList excludes."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_CONFINED_NODE, "Half Battery Hub")
    temp_id = ds.lookup(0x64, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x64, 2, "matterHumiditySensor")
    # Simulate the mid-interview creation #45 exists for: the covered device
    # lost the prop it should have.
    props = dict(devices[temp_id].pluginProps)
    props.pop("SupportsBatteryLevel", None)
    devices[temp_id].pluginProps = props

    ds.reconcile_all([POWER_SOURCE_CONFINED_NODE])

    assert devices[temp_id].pluginProps.get("SupportsBatteryLevel") is True
    assert not devices[hum_id].pluginProps.get("SupportsBatteryLevel")


def test_bridge_with_per_child_endpoint_lists_is_isolated_from_evidence(ds, indigo_env):
    """The issue #82 outcome, now reached because each child SAID so rather
    than because the plugin counted PowerSource endpoints."""
    _indigo, devices = indigo_env
    ds.create_from_raw(BRIDGE_ENDPOINT_LIST_NODE, "Multi Sensor Bridge")
    temp_id = ds.lookup(0x65, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x65, 2, "matterHumiditySensor")
    relay_id = ds.lookup(0x65, 3, "matterRelay")
    assert temp_id and hum_id and relay_id

    assert devices[temp_id].states.get("batteryLevel") == 80
    assert devices[hum_id].states.get("batteryLevel") == 40
    assert not devices[relay_id].pluginProps.get("SupportsBatteryLevel")

    # Live routing: ep1's source powers ep1 and nothing else.
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x65, endpoint=1, cluster=0x002F, attribute=0x000C, value=200,
    ))
    assert devices[temp_id].states.get("batteryLevel") == 100
    assert devices[hum_id].states.get("batteryLevel") == 40   # untouched, no leak


def test_an_unknown_source_in_a_known_multi_source_map_does_not_fan_out(ds, indigo_env):
    """The #82 posture for a source ``_battery_targets`` has never heard of.

    BRIDGE_ENDPOINT_LIST_NODE has TWO PowerSource endpoints (ep1, ep2), each
    naming only its own endpoint via EndpointList — so the cached
    ``by_source`` map (device_sync.py) has more than one entry. A live event
    from a source NOT in that map (endpoint 0 — no PowerSource lives there on
    this fixture) exercises ``_battery_targets``' per-source #82 fallback,
    `return frozenset({int(source_ep)}) if len(by_source) > 1 else None` —
    confining an unattributed reading to its OWN (deviceless) endpoint rather
    than fanning it across the bridge's children. The observable outcome is
    that no existing device changes.
    """
    _indigo, devices = indigo_env
    ds.create_from_raw(BRIDGE_ENDPOINT_LIST_NODE, "Multi Sensor Bridge")
    temp_id = ds.lookup(0x65, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x65, 2, "matterHumiditySensor")
    relay_id = ds.lookup(0x65, 3, "matterRelay")
    assert temp_id and hum_id and relay_id

    # Seed a batteryLevel state on the relay too, so the assertion below
    # proves routing confinement rather than the handler's missing-state
    # guard (a device with no such state looks "unchanged" either way).
    devices[relay_id].states["batteryLevel"] = 0
    temp_before = devices[temp_id].states.get("batteryLevel")
    hum_before = devices[hum_id].states.get("batteryLevel")
    relay_before = devices[relay_id].states.get("batteryLevel")

    # A PowerSource event from endpoint 0 — not a key in the cached map.
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x65, endpoint=0, cluster=0x002F, attribute=0x000C, value=200,
    ))

    assert devices[temp_id].states.get("batteryLevel") == temp_before
    assert devices[hum_id].states.get("batteryLevel") == hum_before
    assert devices[relay_id].states.get("batteryLevel") == relay_before


def test_reconcile_self_heal_confines_battery_prop_to_each_fallback_source(ds, indigo_env):
    """The reconcile self-heal (issue #45) must use the SAME per-source #82
    confinement the create pass does when there is no EndpointList evidence —
    not re-fan SupportsBatteryLevel across the whole node.

    BRIDGE_MULTI_BATTERY_NODE deliberately carries no EndpointList (0x001F):
    its two PowerSource-bearing children (ep1, ep2) fall back to the pre-#205
    heuristic, which confines each source to its own endpoint. A relay on ep3
    has no PowerSource at all. Mirrors
    test_reconcile_reasserts_battery_prop_from_power_source_on_other_endpoint's
    shape, but for the multi-source fallback rather than the single-source
    node-wide case.
    """
    _indigo, devices = indigo_env
    ds.create_from_raw(BRIDGE_MULTI_BATTERY_NODE, "Multi Sensor Bridge")
    temp_id = ds.lookup(0x61, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x61, 2, "matterHumiditySensor")
    relay_id = ds.lookup(0x61, 3, "matterRelay")
    assert temp_id and hum_id and relay_id

    # Simulate a mid-interview creation: both battery children lost the prop.
    for dev_id in (temp_id, hum_id):
        props = dict(devices[dev_id].pluginProps)
        props.pop("SupportsBatteryLevel", None)
        devices[dev_id].pluginProps = props

    ds.reconcile_all([BRIDGE_MULTI_BATTERY_NODE])

    assert devices[temp_id].pluginProps.get("SupportsBatteryLevel") is True
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True
    # The relay must NOT gain the prop — a node-wide re-fan would give it one.
    assert not devices[relay_id].pluginProps.get("SupportsBatteryLevel")


def test_a_battery_event_with_no_cached_coverage_still_fans_out_node_wide(ds, indigo_env):
    """No cached coverage is "no authority", NOT "powers nothing" — the reading
    keeps the pre-#205 node-wide default rather than being dropped. Asserted
    against a node whose devices exist but whose coverage never got cached (a
    non-informative snapshot), because a dropped battery reading looks like
    broken hardware and a stray one self-corrects on the next pass."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_NODE_WIDE_NODE, "Landing Presence")
    hum_id = ds.lookup(0x63, 2, "matterHumiditySensor")
    ds._power_coverage.clear()

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x63, endpoint=0, cluster=0x002F, attribute=0x000C, value=100,
    ))
    assert devices[hum_id].states.get("batteryLevel") == 50


def test_an_unusable_endpoint_list_falls_back_and_says_so(ds, indigo_env, mock_logger):
    """Degrade, don't raise: a non-conformant list must not abort the node's
    device creation, and the fallback it took is diagnosable from the log."""
    _indigo, devices = indigo_env
    import copy
    broken = copy.deepcopy(POWER_SOURCE_CONFINED_NODE)
    broken["attributes"]["0/47/31"] = ["ep1"]
    ds.create_from_raw(broken, "Half Battery Hub")
    # Single source + unusable list ⇒ the heuristic's node-wide answer.
    assert devices[ds.lookup(0x64, 1, "matterTemperatureSensor")].states.get("batteryLevel") == 60
    assert devices[ds.lookup(0x64, 2, "matterHumiditySensor")].states.get("batteryLevel") == 60
    debug_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.debug.call_args_list]
    assert any("EndpointList" in m for m in debug_msgs)


#: Shared substring for the exclusion INFO — kept in one place so the wording
#: in device_sync.py and the assertions below cannot silently drift apart.
_EXCLUSION_PHRASE = "no power source on this node covers this endpoint"


def test_newly_excluded_battery_device_gets_one_time_info(ds, indigo_env, mock_logger):
    """Issue #205 upgrade gap: an endpoint covered under the pre-#205 fallback
    (single source, no EndpointList ⇒ node-wide) that a LATER, more specific
    EndpointList excludes must not silently freeze forever — SupportsBatteryLevel
    and batteryLevel are add-only, so nothing withdraws them, but the device
    gets exactly ONE INFO naming it so the staleness is discoverable."""
    _indigo, devices = indigo_env
    import copy
    fallback_node = {
        "node_id": 0x70, "available": True,
        "attributes": {
            "0/47/12": 160,             # BatPercentRemaining = 80%
            "1/29/0": [{"0": 770}],
            "1/1026/0": 2100,
            "2/29/0": [{"0": 775}],
            "2/1029/0": 5500,
        },
    }
    ds.create_from_raw(fallback_node, "Fallback Bridge")
    hum_id = ds.lookup(0x70, 2, "matterHumiditySensor")
    assert hum_id is not None
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True
    # Hand-set a distinctive reading so it is unambiguous below that this
    # exact value is what got frozen, independent of whatever priming wrote.
    devices[hum_id].states["batteryLevel"] = 33

    # A later snapshot ADDS a naming EndpointList that excludes ep2.
    named_node = copy.deepcopy(fallback_node)
    named_node["attributes"]["0/47/31"] = [1]   # names ep1 only; ep2 drops out

    mock_logger.info.reset_mock()
    ds.create_from_raw(named_node, "Fallback Bridge")
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True   # add-only: still set
    assert devices[hum_id].states.get("batteryLevel") == 33                  # frozen, not withdrawn
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    exclusion_msgs = [m for m in info_msgs if _EXCLUSION_PHRASE in m]
    assert len(exclusion_msgs) == 1
    # Names the RIGHT device, not just any excluded one — a bare count==1
    # would pass even if the message pointed at the wrong id.
    assert f"device {hum_id} " in exclusion_msgs[0]

    # A second pass over the SAME excluding snapshot must not repeat the log.
    mock_logger.info.reset_mock()
    ds.create_from_raw(named_node, "Fallback Bridge")
    info_msgs2 = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert not [m for m in info_msgs2 if _EXCLUSION_PHRASE in m]


def test_non_informative_pass_never_warns_of_exclusion(ds, indigo_env, mock_logger):
    """Issue #192 failure class: an empty ``attributes: {}`` snapshot (the
    lazily-filled matter-server cache on a WS reconnect) is "no information",
    never "implements nothing". Feeding it through here must not read a
    previously-covered device as newly excluded — `informative` gates the
    exclusion check the same way it gates every other capability answer."""
    _indigo, devices = indigo_env
    fallback_node = {
        "node_id": 0x71, "available": True,
        "attributes": {
            "0/47/12": 160,
            "1/29/0": [{"0": 770}],
            "1/1026/0": 2100,
            "2/29/0": [{"0": 775}],
            "2/1029/0": 5500,
        },
    }
    ds.create_from_raw(fallback_node, "Fallback Bridge")
    hum_id = ds.lookup(0x71, 2, "matterHumiditySensor")
    assert hum_id is not None
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True

    empty_node = {"node_id": 0x71, "available": True, "attributes": {}}
    mock_logger.info.reset_mock()
    ds.create_from_raw(empty_node, "Fallback Bridge")
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert not [m for m in info_msgs if _EXCLUSION_PHRASE in m]


def test_device_without_battery_prop_at_excluded_endpoint_stays_silent(ds, indigo_env, mock_logger):
    """With the ``has_reading`` gate gone, ``SupportsBatteryLevel`` is the only
    gate left. A device that never had the prop (e.g. a mains-powered sibling
    that was never in coverage to begin with) must stay silent forever, not
    just on the pass that happens to freeze a reading."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_CONFINED_NODE, "Half Battery Hub")
    hum_id = ds.lookup(0x64, 2, "matterHumiditySensor")
    assert hum_id is not None
    assert not devices[hum_id].pluginProps.get("SupportsBatteryLevel")

    # A reconcile-shaped re-pass over the identical, still-excluding snapshot.
    mock_logger.info.reset_mock()
    ds.create_from_raw(POWER_SOURCE_CONFINED_NODE, "Half Battery Hub")
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert not [m for m in info_msgs if _EXCLUSION_PHRASE in m]


def test_bridge_child_departure_does_not_warn_of_exclusion(ds, indigo_env, mock_logger):
    """A departed bridge child (its endpoint gone from the snapshot entirely,
    not merely re-routed away) is the orphan sweep's story — reconcile_all
    marks its device unreachable. This diagnostic is only about an endpoint
    that is still present whose routing narrowed, so the presence filter must
    keep it out of the exclusion warning."""
    _indigo, devices = indigo_env
    import copy
    ds.create_from_raw(BRIDGE_ENDPOINT_LIST_NODE, "Multi Sensor Bridge")
    hum_id = ds.lookup(0x65, 2, "matterHumiditySensor")
    assert hum_id is not None
    assert devices[hum_id].pluginProps.get("SupportsBatteryLevel") is True

    # ep2 leaves the node entirely — a bridge child departure, not a routing
    # change. Its device stays in the index; the endpoint itself is gone.
    departed_node = copy.deepcopy(BRIDGE_ENDPOINT_LIST_NODE)
    for key in list(departed_node["attributes"]):
        if key.startswith("2/"):
            del departed_node["attributes"][key]

    mock_logger.info.reset_mock()
    ds.create_from_raw(departed_node, "Multi Sensor Bridge")
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert not [m for m in info_msgs if _EXCLUSION_PHRASE in m]


# ===========================================================================
# issue #85 — BooleanStateConfiguration (0x0080) sensitivity level
# ===========================================================================

# Aqara FP300-shaped node: OccupancySensing (0x0406) co-located with
# BooleanStateConfiguration (0x0080, decimal 128) on endpoint 1 — the live
# hardware wrinkle behind issue #85 (the spec pairs 0x0080 with BooleanState/
# 0x0045 instead, exercised separately below via CONTACT_SENSITIVITY_NODE).
OCCUPANCY_SENSITIVITY_NODE = {
    "node_id": 0x2D,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "1/29/0": [{"0": 263}],   # OccupancySensor
        "1/1030/0": 1,            # occupied
        "1/128/0": 1,             # CurrentSensitivityLevel = Standard
        "1/128/1": 3,             # SupportedSensitivityLevels
    },
}

# BooleanState-paired shape (the spec's own pairing) — a contact sensor
# exposing 0x0080 alongside BooleanState (0x0045, decimal 69).
CONTACT_SENSITIVITY_NODE = {
    "node_id": 0x2E,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 21}],    # ContactSensor
        "1/69/0": True,
        "1/128/0": 2,             # CurrentSensitivityLevel = High
        "1/128/1": 3,             # SupportedSensitivityLevels
    },
}


def test_sensitivity_level_primed_on_motion_sensor_creation(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "Landing Presence")
    dev = devices[ds.lookup(0x2D, 1)]
    assert dev.deviceTypeId == "matterMotionSensor"
    assert dev.states.get("sensitivityLevel") == 1


def test_sensitivity_level_primed_on_contact_sensor_creation(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(CONTACT_SENSITIVITY_NODE, "Back Door")
    dev = devices[ds.lookup(0x2E, 1)]
    assert dev.deviceTypeId == "matterContactSensor"
    assert dev.states.get("sensitivityLevel") == 2


def test_sensitivity_level_live_attribute_event_updates_state(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "Landing Presence")
    dev = devices[ds.lookup(0x2D, 1)]
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x2D, endpoint=1, cluster=0x0080, attribute=0x0000, value=0,
    ))
    assert dev.states.get("sensitivityLevel") == 0


def test_no_leftover_cluster_log_for_boolean_state_config(ds, indigo_env, mock_logger):
    """Regression: before issue #85 registered a handler for 0x0080, this
    node's motion sensor would have logged the issue #81 'does not support
    yet' diagnostic for it — now it must be quiet."""
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "Landing Presence")
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert not any("0x0080" in m and "does not support yet" in m for m in info_msgs)


def test_sensitivity_levels_supported_returns_cached_count(ds, indigo_env):
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "Landing Presence")
    assert ds.sensitivity_levels_supported(0x2D, 1) == 3


def test_sensitivity_levels_supported_none_before_reconcile(ds, indigo_env):
    assert ds.sensitivity_levels_supported(0x2D, 1) is None


def test_sensitivity_levels_supported_refreshed_on_reconcile(ds, indigo_env):
    """The cache must not go stale across a reconnect — reconcile_all re-primes
    it from the fresh snapshot, same as every other capability cache here."""
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "Landing Presence")
    assert ds.sensitivity_levels_supported(0x2D, 1) == 3
    import copy
    changed = copy.deepcopy(OCCUPANCY_SENSITIVITY_NODE)
    changed["attributes"]["1/128/1"] = 5
    ds.reconcile_all([changed])
    assert ds.sensitivity_levels_supported(0x2D, 1) == 5


# ===========================================================================
# issue #84 — KeyError guard: Indigo device deleted out-of-band while its
# Matter node keeps reporting must not raise/spam warnings on every event.
# ===========================================================================

def test_attribute_event_deleted_device_debug_not_bad_update_warning(ds, indigo_env, mock_logger):
    """Non-node-scoped path (_on_attribute): a deletion race is a debug line,
    not the "bad update" WARNING — and must not raise."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    del devices._by_id[dev_id]

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=42, endpoint=1, cluster=0x0006, attribute=0x0000, value=True,
    ))  # must not raise

    debug_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.debug.call_args_list]
    assert any("vanished" in m and str(dev_id) in m for m in debug_msgs)
    warn_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("bad update" in m for m in warn_msgs)


def test_apply_states_deleted_device_debug_logs_no_raise(ds, indigo_env, mock_logger):
    """apply_states (the single asyncio→Indigo write seam): a deletion race
    guards only the dict lookup — debug log, no raise."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    del devices._by_id[dev_id]

    ds.apply_states(dev_id, [{"key": "onOffState", "value": True}])  # must not raise

    debug_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.debug.call_args_list]
    assert any("vanished" in m and str(dev_id) in m for m in debug_msgs)


def test_apply_states_non_keyerror_update_failure_still_propagates(ds, indigo_env):
    """Pins today's behaviour: apply_states' guard is scoped to the dict
    lookup only. None of apply_states' three callers (_on_attribute's two
    paths, the reconcile prime path) catch its exceptions today, so an
    ``updateStatesOnServer`` failure that is NOT a deletion race must keep
    propagating exactly as before this fix."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    dev_id = ds.lookup(42, 1)
    devices[dev_id].updateStatesOnServer = Mock(side_effect=RuntimeError("server write failed"))

    with pytest.raises(RuntimeError, match="server write failed"):
        ds.apply_states(dev_id, [{"key": "onOffState", "value": True}])


def _create_aq_battery_node(ds, indigo_env, node_id=830):
    """AQ node (4 additive devices on endpoint 1) + node-scoped PowerSource
    on endpoint 0 — mirrors test_battery_fanout_reaches_all_devices_on_aq_endpoint."""
    _indigo, devices = indigo_env
    node = {
        "node_id": node_id,
        "attributes": {
            "0/47/12": 120,        # PowerSource.BatPercentRemaining = 60 %
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
    ds.create_from_raw(node, "AQ Sensor")
    _indigo.device.create = original_create
    return node_id


def test_node_scoped_deleted_device_debug_not_warning(ds, indigo_env, mock_logger):
    """Node-scoped path (PowerSource fan-out): one sibling device deleted
    out-of-band must be a debug line, not the "bad update" WARNING, and the
    fan-out must still reach the surviving siblings on the endpoint."""
    _indigo, devices = indigo_env
    node_id = _create_aq_battery_node(ds, indigo_env)
    victim_id = ds.lookup(node_id, 1, "matterCO2Sensor")
    del devices._by_id[victim_id]

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=node_id, endpoint=0, cluster=0x002F, attribute=0x000C, value=160,
    ))  # must not raise

    debug_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.debug.call_args_list]
    assert any("vanished" in m and str(victim_id) in m for m in debug_msgs)
    warn_msgs = [c[0][0] for c in mock_logger.warning.call_args_list]
    assert not any("bad update" in m for m in warn_msgs)
    # surviving siblings still received the fan-out
    for type_id in ("matterAirQualitySensor", "matterPM25Sensor", "matterTVOCSensor"):
        dev_id = ds.lookup(node_id, 1, type_id)
        assert devices[dev_id].states.get("batteryLevel") == 80


def test_node_scoped_handler_keyerror_still_bad_update_warning(ds, indigo_env, mock_logger, monkeypatch):
    """Scoping test: the KeyError guard at the node-scoped site covers ONLY
    the ``indigo.devices[dev_id]`` lookup. A handler that itself raises
    KeyError (for reasons unrelated to a deleted device) must still hit the
    broad "bad update" WARNING, not be mistaken for a deletion race."""
    _indigo, devices = indigo_env
    node_id = _create_aq_battery_node(ds, indigo_env)
    handler = ds.registry.handler_for_cluster(0x002F)
    monkeypatch.setattr(handler, "on_attribute_update", Mock(side_effect=KeyError("unrelated")))

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=node_id, endpoint=0, cluster=0x002F, attribute=0x000C, value=160,
    ))  # must not raise

    warn_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.warning.call_args_list]
    assert any("bad update" in m for m in warn_msgs)
    debug_msgs = [c[0][0] for c in mock_logger.debug.call_args_list]
    assert not any("vanished" in m for m in debug_msgs)


# ===========================================================================
# issue #186 — the generalised writable-setting limits cache
# ===========================================================================

#: The FP300 as it actually reports (issue #186 research, node 56): a Matter 1.4
#: unit, so HoldTime (1030/3) + HoldTimeLimits (1030/4) and NOT the deprecated
#: 0x0010/0x0011 PIR delays, which this firmware does not implement at all.
FP300_SETTINGS_NODE = {
    "node_id": 0x38,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "1/29/0": [{"0": 263}],
        "1/1030/0": 0,
        "1/1030/3": 10,
        "1/1030/4": {"0": 1, "1": 300, "2": 10},   # struct: index-keyed on the wire
        "1/128/0": 1,
        "1/128/1": 3,
    },
}


def test_setting_limits_caches_hold_time_limits(ds, indigo_env):
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) == {"0": 1, "1": 300, "2": 10}


def test_setting_limits_caches_the_sensitivity_count_too(ds, indigo_env):
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    assert ds.setting_limits(0x38, 1, 0x0080, 0x0001) == 3


def test_setting_limits_stores_the_RAW_value_not_a_parsed_one(ds, indigo_env):
    """Each setting declares its own parser (a struct here, a count there), so
    the cache must not presume a shape."""
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    assert isinstance(ds.setting_limits(0x38, 1, 0x0406, 0x0004), dict)


def test_setting_limits_is_none_for_an_attribute_the_node_lacks(ds, indigo_env):
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "Landing Presence")
    # This fixture is a pre-1.4-shaped node: sensitivity but no HoldTimeLimits.
    assert ds.setting_limits(0x2D, 1, 0x0406, 0x0004) is None


def test_setting_limits_is_none_before_any_reconcile(ds, indigo_env):
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) is None


def test_setting_limits_refreshed_on_reconcile(ds, indigo_env):
    """A firmware update can widen a range; the cache must not outlive it."""
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    import copy
    changed = copy.deepcopy(FP300_SETTINGS_NODE)
    changed["attributes"]["1/1030/4"] = {"0": 1, "1": 600, "2": 10}
    ds.reconcile_all([changed])
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004)["1"] == 600


def test_sensitivity_levels_supported_still_works_off_the_general_cache(ds, indigo_env):
    """Issue #85's accessor is now a view over the #186 cache — it must not have
    changed behaviour for the action picker that depends on it."""
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    assert ds.sensitivity_levels_supported(0x38, 1) == 3


def test_sensitivity_levels_supported_survives_an_unparseable_count(ds, indigo_env):
    import copy
    changed = copy.deepcopy(FP300_SETTINGS_NODE)
    changed["attributes"]["1/128/1"] = "three"
    ds.create_from_raw(changed, "Landing Presence")
    assert ds.sensitivity_levels_supported(0x38, 1) is None


def test_hold_time_is_primed_onto_the_device_at_creation(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    dev = devices[ds.lookup(0x38, 1)]
    assert dev.states.get("holdTime") == 10


#: A plug implementing OnOff's Lighting feature — the IKEA GRILLPLATS, read off
#: the live fabric on 2026-08-11 (node 0x34): AttributeList carries
#: 16385/16386/16387 and StartUpOnOff really reads 1.
#:
#: NOTE: issue #190 attributes this the other way round — it names the Shelly as
#: the one implementing Lighting and the GRILLPLATS as the one without. The live
#: devices say the opposite, and the list the issue quotes for the GRILLPLATS is
#: byte-for-byte node 0x32's (the Shelly's), 65530 and all. The issue's reasoning
#: is unaffected — one plug has these attributes and one does not — but the two
#: device names are swapped, and these fixtures used to inherit that.
LIGHTING_PLUG_NODE = {
    "node_id": 0x34,
    "available": True,
    "attributes": {
        "0/40/1": "IKEA",
        "0/40/3": "GRILLPLATS Plug",
        "1/29/0": [{"0": 266}],
        "1/6/0": True,
        "1/6/16385": 0,
        "1/6/16387": 1,
        "1/6/65531": [0, 16384, 16385, 16386, 16387, 65528, 65529, 65531, 65532, 65533],
    },
}

#: A pre-1.4 occupancy sensor. Its deprecated PirOccupiedToUnoccupiedDelay is
#: writable, really implemented, and not offered by the plugin — which declares
#: HoldTime (0x0003) instead, and this device does not implement it. So it is a
#: genuine gap, and it is the fixture the report-MACHINERY tests use: the
#: GRILLPLATS below stopped being one when #197 established that OnTime and
#: OffWaitTime are command parameters rather than settings.
PIR_DELAY_NODE = {
    "node_id": 0x35,
    "available": True,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "FP300",
        "1/29/0": [{"0": 263}],   # OccupancySensor
        "1/1030/0": 1,            # occupied
        "1/1030/16": 30,          # PirOccupiedToUnoccupiedDelay (0x0010)
        "1/1030/65531": [0, 16, 65528, 65529, 65531, 65532, 65533],
    },
}


#: …and one that does not: the Shelly 1PM Mini Gen3 (live node 0x32). OnOff and
#: nothing else — no 16385/16386/16387.
PLAIN_PLUG_NODE = {
    "node_id": 0x32,
    "available": True,
    "attributes": {
        "0/40/1": "Shelly",
        "0/40/3": "1PM Mini Gen3",
        "1/29/0": [{"0": 266}],
        "1/6/0": True,
        "1/6/65531": [0, 65528, 65529, 65530, 65531, 65532, 65533],
    },
}


def test_attribute_list_is_cached_per_cluster(ds, indigo_env):
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert 16387 in ds.attribute_list(0x34, 1, 0x0006)


def test_attribute_list_is_none_for_an_uncached_cluster(ds, indigo_env):
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert ds.attribute_list(0x34, 1, 0x0406) is None


def test_attribute_list_is_none_before_reconcile(ds, indigo_env):
    assert ds.attribute_list(0x34, 1, 0x0006) is None


def test_a_plain_plug_reports_a_list_without_the_lighting_attributes(ds, indigo_env):
    ds.create_from_raw(PLAIN_PLUG_NODE, "Desk Light Switch")
    listed = ds.attribute_list(0x32, 1, 0x0006)
    assert listed is not None, "the list itself is known"
    assert 16387 not in listed, "…and it says the device lacks StartUpOnOff"


def test_lighting_settings_are_primed_onto_the_relay(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    assert dev.states.get("startUpOnOff") == 1


def test_the_relay_handler_no_longer_maps_onTime_to_a_STATE():
    """Pinned against the handler, not against a fixture.

    The obvious version of this — asserting "onTime" not in a created device's
    states — passes for the wrong reason: on_off.on_attribute_update returns {}
    for any key the device does not already carry, and the fake's declared state
    set was edited alongside the code. Restoring ATTR_ON_TIME to SETTING_STATES
    left the whole suite green, so the handler could resurrect the state and
    nothing would notice (#197).
    """
    from matter_handlers.on_off import OnOffHandler
    from matter_handlers.settings import ATTR_ON_TIME
    assert ATTR_ON_TIME not in OnOffHandler.SETTING_STATES
    assert "onTime" not in OnOffHandler.SETTING_STATES.values()
    assert ATTR_ON_TIME not in OnOffHandler().attributes_to_subscribe()


# ===========================================================================
# issues #190 / #192 — the capability caches are rebuilt, evicted, and never
# believe an empty snapshot
# ===========================================================================

#: matter-server's attribute cache is populated LAZILY: getNodeDetails returns
#: `attributeCache.get(nodeId) ?? {}` and fills in the background, so the first
#: node_updated after a server restart legitimately carries no attributes at all
#: and a second follows once the cache is warm. Every capability answer is
#: derived from these attributes, so an empty one must mean "no information".
COLD_CACHE_NODE = {"node_id": 0x32, "available": True, "attributes": {}}


def test_an_empty_snapshot_does_not_retract_an_attribute_list(ds, indigo_env):
    ds.create_from_raw(PLAIN_PLUG_NODE, "Desk Light Switch")
    ds.reconcile_all([COLD_CACHE_NODE])
    assert ds.attribute_list(0x32, 1, 0x0006) is not None, (
        "a cold-cache node_updated must not be read as 'implements nothing'")


def test_an_empty_snapshot_does_not_retract_setting_limits(ds, indigo_env):
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    ds.reconcile_all([{"node_id": 0x38, "available": True, "attributes": {}}])
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) is not None


def test_an_empty_snapshot_does_not_retract_the_power_coverage(ds, indigo_env):
    """The same bug, already live on a different cache before #192.

    The coverage map (`_power_source_eps` before #205) popped whenever a
    snapshot carried no PowerSource endpoint — which an empty snapshot never
    does — so a routine matter-server restart dropped a node's battery
    endpoints.
    """
    ds.create_from_raw(TEMP_BATTERY_NODE, "Hall Temperature")
    before = dict(ds._power_coverage)
    assert before, "fixture must actually bear a PowerSource for this to test anything"
    ds.reconcile_all([{"node_id": 0x42, "available": True, "attributes": {}}])
    assert ds._power_coverage == before


def test_a_cluster_that_stops_reporting_its_list_drops_the_cached_answer(ds, indigo_env):
    """The stale case an overwriting cache could never fix.

    Re-reporting a CHANGED list always overwrote the same key, so that half was
    never broken. What survived forever was a key the node stopped reporting at
    all — the answer then stayed YES with nothing behind it.
    """
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert 16387 in ds.attribute_list(0x34, 1, 0x0006)
    import copy
    silent = copy.deepcopy(LIGHTING_PLUG_NODE)
    del silent["attributes"]["1/6/65531"]
    ds.reconcile_all([silent])
    assert ds.attribute_list(0x34, 1, 0x0006) is None, (
        "a list no longer reported is unknown, not the list from last time")


def test_a_reused_node_id_does_not_inherit_the_previous_devices_answers(ds, indigo_env):
    """Node ids are assigned by the controller at commissioning, so re-use
    across a decommission/recommission cycle is ordinary, not exotic.

    The recommissioned unit is a DIFFERENT device type on purpose: a like-for-
    like swap overwrites the same keys and hides the bug, which is precisely why
    it went unnoticed.
    """
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert ds.attribute_list(0x34, 1, 0x0006) is not None
    import copy
    recommissioned = copy.deepcopy(FP300_SETTINGS_NODE)
    recommissioned["node_id"] = 0x34          # same id, different physical device
    ds.reconcile_all([recommissioned])
    assert ds.attribute_list(0x34, 1, 0x0006) is None, (
        "the OnOff answer belonged to the plug that used to hold this id")


def test_node_removed_evicts_the_capability_caches(ds, indigo_env):
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_REMOVED, node_id=0x34,
        raw={"event": "node_removed", "data": 0x34}))
    assert ds.attribute_list(0x34, 1, 0x0006) is None


def test_a_node_missing_from_get_nodes_KEEPS_its_capability_answers(ds, indigo_env):
    """The deliberate non-eviction, and it is not an oversight.

    reconcile_all's own comment says a transient short or empty get_nodes() puts
    a LIVE node in `dropped`. Forgetting its answers is not harmless: unknown
    makes offered_settings withhold every spec-bounded setting, so the whole
    Device Settings section of a working device goes blank. Eviction is left to
    node_removed, which is an explicit decommission. The re-used node id that
    motivated #192 is handled by the per-node rebuild instead.
    """
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    ds.reconcile_all([])          # authoritative list came back short/empty
    assert ds.attribute_list(0x34, 1, 0x0006) is not None


def test_a_blip_does_not_blank_the_device_settings_section(ds, indigo_env):
    """The user-visible consequence of the above, asserted through the gate the
    Edit Device dialog actually uses rather than through the cache."""
    import device_settings
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    ds.reconcile_all([])
    offered = device_settings.offered_settings(
        "matterMotionSensor",
        lambda cluster, attr: ds.setting_limits(0x38, 1, cluster, attr),
        lambda cluster: ds.attribute_list(0x38, 1, cluster))
    assert sorted(o.setting.key for o in offered) == ["holdTime", "sensitivityLevel"]


def test_decommissioning_DOES_drop_the_limits_cache_too(ds, indigo_env):
    """The limits half of #192 — a separate dict from the AttributeLists, and
    the one the issue names first."""
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) is not None
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_REMOVED, node_id=0x38,
        raw={"event": "node_removed", "data": 0x38}))
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) is None


def test_a_limits_attribute_that_stops_being_reported_drops_out(ds, indigo_env):
    """"Validate against bounds from a previous firmware" — the failure #192
    names. Rebuilding the limits dict is what prevents it."""
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    import copy
    silent = copy.deepcopy(FP300_SETTINGS_NODE)
    del silent["attributes"]["1/1030/4"]        # HoldTimeLimits gone
    ds.reconcile_all([silent])
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) is None


def test_a_reused_node_id_does_not_inherit_the_previous_devices_LIMITS(ds, indigo_env):
    """The mirror of the AttributeList case, on the other cache."""
    ds.create_from_raw(FP300_SETTINGS_NODE, "Landing Presence")
    import copy
    recommissioned = copy.deepcopy(LIGHTING_PLUG_NODE)
    recommissioned["node_id"] = 0x38
    ds.reconcile_all([recommissioned])
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004) is None


def test_node_removed_does_NOT_forget_the_power_coverage(ds, indigo_env):
    """The non-eviction survives #205; its justification does not.

    Before #205 the cache was read as ``len(...) > 1``, so an absent entry was a
    positive "one power source" — evicting it selected the node-wide fan-out and
    re-opened issue #82 on a multi-battery bridge. Coverage has no such failure
    mode (absent = no coverage = the safe answer), so this is now kept because
    eviction buys nothing rather than because it would break something. Either
    way `_forget_node_capabilities` must leave it alone.
    """
    ds.create_from_raw(TEMP_BATTERY_NODE, "Hall Temperature")
    before = dict(ds._power_coverage)
    assert before
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_REMOVED, node_id=0x42,
        raw={"event": "node_removed", "data": 0x42}))
    assert ds._power_coverage == before


#: Two OnOff endpoints on one node, and only ONE of them implements the Lighting
#: feature — the case that catches a cache keyed on the node while ignoring the
#: endpoint. Issue #82 was this same cross-endpoint mistake on another cache.
SPLIT_LIGHTING_BRIDGE_NODE = {
    "node_id": 0x40,
    "available": True,
    "attributes": {
        "0/40/1": "Acme",
        "0/40/3": "Two Gang",
        "1/29/0": [{"0": 266}],
        "1/6/0": True,
        "1/6/16387": 1,
        "1/6/65531": [0, 16384, 16385, 16386, 16387, 65528, 65529, 65531, 65532, 65533],
        "2/29/0": [{"0": 266}],
        "2/6/0": False,
        "2/6/65531": [0, 65528, 65529, 65531, 65532, 65533],
    },
}


def test_each_endpoint_keeps_its_own_capability_answer(ds, indigo_env):
    ds.create_from_raw(SPLIT_LIGHTING_BRIDGE_NODE, "Two Gang")
    assert 16387 in ds.attribute_list(0x40, 1, 0x0006)
    assert 16387 not in ds.attribute_list(0x40, 2, 0x0006), (
        "endpoint 2 must not inherit endpoint 1's answer")


def test_the_endpoint_is_part_of_the_limits_key_too(ds, indigo_env):
    """Guards the same hardcoded-endpoint mistake on the limits cache."""
    import copy
    node = copy.deepcopy(FP300_SETTINGS_NODE)
    node["attributes"]["2/1030/4"] = {"0": 5, "1": 900, "2": 30}
    ds.create_from_raw(node, "Landing Presence")
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004)["1"] == 300
    assert ds.setting_limits(0x38, 2, 0x0406, 0x0004)["1"] == 900


def test_a_reordered_attribute_list_is_not_a_capability_change(ds, indigo_env):
    """No ordering is promised for an AttributeList and `implements` reads it as
    a set, so a reshuffle must not rebuild state lists or reprint the log."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    before = dev.state_list_rebuilds
    import copy
    shuffled = copy.deepcopy(LIGHTING_PLUG_NODE)
    shuffled["attributes"]["1/6/65531"] = list(
        reversed(shuffled["attributes"]["1/6/65531"]))
    ds.reconcile_all([shuffled])
    assert dev.state_list_rebuilds == before


def test_a_node_updated_event_rebuilds_the_cache_too(ds, indigo_env):
    """The live path a firmware change actually arrives on — reconcile is only
    the connect-time one."""
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    import copy
    downgraded = copy.deepcopy(LIGHTING_PLUG_NODE)
    downgraded["attributes"]["1/6/65531"] = [0, 65528, 65529, 65531, 65532, 65533]
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_UPDATED, node_id=0x34,
        raw={"event": "node_updated", "data": downgraded}))
    assert 16387 not in ds.attribute_list(0x34, 1, 0x0006)


def test_a_device_created_on_an_unchanged_pass_still_gets_its_states_rebuilt(ds, indigo_env):
    """A device the user deleted and a node_updated recreated. The AttributeLists
    match the cache, so the change flag is False — but the NEW device still
    carries the unfiltered type-level state list and must be revisited."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev_id = ds.lookup(0x34, 1)
    del devices._by_id[dev_id]          # the user deleted it in the Indigo UI
    ds.rebuild_index()
    ds.reconcile_all([LIGHTING_PLUG_NODE])
    recreated = devices[ds.lookup(0x34, 1)]
    assert recreated.state_list_rebuilds >= 1


def test_a_refresh_that_fails_is_retried_on_the_next_pass(ds, indigo_env):
    """The cache has already moved by the time the refresh runs, so a refresh
    that throws can never be rediscovered — without the pending set the stale
    state list is latched until a plugin restart."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    import copy
    downgraded = copy.deepcopy(LIGHTING_PLUG_NODE)
    downgraded["attributes"]["1/6/65531"] = [0, 65528, 65529, 65531, 65532, 65533]

    def explode():
        raise RuntimeError("Indigo refused the rebuild")

    dev.stateListOrDisplayStateIdChanged = explode
    ds.reconcile_all([downgraded])          # change detected, refresh fails
    del dev.stateListOrDisplayStateIdChanged
    before = dev.state_list_rebuilds
    ds.reconcile_all([downgraded])          # cache now AGREES — only the pending set can save us
    assert dev.state_list_rebuilds > before


def test_eviction_leaves_the_devices_alone(ds, indigo_env):
    """Forgetting a capability answer must never look like a state change —
    unknown is 'fall back to older evidence', not 'the device lacks this'."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_NODE_REMOVED, node_id=0x34,
        raw={"event": "node_removed", "data": 0x34}))
    assert dev.states.get("startUpOnOff") == 1


# --- when the state list is rebuilt (issue #190) ---------------------------

def test_a_changed_attribute_list_rebuilds_the_devices_state_list(ds, indigo_env):
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    before = dev.state_list_rebuilds
    import copy
    downgraded = copy.deepcopy(LIGHTING_PLUG_NODE)
    downgraded["attributes"]["1/6/65531"] = [0, 65528, 65529, 65531, 65532, 65533]
    ds.reconcile_all([downgraded])
    assert dev.state_list_rebuilds > before


def test_an_unchanged_reconcile_does_not_rebuild_the_state_list(ds, indigo_env):
    """The no-flap guarantee. matter-server emits node_updated on every
    availability change, and a device dropping offline and returning reports the
    same AttributeList both times — so nothing may move."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    before = dev.state_list_rebuilds
    ds.reconcile_all([LIGHTING_PLUG_NODE])
    ds.reconcile_all([LIGHTING_PLUG_NODE])
    assert dev.state_list_rebuilds == before


def test_an_empty_snapshot_does_not_rebuild_the_state_list(ds, indigo_env):
    """The flap this whole rule exists to prevent — and note which way round it
    goes: unknown KEEPS a state, so believing the cold-cache frame would
    RESURRECT the spurious 0s #190 removes, for the next warm frame to withdraw
    them again."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    dev = devices[ds.lookup(0x34, 1)]
    before = dev.state_list_rebuilds
    ds.reconcile_all([{"node_id": 0x34, "available": True, "attributes": {}}])
    assert dev.state_list_rebuilds == before


def test_a_limits_only_change_does_not_rebuild_the_state_list(ds, indigo_env):
    """Limits decide permitted VALUES, not which states exist — rebuilding a
    state list over one would be churn for nothing.

    The fixture carries a real AttributeList on purpose: without one, "the
    AttributeLists did not change" would be trivially true because there were
    none on either side, and the test would pass for the wrong reason.
    """
    _indigo, devices = indigo_env
    import copy
    listed = copy.deepcopy(FP300_SETTINGS_NODE)
    listed["attributes"]["1/1030/65531"] = [0, 1, 2, 3, 4, 65531]
    ds.create_from_raw(listed, "Landing Presence")
    dev = devices[ds.lookup(0x38, 1)]
    before = dev.state_list_rebuilds
    changed = copy.deepcopy(listed)
    changed["attributes"]["1/1030/4"] = {"0": 1, "1": 600, "2": 10}
    ds.reconcile_all([changed])
    assert dev.state_list_rebuilds == before
    assert ds.setting_limits(0x38, 1, 0x0406, 0x0004)["1"] == 600, "…but the limit did move"


# ---------------------------------------------------------------------------
# issue #191 — the automatic settable-attribute report
#
# The decision logic lives in settings_report and is tested there; these pin the
# WIRING, which is the half a pure test cannot see: when it fires, when it must
# not, and that it can never take reconcile down with it.
# ---------------------------------------------------------------------------

def _parsed(raw):
    """A NodeInfo from a raw fixture — the shape report_settable_attributes takes."""
    from matter_model import parse_node
    return parse_node(raw)


def _survey_lines(mock_logger) -> list[str]:
    """Every INFO line the settable-attribute report emitted."""
    return [call.args[1] for call in mock_logger.info.call_args_list
            if call.args and call.args[0] == "%s"]


def _survey_report(mock_logger) -> str:
    return "\n".join(_survey_lines(mock_logger))


@pytest.fixture
def surveying_ds(ds):
    """A DeviceSync with the once-per-device log wired, as plugin.startup does."""
    import settings_report
    ds.survey_log = settings_report.SurveyLog()
    return ds


def test_a_new_device_reports_what_it_exposes(surveying_ds, indigo_env, mock_logger):
    """A pre-1.4 occupancy sensor implements the deprecated PIR delay, the plugin
    declares HoldTime instead, and this unit does not implement HoldTime — so
    exactly one line is owed."""
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    body = _survey_report(mock_logger)
    assert "PirOccupiedToUnoccupiedDelay (0x0010)" in body
    assert "Nothing is wrong" in body


def test_the_lighting_plug_now_has_NOTHING_to_report(
        surveying_ds, indigo_env, mock_logger):
    """The behaviour change #197 shipped, asserted end to end from raw
    matter-server JSON.

    The GRILLPLATS implements all three of OnOff's writable attributes. The
    plugin offers exactly one of them as a setting (StartUpOnOff); the other two
    are parameters of OnWithTimedOff. Before #197 this device produced an INFO
    naming OffWaitTime as a setting that "COULD be added" — a recommendation to
    build something that cannot work. It must now be silent.
    """
    surveying_ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert _survey_lines(mock_logger) == []


def test_the_report_names_the_indigo_device(surveying_ds, indigo_env, mock_logger):
    """The user knows the device by its Indigo name, not by an endpoint number."""
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    assert "Kitchen motion - Motion (endpoint 1)" in _survey_report(mock_logger)


def test_the_report_fires_once_and_not_on_every_reconcile(surveying_ds, indigo_env, mock_logger):
    """The failure this guards is the whole design constraint: an INFO block on
    every reconcile pass — i.e. every reconnect — is wallpaper, and wallpaper is
    not read."""
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    first = len(_survey_lines(mock_logger))
    assert first
    surveying_ds.reconcile_all([PIR_DELAY_NODE])
    surveying_ds.reconcile_all([PIR_DELAY_NODE])
    assert len(_survey_lines(mock_logger)) == first


def test_a_device_with_no_gap_is_banked_silently(surveying_ds, indigo_env, mock_logger):
    """A fully-supported device says nothing — and must not re-survey forever
    because "nothing to report" was never recorded as an answer."""
    surveying_ds.create_from_raw(PLAIN_PLUG_NODE, "Shelly")
    assert _survey_lines(mock_logger) == []
    assert not surveying_ds.survey_log.should_report(
        0x32, surveying_ds.survey_node(_parsed(PLAIN_PLUG_NODE)).fingerprint())


def test_firmware_moving_reports_again(surveying_ds, indigo_env, mock_logger):
    """#186 established behaviour here is firmware-specific, so a device on new
    firmware is a device that has not been reported."""
    import copy
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    first = len(_survey_lines(mock_logger))
    updated = copy.deepcopy(PIR_DELAY_NODE)
    updated["attributes"]["0/40/10"] = "2.0.0"
    surveying_ds.reconcile_all([updated])
    assert len(_survey_lines(mock_logger)) > first


def test_an_empty_snapshot_is_not_surveyed_at_all(surveying_ds, indigo_env, mock_logger):
    """matter-server's attribute cache populates lazily, so the first
    node_updated after a server restart legitimately carries no attributes at
    all — an empty snapshot is "no information", never "implements nothing"
    (issue #192).

    What that costs is a PREFS WRITE, and that is what this asserts. The report
    itself survives an unguarded cold frame by luck rather than design: the
    fingerprint includes the settable set, so the cold answer and the warm one
    differ and the warm one still reports. But banking it means an Indigo
    database write per node per matter-server restart, recording nothing. Stated
    the long way round because a mutation check proved the obvious assertion —
    that the real report still arrives — passes with the guard removed.
    """
    saved = []
    import settings_report
    surveying_ds.survey_log = settings_report.SurveyLog(save=saved.append)
    surveying_ds.reconcile_all([{"node_id": 0x35, "available": True, "attributes": {}}])
    assert _survey_lines(mock_logger) == []
    assert saved == [], "a cold-cache frame must not write an answer to prefs"
    surveying_ds.reconcile_all([PIR_DELAY_NODE])
    assert "PirOccupiedToUnoccupiedDelay (0x0010)" in _survey_report(mock_logger)
    assert saved, "…but the warm frame does"


def test_no_report_at_all_without_a_survey_log(ds, indigo_env, mock_logger):
    """No log means no way to be quiet the second time, so the automatic report
    stays off rather than repeating on every pass."""
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert _survey_lines(mock_logger) == []


def test_forcing_reports_again_even_when_already_seen(surveying_ds, indigo_env, mock_logger):
    """The menu item's whole purpose: a user asking now deserves an answer."""
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    first = len(_survey_lines(mock_logger))
    assert surveying_ds.report_settable_attributes(_parsed(PIR_DELAY_NODE), force=True)
    assert len(_survey_lines(mock_logger)) > first


def test_forcing_a_device_with_no_gap_says_so(surveying_ds, indigo_env, mock_logger):
    """Silence would read as a failure to someone who just clicked the menu."""
    assert not surveying_ds.report_settable_attributes(_parsed(PLAIN_PLUG_NODE), force=True)
    assert any("implements no settable Matter attributes" in str(call)
               for call in mock_logger.info.call_args_list)


def test_decommissioning_forgets_the_report_mark(surveying_ds, indigo_env, mock_logger):
    """Node ids are re-used across a decommission/recommission cycle, so a
    different device must not inherit the old one's "already told you"."""
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    first = len(_survey_lines(mock_logger))
    surveying_ds.handle_event(MatterEvent(kind=protocol.EVT_NODE_REMOVED, node_id=0x35, raw={}))
    surveying_ds.create_from_raw(PIR_DELAY_NODE, "Kitchen motion")
    assert len(_survey_lines(mock_logger)) > first


def test_a_broken_report_never_sinks_the_create_pass(surveying_ds, indigo_env, mock_logger):
    """A diagnostic hanging off the creation path that can break creation is
    strictly worse than no diagnostic."""
    surveying_ds.survey_log = Mock()
    surveying_ds.survey_log.should_report.side_effect = RuntimeError("boom")
    result = surveying_ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert result["indigoDeviceIds"], "the device must still be created"
    assert mock_logger.warning.called


def test_offered_pairs_follow_the_endpoint_device_type(surveying_ds, indigo_env):
    """What the plugin offers is decided by the Indigo TYPE an endpoint became —
    a setting with no ConfigUI field on that type is not offered however capable
    the hardware is."""
    from matter_handlers.settings import ATTR_START_UP_ON_OFF, CLUSTER_ON_OFF
    surveying_ds.create_from_raw(LIGHTING_PLUG_NODE, "Grillplats socket")
    assert (CLUSTER_ON_OFF, ATTR_START_UP_ON_OFF) in surveying_ds.offered_setting_pairs(0x34, 1)
    assert surveying_ds.offered_setting_pairs(0x34, 99) == set(), "no device, nothing offered"


# ===========================================================================
# issue #204 / ADR-0008 — the synthetic per-node device
#
# Gated on ep 0's own BasicInformation AttributeList (0xFFFB, decimal 65531)
# evidencing NodeLabel (0x0005) OR SoftwareVersionString (0x000A) — neither of
# which any existing fixture in this file reports, so every pre-#204 test
# above is unaffected by this section existing at all. `_with_node_evidence`
# is the one place that adds it, so every test below is explicit about opting
# a fixture INTO node-device creation.
# ===========================================================================

def _with_node_evidence(raw: dict, node_label: str = None) -> dict:
    """A deepcopy of *raw* with ep 0's BasicInformation AttributeList made to
    evidence NodeLabel (0x0005) and SoftwareVersionString (0x000A) — the
    ADR-0003 gate `_ensure_node_device` checks. Optionally also stamps
    NodeLabel's own value (0/40/5) so a test can assert priming."""
    node = copy.deepcopy(raw)
    node["attributes"]["0/40/65531"] = [1, 2, 3, 4, 5, 10]
    if node_label is not None:
        node["attributes"]["0/40/5"] = node_label
    return node


def _with_attribute_list(raw: dict, attrs: list) -> dict:
    """A deepcopy of *raw* with ep 0's BasicInformation AttributeList set to
    exactly *attrs* — for exercising the ADR-0003 OR gate's three outcomes
    directly, rather than always granting both (issue #204 review, fix I.1:
    `_with_node_evidence` above always grants both attributes, so an
    and→or mutation of the gate would survive the suite)."""
    node = copy.deepcopy(raw)
    node["attributes"]["0/40/65531"] = attrs
    return node


def test_no_evidence_no_node_device(ds, indigo_env):
    """ADR-0003: unknown is not yes. RELAY_NODE itself carries no AttributeList
    for BasicInformation at all, so this pass is a node "mid-interview"."""
    result = ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert result["nodeDeviceId"] is None
    assert ds.lookup(42, 0, "matterNode") is None


def test_no_evidence_no_node_device_stays_silent(ds, indigo_env, mock_logger):
    """Fix D (issue #204 review): a mid-interview node (no AttributeList at
    all — both implements() calls return None, not False) must not log the
    non-conformant INFO. None is not a verdict."""
    ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert not any(
        "non-conformant" in str(call.args) for call in mock_logger.info.call_args_list
    )


def test_attribute_list_missing_both_ids_no_node_device_and_logs_once(ds, indigo_env, mock_logger):
    """ADR-0003 gate OR, NEITHER case (issue #204 review, fix I.1): the
    AttributeList is positively read (not absent) but contains neither
    NodeLabel (5) nor SoftwareVersionString (10) — a stable non-conformant
    verdict, not "unknown". Fix D: logged once, not once per reconcile pass."""
    evidenced = _with_attribute_list(RELAY_NODE, [1, 2, 3, 4])
    result = ds.create_from_raw(evidenced, "Office Plug")
    assert result["nodeDeviceId"] is None
    assert sum(
        "non-conformant" in str(call.args) for call in mock_logger.info.call_args_list
    ) == 1

    # A second pass over the same non-conformant node must not repeat the INFO.
    ds.create_from_raw(evidenced, "Office Plug")
    assert sum(
        "non-conformant" in str(call.args) for call in mock_logger.info.call_args_list
    ) == 1


def test_attribute_list_with_only_node_label_creates_device(ds, indigo_env):
    """ADR-0003 gate OR, ONLY-5 case (issue #204 review, fix I.1)."""
    evidenced = _with_attribute_list(RELAY_NODE, [1, 2, 3, 5])
    result = ds.create_from_raw(evidenced, "Office Plug")
    assert result["nodeDeviceId"] is not None


def test_attribute_list_with_only_sw_version_creates_device(ds, indigo_env):
    """ADR-0003 gate OR, ONLY-10 case (issue #204 review, fix I.1)."""
    evidenced = _with_attribute_list(RELAY_NODE, [1, 2, 3, 10])
    result = ds.create_from_raw(evidenced, "Office Plug")
    assert result["nodeDeviceId"] is not None


def test_node_device_created_once_evidence_arrives_on_a_later_pass(ds, indigo_env):
    """The node "finishes its interview" between two reconcile passes — the
    same shape #190's capability gates already handle for settings/states."""
    _indigo, devices = indigo_env
    result1 = ds.create_from_raw(RELAY_NODE, "Office Plug")
    assert result1["nodeDeviceId"] is None
    evidenced = _with_node_evidence(RELAY_NODE)
    result2 = ds.create_from_raw(evidenced, "Office Plug")
    assert result2["nodeDeviceId"] is not None
    assert devices[result2["nodeDeviceId"]].deviceTypeId == "matterNode"


def test_single_endpoint_node_creates_node_device_without_touching_the_relay(ds, indigo_env):
    """The node device must not flip `multi = len(plan) > 1` for a genuinely
    single-endpoint node (the most dangerous regression stage 1 could have
    introduced): the relay is named by its ROLE, never by an endpoint number,
    and never as one of two identical siblings.

    Stage 2 (ADR-0008 option B) is what changed the names here: the relay takes
    its role suffix and the node device takes the bare base, so the numeric
    collision suffix stage 1 documented as temporary is gone."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    relay_id = ds.lookup(42, 1, "matterRelay")
    node_id = result["nodeDeviceId"]
    assert relay_id is not None and node_id is not None and relay_id != node_id
    assert devices[relay_id].name == "Office Plug - Switch"
    assert "endpoint" not in devices[relay_id].name
    assert result["indigoDeviceIds"] == [relay_id]        # node device NOT in this list
    assert result["primaryDeviceId"] == relay_id           # untouched — the plug stays primary
    assert devices[node_id].name == "Office Plug"          # bare base, no collision to resolve


def test_node_device_adopts_the_folder_of_its_endpoint_siblings(ds, indigo_env):
    """Reconcile passes no commission-time folder (folder_id 0), so a node
    device created for an EXISTING install would land in the root folder while
    its endpoint siblings sit in a real one — observed on the live rig during
    the #204 validation pass. It must adopt a sibling's folder instead."""
    _indigo, devices = indigo_env
    # Commission WITHOUT node evidence: the relay lands in the "Office" folder
    # and the ADR-0003 gate withholds the node device.
    ds.create_from_raw(RELAY_NODE, "Office Plug", "Office")
    relay_id = ds.lookup(42, 1, "matterRelay")
    assert ds.lookup(42, 0, "matterNode") is None
    assert devices[relay_id].folderId != 0
    # A later reconcile-style pass (non-authoritative: no name, no room)
    # carries the evidence and creates the node device.
    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")
    node_dev = devices[result["nodeDeviceId"]]
    assert node_dev.folderId == devices[relay_id].folderId


def test_node_device_folder_falls_back_to_root_with_no_homed_siblings(ds, indigo_env):
    """No sibling has a real folder (fresh commission with no room): the node
    device stays in root alongside them, and the adoption walk must not raise."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")
    assert devices[result["nodeDeviceId"]].folderId == 0


def test_multi_device_node_creates_node_device_without_touching_endpoint_names(ds, indigo_env):
    """A genuinely multi-device node (4 additive AQ sensors on one endpoint):
    role-suffixed endpoint names, and role_counts/`multi`, must be exactly
    what they were before the node device existed."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(AQ_NODE)
    result = ds.create_from_raw(evidenced, "Air Sensor")
    assert len(result["indigoDeviceIds"]) == 4
    names = {devices[dev_id].name for dev_id in result["indigoDeviceIds"]}
    assert names == {
        "Air Sensor - Air Quality", "Air Sensor - CO₂",
        "Air Sensor - PM2.5", "Air Sensor - TVOC",
    }
    node_id = result["nodeDeviceId"]
    assert node_id is not None
    assert node_id not in result["indigoDeviceIds"]
    assert devices[node_id].name == "Air Sensor"           # bare base, no collision here


def test_node_device_index_entry_at_endpoint_zero(ds, indigo_env):
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    assert ds.lookup(42, 0, "matterNode") == result["nodeDeviceId"]


def test_second_create_devices_pass_is_idempotent_for_the_node_device(ds, indigo_env):
    evidenced = _with_node_evidence(RELAY_NODE)
    first = ds.create_from_raw(evidenced, "Office Plug")
    second = ds.create_from_raw(evidenced, "Office Plug")
    assert first["nodeDeviceId"] == second["nodeDeviceId"]


def test_empty_plan_creates_no_node_device(ds, indigo_env):
    """Issue #105's empty-bridge case: a node with no endpoint devices at all
    has nothing for a node device to be the root of."""
    evidenced = _with_node_evidence(ROOT_ONLY_NODE)
    result = ds.create_from_raw(evidenced, "Root Only")
    assert result["nodeDeviceId"] is None
    assert result["indigoDeviceIds"] == []


def test_sw_version_lands_in_props_and_state(ds, indigo_env):
    """First surfacing anywhere of matter_model.parse_node's sw_version field."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    dev = devices[result["nodeDeviceId"]]
    assert dev.pluginProps["softwareVersion"] == "1.2.3"
    assert dev.states.get("softwareVersion") == "1.2.3"


def test_node_label_primes_from_snapshot_when_present(ds, indigo_env):
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE, node_label="Landing Node")
    result = ds.create_from_raw(evidenced, "Office Plug")
    dev = devices[result["nodeDeviceId"]]
    assert dev.states.get("nodeLabel") == "Landing Node"


def test_node_label_absent_leaves_the_default(ds, indigo_env):
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)  # no 0/40/5 this time
    result = ds.create_from_raw(evidenced, "Office Plug")
    dev = devices[result["nodeDeviceId"]]
    assert dev.states.get("nodeLabel") == 0  # FakeDev's declared-state default


def test_live_node_label_attribute_event_updates_the_node_device(ds, indigo_env):
    """Issue #204 review, fix I.3: a LIVE BasicInformation (0x0028) NodeLabel
    (0x0005) attribute_updated event, through the real ds.handle_event →
    _on_attribute dispatch (not BasicInformationHandler unit-tested in
    isolation — test_handlers.py already covers that) must land on the
    matterNode device."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=42, endpoint=0, cluster=0x0028, attribute=0x0005, value="Landing Node",
    ))
    assert devices[node_dev_id].states.get("nodeLabel") == "Landing Node"


def test_live_software_version_attribute_event_updates_the_node_device(ds, indigo_env):
    """Same as above for SoftwareVersionString (0x000A) (issue #204 review, fix I.3)."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=42, endpoint=0, cluster=0x0028, attribute=0x000A, value="9.9.9",
    ))
    assert devices[node_dev_id].states.get("softwareVersion") == "9.9.9"


def test_node_device_gets_battery_when_node_wide_coverage_includes_endpoint_zero(ds, indigo_env):
    """POWER_SOURCE_NODE_WIDE_NODE (the FP300 shape): PowerSource lives ON
    endpoint 0 with an EMPTY EndpointList — "the whole node" per spec, which
    includes endpoint 0 itself. The #205→#204 join: the node device is just
    another endpoint as far as power coverage is concerned."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE)
    result = ds.create_from_raw(evidenced, "Landing Presence")
    dev = devices[result["nodeDeviceId"]]
    assert dev.pluginProps.get("SupportsBatteryLevel") is True
    assert dev.states.get("batteryLevel") == 80   # BatPercentRemaining 160 // 2


def test_node_device_has_no_battery_when_sources_cover_only_children(ds, indigo_env):
    """BRIDGE_ENDPOINT_LIST_NODE: each PowerSource names only its own child
    endpoint (1, 2) — endpoint 0 is not covered by anything, so the node
    device correctly gets no battery at all. The #205→#204 join's negative
    case."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(BRIDGE_ENDPOINT_LIST_NODE)
    result = ds.create_from_raw(evidenced, "Multi Sensor Bridge")
    dev = devices[result["nodeDeviceId"]]
    assert "SupportsBatteryLevel" not in dev.pluginProps
    assert dev.states.get("batteryLevel") == 0    # FakeDev's declared-state default, never primed


# ===========================================================================
# issue #204 final stage — energy on endpoint 0 (Simon's original ask)
#
# _is_meter_source_candidate no longer excludes endpoint 0. Same two outcomes
# as any other meter-link source: a resolvable target (the reading lands on
# the actuator, no device at all on ep0) or an ambiguous/absent one (issue #79
# ordinarily falls back to a standalone matterEnergyMeter, but ep0 instead
# falls back to the matterNode device's own curEnergyLevel/accumEnergyTotal
# states — it already has a home, so no third/standalone device is created).
# ===========================================================================

EP0_ENERGY_SOLE_ACTUATOR_NODE = {
    "node_id": 0x38,
    "available": True,
    "attributes": {
        "0/144/8": 1200,                    # ElectricalPowerMeasurement.ActivePower, on ep0
        "0/145/1": {"energy": 3_600_000},   # ElectricalEnergyMeasurement, on ep0
        "1/29/0": [{"0": 266}],             # OnOffPlugInUnit
        "1/6/0": False,                     # OnOff
    },
}


def test_ep0_energy_links_to_sole_relay_no_third_device(ds, indigo_env):
    """The structural gap Simon reported: 0x0090/0x0091 on endpoint 0 used to
    be silently dropped (_is_meter_source_candidate excluded ep0 explicitly).
    Now it resolves through the SAME sole-actuator heuristic the GRILLPLATS
    ep2 fixtures already exercise — just sourced from ep0 instead of an
    ordinary application endpoint. No node device exists here (this fixture
    carries no BasicInformation AttributeList evidence, so the ADR-0003 gate
    withholds it) — the point of this test is the link, not the node
    device."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(EP0_ENERGY_SOLE_ACTUATOR_NODE, "Grill Plug")
    assert len(result["indigoDeviceIds"]) == 1
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    assert dev.states.get("curEnergyLevel") == 1.2        # 1200 mW → 1.2 W, primed
    assert dev.states.get("accumEnergyTotal") == 3.6       # 3,600,000 mWh → 3.6 kWh, primed
    assert ds.lookup(0x38, 0) is None, "ep0 must get no device at all when the link succeeds"


EP0_ENERGY_AMBIGUOUS_TWO_RELAY_NODE = {
    "node_id": 0x39,
    "available": True,
    "attributes": {
        "0/144/8": 800,                     # ElectricalPowerMeasurement.ActivePower, on ep0
        "0/145/1": {"energy": 200_000},     # ElectricalEnergyMeasurement, on ep0
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/29/0": [{"0": 266}],
        "2/6/0": False,
    },
}


def test_ep0_energy_ambiguous_two_relay_lands_on_node_device(ds, indigo_env):
    """No PowerTopology to disambiguate two actuator endpoints, so
    attribution is ambiguous (mirrors AMBIGUOUS_MULTI_RELAY_NODE) — but
    unlike an ambiguous source at endpoint >0 (which falls back to a
    standalone matterEnergyMeter, issue #79), ep0 has its own home: the
    matterNode device, evidenced here via _with_node_evidence. Neither relay
    gets meter props, and ep0 itself gets no SEPARATE device — the node
    device, already indexed at (node, 0), is what _lookup_for_cluster's bare
    lookup(node, 0) fallback resolves to."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(EP0_ENERGY_AMBIGUOUS_TWO_RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Power Strip")
    ep1_id = ds.lookup(0x39, 1)
    ep2_id = ds.lookup(0x39, 2)
    assert ep1_id is not None and ep2_id is not None
    assert not devices[ep1_id].pluginProps.get("SupportsPowerMeter")
    assert not devices[ep2_id].pluginProps.get("SupportsPowerMeter")
    node_id = result["nodeDeviceId"]
    assert node_id is not None
    assert ds.lookup(0x39, 0) == node_id, "ep0 resolves to the node device, not a standalone one"
    assert ds.lookup(0x39, 0, "matterEnergyMeter") is None, "no standalone fallback for ep0"
    node_dev = devices[node_id]
    assert node_dev.states.get("curEnergyLevel") == 0.8     # 800 mW → 0.8 W, primed
    assert node_dev.states.get("accumEnergyTotal") == 0.2   # 200,000 mWh → 0.2 kWh, primed


def test_ep0_energy_linked_case_does_not_also_prime_the_node_device(ds, indigo_env):
    """The scenario EP0_ENERGY_SOLE_ACTUATOR_NODE sidesteps by carrying no
    node evidence: a node with BOTH a linkable ep0 energy source AND enough
    BasicInformation evidence for a node device. Without the source-endpoint
    guard in _prime_states, the node device's own-endpoint priming would
    ALSO pick up ep0's 0x0090/0x0091 values — showing the same wattage twice,
    once on the relay (correctly) and once on the node device (a mirror of a
    reading that was just attributed elsewhere). The reading must land on the
    relay only."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(EP0_ENERGY_SOLE_ACTUATOR_NODE)
    result = ds.create_from_raw(evidenced, "Grill Plug")
    relay_id = ds.lookup(0x38, 1)
    node_id = result["nodeDeviceId"]
    assert relay_id is not None and node_id is not None
    assert devices[relay_id].states.get("curEnergyLevel") == 1.2
    assert devices[relay_id].states.get("accumEnergyTotal") == 3.6
    node_dev = devices[node_id]
    assert node_dev.states.get("curEnergyLevel") == 0     # FakeDev's declared-state default
    assert node_dev.states.get("accumEnergyTotal") == 0   # — never primed, the link claimed it
    assert ds.lookup(0x38, 0) == node_id, "ep0 still resolves to the node device by lookup()"


def test_ep0_energy_live_event_reaches_the_node_device_when_unlinkable(ds, indigo_env):
    """Live-routing twin of test_ep0_energy_ambiguous_two_relay_lands_on_node_device:
    an attribute_updated event for (node, 0, 0x0090/0x0091) must reach the
    node device via handle_event, the same production path the GRILLPLATS
    live-routing test exercises for a linked ep2 source."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(EP0_ENERGY_AMBIGUOUS_TWO_RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Power Strip")
    node_id = result["nodeDeviceId"]

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x39, endpoint=0, cluster=0x0090, attribute=0x0008, value=2500,
    ))
    assert devices[node_id].states.get("curEnergyLevel") == 2.5   # 2500 mW → 2.5 W

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x39, endpoint=0, cluster=0x0091, attribute=0x0001,
        value={"energy": 5_000_000},
    ))
    assert devices[node_id].states.get("accumEnergyTotal") == 5.0  # 5,000,000 mWh → 5.0 kWh


# ===========================================================================
# issue #204 review — fix A (CRITICAL): meter links must survive a
# non-informative snapshot, and a live ep-0 event must keep routing to the
# linked relay even once a node device also exists at (node, 0).
# ===========================================================================

def test_ep0_energy_live_event_with_node_device_still_routes_to_relay(ds, indigo_env):
    """Live-routing companion to test_grillplats_live_attribute_event_routes_to_linked_relay,
    but sourced from ep0 WITH a node device also present (via _with_node_evidence)
    — the shape fix A's test 1 asks for. A live (node, 0, 0x0090/0x0091) event
    must still resolve through _lookup_for_cluster's linked-target rewrite to
    the relay, not fall through to the bare lookup(node, 0) fallback just
    because ep0 now also has a device. This is what would catch an `if eid !=
    0` mutation of that rewrite: without it firing for ep0, the bare fallback
    would resolve the event to the node device instead of the relay."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(EP0_ENERGY_SOLE_ACTUATOR_NODE)
    result = ds.create_from_raw(evidenced, "Grill Plug")
    relay_id = ds.lookup(0x38, 1)
    node_id = result["nodeDeviceId"]
    assert relay_id is not None and node_id is not None

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x38, endpoint=0, cluster=0x0090, attribute=0x0008, value=2500,
    ))
    assert devices[relay_id].states.get("curEnergyLevel") == 2.5   # 2500 mW → 2.5 W, routed
    assert devices[node_id].states.get("curEnergyLevel") == 0      # unchanged — link claims it


def test_meter_links_survive_a_non_informative_reconcile_pass(ds, indigo_env):
    """CRITICAL (issue #204 review, fix A): an empty ``attributes: {}``
    snapshot (routine after a matter-server restart — see create_devices'
    ``informative`` docstring) must never retract meter links a prior
    informative pass established. Before the fix, ``_resolve_meter_links`` ran
    unconditionally and CLEARS-then-rebuilds this node's links from
    ``node.endpoints``, which a non-informative pass reports as empty — wiping
    the link. A live ep-0 reading arriving after that wipe would then resolve
    to nothing (or, with a node device present, to the wrong device), and the
    #204 review's own priming guard ensures nothing ever corrects it again."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(EP0_ENERGY_SOLE_ACTUATOR_NODE, "Grill Plug")
    relay_id = ds.lookup(0x38, 1)
    assert relay_id is not None
    assert devices[relay_id].states.get("curEnergyLevel") == 1.2   # primed at creation

    # A non-informative pass — matter-server's cold-cache restart shape.
    ds.reconcile_all([{"node_id": 0x38, "attributes": {}, "available": True}])

    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=0x38, endpoint=0, cluster=0x0090, attribute=0x0008, value=2500,
    ))
    assert devices[relay_id].states.get("curEnergyLevel") == 2.5   # link survived the empty pass
    assert ds.lookup(0x38, 0) is None, "still no device created on ep0"


# ===========================================================================
# issue #204 review — fix B (IMPORTANT): the _prime_states link-source guard
# is scoped to endpoint 0 only — a standalone matterEnergyMeter whose link
# only resolves on a LATER pass must keep receiving fresh readings.
# ===========================================================================

# Pass 1: ep1/ep3 both actuators, no SetTopology — attribution is ambiguous,
# so ep2 falls back to a standalone matterEnergyMeter (mirrors
# AMBIGUOUS_MULTI_RELAY_NODE). Pass 2: ep3 is gone, so ep1 is now the sole
# actuator — the link resolves without recreating ep2's device.
LATER_RESOLVED_LINK_NODE_PASS1 = {
    "node_id": 0x3D,
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

LATER_RESOLVED_LINK_NODE_PASS2 = {
    "node_id": 0x3D,
    "available": True,
    "attributes": {
        "1/29/0": [{"0": 266}],
        "1/6/0": False,
        "2/144/8": 1500,
        "2/145/1": {"energy": 400_000},
    },
}


def test_standalone_meter_re_primes_after_its_link_later_resolves(ds, indigo_env):
    """IMPORTANT (issue #204 review, fix B): a standalone matterEnergyMeter on
    an endpoint whose link only resolves on a LATER pass must keep receiving
    fresh readings via ordinary ``_prime_states`` re-priming. The guard that
    stops a link SOURCE endpoint's own device double-counting a reading
    ``_resolve_meter_links`` already routed elsewhere (issue #204/ADR-0008) is
    scoped to endpoint 0 only, because endpoint 0 is the one endpoint whose
    device (the node device) has a real identity independent of the actuator
    it might link to; every other source endpoint's device exists ONLY
    because no actuator claimed it, so there is no separate identity for its
    own reading to duplicate. Before the fix the guard fired for ANY
    link-source endpoint hosting a device — this repro shape (ep2's link is
    ambiguous on pass 1, resolves on pass 2) is exactly what would freeze such
    a device at its pre-link reading with no diagnostic."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LATER_RESOLVED_LINK_NODE_PASS1, "Power Strip")
    meter_id = ds.lookup(0x3D, 2)
    assert meter_id is not None
    assert devices[meter_id].deviceTypeId == "matterEnergyMeter"
    assert devices[meter_id].states.get("curEnergyLevel") == 0.8   # 800 mW, pass 1

    ds.reconcile_all([LATER_RESOLVED_LINK_NODE_PASS2])

    assert ds.lookup(0x3D, 2) == meter_id, "same device, not recreated"
    assert devices[meter_id].states.get("curEnergyLevel") == 1.5   # 1500 mW, pass 2 — re-primed
    assert devices[meter_id].states.get("accumEnergyTotal") == 0.4  # 400,000 mWh, pass 2


# ===========================================================================
# issue #215 — once a standalone issue-#79 matterEnergyMeter's link resolves,
# PR #213 keeps it updating rather than going stale, but nothing told the
# user the two devices now duplicate each other. Same fixtures as the
# re-priming test above: pass 1 is ambiguous (standalone meter created),
# pass 2 resolves the link without recreating it.
# ===========================================================================

def test_standalone_meter_redundancy_logged_once_when_its_link_resolves(ds, indigo_env, mock_logger):
    """The redundancy INFO fires the pass the link resolves, names the
    standalone device by its actual Indigo name, and — unlike the
    matterUnknown obsolescence log — does NOT repeat on a later pass that
    finds the same already-linked shape. The device itself is never touched;
    deleting it stays the user's call."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LATER_RESOLVED_LINK_NODE_PASS1, "Power Strip")
    meter_id = ds.lookup(0x3D, 2)
    assert meter_id is not None
    meter_name = devices[meter_id].name

    ds.reconcile_all([LATER_RESOLVED_LINK_NODE_PASS2])
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    redundant_msgs = [m for m in info_msgs if "redundant" in m]
    assert len(redundant_msgs) == 1
    assert meter_name in redundant_msgs[0]
    assert devices[meter_id].deviceTypeId == "matterEnergyMeter", "never deleted"
    assert ds.lookup(0x3D, 2) == meter_id, "never deleted"

    # Same already-resolved shape again — must not repeat.
    ds.reconcile_all([LATER_RESOLVED_LINK_NODE_PASS2])
    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    redundant_msgs = [m for m in info_msgs if "redundant" in m]
    assert len(redundant_msgs) == 1, "one-time log, not once-per-pass"


def test_standalone_meter_redundancy_never_logged_while_link_stays_ambiguous(ds, indigo_env, mock_logger):
    """No link ever resolves (pass 1's ambiguous shape repeats) — the
    standalone matterEnergyMeter is the endpoint's only home for the reading,
    so there is nothing redundant to report."""
    _indigo, devices = indigo_env
    ds.create_from_raw(LATER_RESOLVED_LINK_NODE_PASS1, "Power Strip")
    meter_id = ds.lookup(0x3D, 2)
    assert meter_id is not None

    ds.reconcile_all([LATER_RESOLVED_LINK_NODE_PASS1])

    info_msgs = [c[0][0] % tuple(c[0][1:]) for c in mock_logger.info.call_args_list]
    assert not any("redundant" in m for m in info_msgs)
    assert ds.lookup(0x3D, 2) == meter_id, "standalone device still there, still the only home"


# ===========================================================================
# issue #204 review — fix F.1: the endpoint-0 purity exemption
# (_is_meter_source_candidate) needs a fixture that actually carries a
# root-utility cluster NOT in _NON_DEVICE_CLUSTERS, or removing the exemption
# is a no-op against every existing fixture (they all carry only 0x0028,
# which is already in that set).
# ===========================================================================

EP0_ENERGY_SOLE_ACTUATOR_ROOT_UTILITY_NODE = {
    "node_id": 0x3E,
    "available": True,
    "attributes": {
        "0/144/8": 1200,                    # ElectricalPowerMeasurement.ActivePower, on ep0
        "0/145/1": {"energy": 3_600_000},   # ElectricalEnergyMeasurement, on ep0
        "0/48/0": 0,                        # GeneralCommissioning.Breadcrumb — root utility
        "0/62/0": [],                       # OperationalCredentials.NOCs — root utility
        "1/29/0": [{"0": 266}],             # OnOffPlugInUnit
        "1/6/0": False,                     # OnOff
    },
}


def test_ep0_energy_with_root_utility_clusters_still_links_to_relay(ds, indigo_env):
    """Endpoint 0 always carries root utility clusters like GeneralCommissioning
    (0x0030) and OperationalCredentials (0x003E) that are NOT in
    _NON_DEVICE_CLUSTERS — unlike every other _is_meter_source_candidate
    fixture in this file, which happens to carry only 0x0028 (BasicInformation,
    itself IN _NON_DEVICE_CLUSTERS) and so never actually exercises the
    endpoint-0 purity exemption (issue #204 review, fix F.1/9-10): removing
    the ``if int(endpoint.endpoint_id) == 0: return True`` line would still
    pass every other ep0-energy fixture, because none of them carries a
    cluster the purity bar would object to. This one does — without the
    exemption, ep0 stops being a meter-source candidate at all, and the
    relay never gets its meter props or its primed reading."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(EP0_ENERGY_SOLE_ACTUATOR_ROOT_UTILITY_NODE, "Grill Plug")
    assert len(result["indigoDeviceIds"]) == 1
    dev = devices[result["primaryDeviceId"]]
    assert dev.deviceTypeId == "matterRelay"
    assert dev.pluginProps.get("SupportsPowerMeter") is True
    assert dev.pluginProps.get("SupportsEnergyMeter") is True
    assert dev.states.get("curEnergyLevel") == 1.2        # 1200 mW → 1.2 W, primed
    assert dev.states.get("accumEnergyTotal") == 3.6       # 3,600,000 mWh → 3.6 kWh, primed
    assert ds.lookup(0x3E, 0) is None, "ep0 must get no device at all even with root utility clusters"


def test_delete_node_deletes_children_before_the_node_device(ds, indigo_env):
    """The node device is still deleted LAST — not because the ordering
    protects anything any more (ADR-0009: the group is dissolved first, and on
    a fielded install the root is an endpoint device anyway), but because the
    decommission response reads node-device-last and `_self_deleted_ids` is
    easiest to reason about that way. Enforced for real by a delete that
    RAISES if the matterNode device goes while a sibling still exists, rather
    than by recording call order and hoping."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    relay_id = result["indigoDeviceIds"][0]
    assert node_dev_id is not None

    def guarded_delete(dev):
        dev_id = dev.id if hasattr(dev, "id") else dev
        if dev_id == node_dev_id and (devices._by_id.keys() - {node_dev_id}):
            raise AssertionError("matterNode deleted while a sibling device still exists")
        devices._by_id.pop(dev_id, None)

    _indigo.device.delete = guarded_delete
    deleted = ds.delete_node(42)
    assert set(deleted) == {relay_id, node_dev_id}
    assert deleted.index(node_dev_id) > deleted.index(relay_id)


def test_note_node_device_deleted_prevents_recreation(ds, indigo_env):
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    assert node_dev_id is not None

    ds.note_node_device_deleted(42, node_dev_id)
    del devices._by_id[node_dev_id]   # simulate the Indigo-side deletion
    assert ds.lookup(42, 0, "matterNode") is None

    second = ds.create_from_raw(evidenced, "Office Plug")
    assert second["nodeDeviceId"] is None, "a tombstoned node must not be recreated"


def test_tombstone_honoured_through_reconcile_all(ds, indigo_env):
    """Issue #204 review, fix I.5: the literal WS-reconnect path
    (plugin._resync → device_sync.reconcile_all) must honour a tombstone too
    — every prior tombstone test above drives ds.create_from_raw directly,
    which is the commission/node_added path, not the reconnect path."""
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    assert node_dev_id is not None

    ds.note_node_device_deleted(42, node_dev_id)
    del devices._by_id[node_dev_id]   # simulate the Indigo-side deletion

    ds.reconcile_all([evidenced])
    assert ds.lookup(42, 0, "matterNode") is None, \
        "a WS reconnect must not silently recreate a tombstoned node device"


def test_menu_route_back_clears_the_tombstone_and_recreates(ds, indigo_env):
    """The menu item's whole job: clear_all() undoes note_node_device_deleted
    on the next create_devices pass."""
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    tombstones = device_sync_mod.NodeDeviceTombstones()
    ds.node_tombstones = tombstones
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    ds.note_node_device_deleted(42, node_dev_id)
    del devices._by_id[node_dev_id]

    tombstones.clear_all()
    third = ds.create_from_raw(evidenced, "Office Plug")
    assert third["nodeDeviceId"] is not None
    assert third["nodeDeviceId"] != node_dev_id  # a fresh device, new id


def test_forget_node_capabilities_clears_the_tombstone(ds, indigo_env):
    """Node ids are reused across decommission/recommission — a genuinely
    different device commissioned onto a reused id must not be born deleted."""
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    ds.note_node_device_deleted(42, result["nodeDeviceId"])
    assert ds.node_tombstones.is_tombstoned(42)

    ds._forget_node_capabilities(42)
    assert not ds.node_tombstones.is_tombstoned(42)


def test_forget_node_capabilities_prunes_this_nodes_meter_links(ds, indigo_env):
    """Verifier note, issue #204 review: unlike `_power_coverage`, a removed
    node's meter links have no devices behind them to protect, and
    `_resolve_meter_links` does not rebuild on the next informative pass the
    way the setting-limits/attribute-list caches do — it only runs from
    `create_devices`. Without pruning here a stale link would sit in the
    dicts (keyed on a node id matter-server may reuse) until the node is
    recommissioned."""
    ds.create_from_raw(GRILLPLATS_NODE, "Grill Plug")
    assert any(k[0] == 0x34 for k in ds._forward_links)
    assert any(k[0] == 0x34 for k in ds._reverse_links)

    ds.handle_event(MatterEvent(kind=protocol.EVT_NODE_REMOVED, node_id=0x34))

    assert not any(k[0] == 0x34 for k in ds._forward_links)
    assert not any(k[0] == 0x34 for k in ds._reverse_links)


def test_forget_node_capabilities_leaves_other_nodes_links_alone(ds, indigo_env):
    """The prune is per-node — a decommission on one node must not touch a
    different node's live meter links."""
    ds.create_from_raw(GRILLPLATS_NODE, "Grill Plug")
    before_forward = dict(ds._forward_links)
    before_reverse = dict(ds._reverse_links)
    assert before_forward and before_reverse

    ds.handle_event(MatterEvent(kind=protocol.EVT_NODE_REMOVED, node_id=0x999999))

    assert ds._forward_links == before_forward
    assert ds._reverse_links == before_reverse


def test_note_node_device_deleted_with_no_tombstones_object_never_raises(ds, indigo_env):
    """self.node_tombstones is None until plugin.startup wires it — inert,
    same discipline as survey_log."""
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    ds.note_node_device_deleted(42, result["nodeDeviceId"])  # must not raise


def test_delete_node_does_not_tombstone_its_own_matterNode_delete(ds, indigo_env, mock_logger):
    """Issue #204 review, fix A: decommission calls delete_node, which deletes
    the matterNode device itself — Indigo's deviceDeleted callback fires for
    THAT delete exactly as it would for a user's manual delete
    (Plugin Guide: "gets called for every kind of device deletion"). Feeding
    the node device's id back through note_node_device_deleted (the only id
    plugin.deviceDeleted ever routes there — its matterNode type check) must
    NOT tombstone the node or log the misleading "will not be recreated"
    INFO — the decommission already forgot the node, and a recommission onto
    the reused id needs a fresh node device."""
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    assert node_dev_id is not None

    deleted = ds.delete_node(42)
    assert node_dev_id in deleted
    # Only the matterNode id ever reaches note_node_device_deleted — mirror
    # plugin.deviceDeleted's type check rather than looping every id, which
    # would hide a mark that leaked onto endpoint-device ids.
    ds.note_node_device_deleted(42, node_dev_id)

    assert not ds.node_tombstones.is_tombstoned(42)
    assert not any(
        "will not be recreated" in str(call.args)
        for call in mock_logger.info.call_args_list
    )
    # And the mark is consumed: nothing lingers to swallow a LATER genuine
    # user deletion.
    assert not ds._self_deleted_ids


def test_a_failed_self_delete_does_not_swallow_a_later_user_delete(ds, indigo_env, mock_logger):
    """Issue #204 verification review: if delete_node's own delete of the
    matterNode device RAISES (the non-empty-group-root refusal, live once
    grouping lands), the self-deleted mark must be withdrawn — otherwise a
    later genuine user deletion of that surviving device is silently
    swallowed: no tombstone, no INFO, and the next reconcile resurrects a
    device the user deliberately deleted."""
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")
    node_dev_id = result["nodeDeviceId"]

    real_delete = _indigo.device.delete

    def refuse_node_device(dev_or_id):
        dev_id = getattr(dev_or_id, "id", dev_or_id)
        if dev_id == node_dev_id:
            raise ValueError("cannot delete the root of a non-empty group")
        return real_delete(dev_or_id)

    _indigo.device.delete = refuse_node_device
    try:
        deleted = ds.delete_node(42)
    finally:
        _indigo.device.delete = real_delete
    assert node_dev_id not in deleted
    assert node_dev_id not in ds._self_deleted_ids

    # The survivor is later deleted by the USER: the tombstone must land.
    ds.note_node_device_deleted(42, node_dev_id)
    assert ds.node_tombstones.is_tombstoned(42)


def test_node_device_reachable_state_gets_a_display_uivalue_at_creation(ds, indigo_env):
    """Fix D (issue #204 review, Simon's UX finding): matterNode's
    UiDisplayStateId is `reachable` — a bare Boolean YesNo renders as a bare
    "yes" in the device list, read next to an on/off actuator like another
    on/off answer rather than a connectivity flag. Creation must seed the
    uiValue alongside the boolean, not just the boolean."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    assert devices[node_dev_id].states.get("reachable") is True
    assert devices[node_dev_id].states.get("reachable.ui") == "reachable"


def test_mark_unreachable_flags_the_node_device(ds, indigo_env):
    """Issue #204 review, fix E: mark_unreachable is the EVT_NODE_REMOVED
    path (and others) — it never went through _apply_reachability, so the
    node device's own `reachable` state used to latch stale True."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    ds.mark_unreachable(42)
    assert devices[node_dev_id].errorState == "unreachable"
    assert devices[node_dev_id].states.get("reachable") is False
    assert devices[node_dev_id].states.get("reachable.ui") == "unreachable"  # fix D


def test_mark_all_unreachable_flags_every_node_device(ds, indigo_env):
    """Issue #204 review, fix E: a WS drop (mark_all_unreachable) is the most
    common outage this plugin sees — it must not leave matterNode devices'
    `reachable` state stale True."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    ds.mark_all_unreachable()
    assert devices[node_dev_id].errorState == "unreachable"
    assert devices[node_dev_id].states.get("reachable") is False
    assert devices[node_dev_id].states.get("reachable.ui") == "unreachable"  # fix D


def test_reconcile_orphan_sweep_marks_a_non_informative_node_device_unreachable(ds, indigo_env):
    """ADR-0008 correction (issue #204 review, fixes E and G.4): a
    non-informative snapshot (empty attributes → node.endpoints == []) puts
    EVERYTHING for that node in the orphan sweep, including the node device
    at (node_id, 0) — the "orphan sweep never touches the node device" claim
    only holds when the pass is informative. It must be marked unreachable,
    not deleted."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    relay_id = ds.lookup(42, 1, "matterRelay")

    ds.reconcile_all([{"node_id": 42, "attributes": {}, "available": True}])

    assert node_dev_id in devices._by_id, "orphan sweep marks unreachable, never deletes"
    assert devices[node_dev_id].errorState == "unreachable"
    assert devices[node_dev_id].states.get("reachable") is False
    assert devices[relay_id].errorState == "unreachable"


def test_apply_reachability_writes_the_node_reachable_state_both_directions(ds, indigo_env):
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    from matter_model import parse_node

    unavailable = parse_node({**evidenced, "available": False}, "Office Plug")
    ds._apply_reachability(unavailable)
    assert devices[node_dev_id].states.get("reachable") is False
    assert devices[node_dev_id].errorState == "unreachable"
    assert devices[node_dev_id].states.get("reachable.ui") == "unreachable"  # fix D

    available = parse_node({**evidenced, "available": True}, "Office Plug")
    ds._apply_reachability(available)
    assert devices[node_dev_id].states.get("reachable") is True
    assert devices[node_dev_id].errorState == ""
    assert devices[node_dev_id].states.get("reachable.ui") == "reachable"  # fix D — recovery


def test_reconcile_all_never_sweeps_the_node_device_as_an_orphan(ds, indigo_env):
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]
    ds.reconcile_all([evidenced])
    assert devices[node_dev_id].errorState == ""
    assert ds.lookup(42, 0, "matterNode") == node_dev_id


# ---------------------------------------------------------------------------
# NodeDeviceTombstones — the class itself (issue #204)
# ---------------------------------------------------------------------------

def test_tombstones_load_from_json_array():
    import device_sync as device_sync_mod
    t = device_sync_mod.NodeDeviceTombstones()
    t.load('["42", "7"]')
    assert t.is_tombstoned(42)
    assert t.is_tombstoned(7)
    assert not t.is_tombstoned(9)


def test_tombstones_load_to_json_round_trip():
    """Issue #204 review, fix I.4: load(other.to_json()) — the actual shape a
    restart uses (pluginPrefs blob → load), rather than only testing load()
    against a hand-written JSON literal."""
    import device_sync as device_sync_mod
    a = device_sync_mod.NodeDeviceTombstones()
    a.add(1)
    a.add(42)
    b = device_sync_mod.NodeDeviceTombstones()
    b.load(a.to_json())
    assert b.is_tombstoned(1)
    assert b.is_tombstoned(42)
    assert not b.is_tombstoned(2)


def test_tombstones_load_tolerates_garbage():
    import device_sync as device_sync_mod
    t = device_sync_mod.NodeDeviceTombstones()
    t.load("not json at all")
    assert not t.is_tombstoned(42)
    t.load(None)
    assert not t.is_tombstoned(42)
    t.load("")
    assert not t.is_tombstoned(42)


def test_tombstones_add_persists():
    import device_sync as device_sync_mod
    saved = []
    t = device_sync_mod.NodeDeviceTombstones(save=saved.append)
    t.add(42)
    assert saved == ['["42"]']
    assert t.is_tombstoned(42)


def test_tombstones_forget_persists_only_when_present():
    import device_sync as device_sync_mod
    saved = []
    t = device_sync_mod.NodeDeviceTombstones(save=saved.append)
    t.forget(42)
    assert saved == [], "nothing to forget — must not persist a no-op write"
    t.add(42)
    saved.clear()
    t.forget(42)
    assert saved == ["[]"]
    assert not t.is_tombstoned(42)


def test_tombstones_clear_all_persists_only_when_something_was_cleared():
    import device_sync as device_sync_mod
    saved = []
    t = device_sync_mod.NodeDeviceTombstones(save=saved.append)
    assert t.clear_all() == 0
    assert saved == [], "nothing tombstoned — must not persist a no-op write"
    t.add(1)
    t.add(2)
    saved.clear()
    # Fix B (issue #204 review): the count is what lets the menu handler skip
    # a pointless reconcile and report an honest number.
    assert t.clear_all() == 2
    assert saved == ["[]"]
    assert not t.is_tombstoned(1) and not t.is_tombstoned(2)


def test_tombstones_persist_failure_never_raises():
    import device_sync as device_sync_mod
    def boom(_blob):
        raise RuntimeError("prefs are read-only")
    t = device_sync_mod.NodeDeviceTombstones(save=boom)
    t.add(42)  # must not raise
    assert t.is_tombstoned(42)


def test_tombstones_persist_failure_logs_a_loud_warning(mock_logger):
    """Issue #204 review, fix C: unlike SurveyLog's cosmetic repeat, a failed
    tombstone save means a deliberately-deleted node device can be
    resurrected after a restart — the failure must be loud, not silent."""
    import device_sync as device_sync_mod
    def boom(_blob):
        raise RuntimeError("prefs are read-only")
    t = device_sync_mod.NodeDeviceTombstones(save=boom, logger=mock_logger)
    t.add(42)  # must not raise
    assert mock_logger.warning.called
    msg = " ".join(str(a) for a in mock_logger.warning.call_args.args)
    assert "may be recreated" in msg


def test_tombstones_persist_failure_without_logger_still_never_raises():
    """logger is optional (constructor default None) — same discipline as
    save=None; a test/path that hasn't wired one gets silence, not a crash."""
    import device_sync as device_sync_mod
    def boom(_blob):
        raise RuntimeError("prefs are read-only")
    t = device_sync_mod.NodeDeviceTombstones(save=boom)
    t.add(42)  # must not raise even with no logger wired
    assert t.is_tombstoned(42)


# ===========================================================================
# issue #204 stage 2 / ADR-0008 option B, as narrowed by ADR-0009 — grouping
# + naming/folder true-up
#
# A node's devices end up in ONE Indigo device group with the node device;
# every endpoint device takes a role suffix (including a single-endpoint node's
# one device); and a fielded install is migrated to that shape by the ordinary
# reconcile path — idempotently, and without ever renaming a device a human
# named.
#
# MEMBERSHIP is what the plugin delivers. Indigo roots a group at its OLDEST
# member and ignores groupWithDevice's argument order (controlled experiment,
# jarvis 2026-08-12 — ADR-0009), so the node device roots the group only where
# it is the oldest: a FRESH commission, where create_devices creates it first.
# Fielded installs lead with their oldest endpoint device, and that is fine.
# ===========================================================================

def _grouped(indigo_module, dev_id):
    """``dev_id``'s group as a SET — what the plugin actually guarantees."""
    return set(indigo_module.device.getGroupList(dev_id))


# --- the group model itself, pinned to the experiment -----------------------

def test_group_membership_is_age_ordered_and_ignores_argument_order(indigo_env):
    """The controlled experiment (jarvis 2026-08-12), reproduced against the
    fake so that every grouping test below rests on the real semantics:

        groupWithDevice(motion, node) -> [motion, node]
        groupWithDevice(node, motion) -> [motion, node]   (IDENTICAL)
        then groupWithDevice(temp, node) -> [motion, temp, node]

    Members come back oldest-first, the oldest is the root, and swapping the
    arguments changes nothing. A plugin that steers the root by argument order
    is fighting something that does not exist."""
    _indigo, _devices = indigo_env
    motion = _indigo.device.create(deviceTypeId="other", name="motion")
    temp = _indigo.device.create(deviceTypeId="other", name="temp")
    node = _indigo.device.create(deviceTypeId="other", name="node")   # youngest

    _indigo.device.groupWithDevice(motion.id, node.id)
    one_way = _indigo.device.getGroupList(node.id)
    _indigo.device.ungroupDevice(motion.id)
    _indigo.device.groupWithDevice(node.id, motion.id)
    other_way = _indigo.device.getGroupList(node.id)

    assert one_way == other_way == [motion.id, node.id]
    # ...and the answer is the same asked from either end.
    assert _indigo.device.getGroupList(motion.id) == one_way

    _indigo.device.groupWithDevice(temp.id, node.id)

    # Accumulates, and the root does not move.
    assert _indigo.device.getGroupList(temp.id) == [motion.id, temp.id, node.id]


# --- the pure name grammar --------------------------------------------------

def test_generated_base_parses_only_this_plugins_own_suffix_forms():
    import device_sync as device_sync_mod
    parse = device_sync_mod._generated_base
    assert parse("Office Plug - Switch", "Switch") == "Office Plug"
    assert parse("Patio - Switch 2", "Switch") == "Patio"          # identical siblings
    assert parse("Weird Thing (endpoint 3)", "") == "Weird Thing"  # unmapped type fallback
    # A role that is not THIS device's own role is a hand-rename that merely
    # looks generated — it must not vote (a matterRelay named "- Motion").
    assert parse("Lamp - Motion", "Switch") is None
    assert parse("Desk lamp", "Switch") is None                    # bare: no evidence either way
    assert parse(None, "Switch") is None


def test_is_generated_name_covers_every_form_including_the_unique_suffix():
    import device_sync as device_sync_mod
    generated = device_sync_mod._is_generated_name
    assert generated("Office Plug", "Office Plug", "Switch")
    assert generated("Office Plug 2", "Office Plug", "Switch")     # _unique_name collision
    assert generated("Office Plug - Switch", "Office Plug", "Switch")
    assert generated("Office Plug - Switch 3", "Office Plug", "Switch")
    assert generated("Office Plug (endpoint 1)", "Office Plug", "Switch")
    assert not generated("Desk lamp", "Office Plug", "Switch")
    assert not generated("Office Plug - Motion", "Office Plug", "Switch")
    assert not generated("Office Plug", "", "Switch")              # no base, no verdict


# --- fresh commissions ------------------------------------------------------

def test_fresh_single_endpoint_commission_suffixes_and_groups(ds, indigo_env):
    """The headline shape: the node device holds the bare name and roots the
    group; the one endpoint device is named by its role.

    It roots the group for exactly one reason (ADR-0009): create_devices
    creates it BEFORE the endpoint loop, so it is the oldest member and
    Indigo's age ordering puts it first. Asserted here as an id comparison
    against creation order, so that moving the call back after the loop fails
    this test rather than silently demoting the node device."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")
    relay_id = ds.lookup(42, 1, "matterRelay")
    node_dev_id = result["nodeDeviceId"]
    assert devices[relay_id].name == "Office Plug - Switch"
    assert devices[node_dev_id].name == "Office Plug"
    assert node_dev_id < relay_id, "the node device must be created first"
    assert _grouped(_indigo, relay_id) == {node_dev_id, relay_id}
    assert _indigo.device.getGroupList(relay_id)[0] == node_dev_id


def test_fresh_multi_endpoint_commission_groups_every_endpoint_device(ds, indigo_env):
    """Multi-endpoint role suffixes are unchanged by stage 2 — all four AQ
    devices keep their names AND all four join the node device's group."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(AQ_NODE), "Air Sensor")
    node_dev_id = result["nodeDeviceId"]
    assert {devices[d].name for d in result["indigoDeviceIds"]} == {
        "Air Sensor - Air Quality", "Air Sensor - CO₂",
        "Air Sensor - PM2.5", "Air Sensor - TVOC",
    }
    assert devices[node_dev_id].name == "Air Sensor"
    for dev_id in result["indigoDeviceIds"]:
        # One group, everyone in it, rooted at the node device because it is
        # the oldest member (ADR-0009 — a fresh commission is the only case
        # where the plugin gets to decide that).
        assert _grouped(_indigo, dev_id) == {node_dev_id, *result["indigoDeviceIds"]}
        assert _indigo.device.getGroupList(dev_id)[0] == node_dev_id


def test_placeholder_devices_group_under_the_node_device_like_any_sibling(ds, indigo_env):
    """matterUnknown carries a role label ("Unsupported") and is grouped like
    every other device on the node — nothing about it is special here."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(UNKNOWN_NODE), "Robot")
    placeholder_id = ds.lookup(0x50, 1, "matterUnknown")
    assert devices[placeholder_id].name == "Robot - Unsupported"
    assert _indigo.device.getGroupList(placeholder_id)[0] == result["nodeDeviceId"]


def test_energy_meter_fallback_device_groups_under_the_node_device(ds, indigo_env):
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(ELECTRICAL_ONLY_NODE), "Mains")
    meter_id = ds.lookup(0x52, 1, "matterEnergyMeter")
    assert _indigo.device.getGroupList(meter_id)[0] == result["nodeDeviceId"]


# --- the fielded-install migration -----------------------------------------

def _fielded_install(ds, indigo_env, base="Kitchen sensor"):
    """Reconstruct what a 2026.13.0 install of the FP300-shaped node looks like
    on disk, then hand back to the caller for ONE migrating reconcile pass.

    Assembled by driving the real code and then mangling the result into the
    older shape, rather than by hand-building devices: the endpoint devices
    really were created by a commission pass (role suffixes, a real folder),
    and the three defects stage 2 heals are then applied exactly as 2026.13.0
    left them —

    * the node device named from the PRODUCT name, because a reconcile
      snapshot has no ``suggested_name`` (live finding), with ``nodeBaseName``
      stamped to match, so the prop is wrong on every fielded install;
    * the node device in folder 0, because folder adoption landed with the
      creation path and the devices created before it never got it;
    * nothing grouped at all — stage 1 grouped nothing, by design.
    """
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_NODE_WIDE_NODE, base, "Kitchen")
    temp_id = ds.lookup(0x63, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x63, 2, "matterHumiditySensor")
    result = ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
    node_dev_id = result["nodeDeviceId"]
    node_dev = devices[node_dev_id]
    node_dev.name = "FP300"                                        # the product name
    node_dev._name_on_server = "FP300"   # the mangled name IS the on-disk state
    node_dev.pluginProps = dict(node_dev.pluginProps, nodeBaseName="FP300")
    node_dev.folderId = 0
    _indigo.device.groups.clear()
    _indigo.device.group_calls.clear()
    return node_dev_id, temp_id, hum_id


def test_migration_pass_groups_renames_adopts_folder_and_restamps(ds, indigo_env):
    """One ordinary reconcile pass over a 2026.13.0 install fixes all four
    defects at once: grouping, the node device's name, its nodeBaseName prop,
    and its folder.

    Grouping means MEMBERSHIP here, not rooting (ADR-0009): this node's
    endpoint devices predate its node device, so Indigo's age ordering makes
    the OLDEST endpoint device the root and no argument order or ungroup
    dance can change that. The family shows together in the device list, which
    is what the user gets out of it."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert devices[node_dev_id].name == "Kitchen sensor"
    assert devices[node_dev_id].pluginProps["nodeBaseName"] == "Kitchen sensor"
    assert devices[node_dev_id].folderId == devices[temp_id].folderId != 0
    assert devices[temp_id].name == "Kitchen sensor - Temperature"   # already canonical
    assert devices[hum_id].name == "Kitchen sensor - Humidity"
    family = {node_dev_id, temp_id, hum_id}
    for dev_id in family:
        assert _grouped(_indigo, dev_id) == family
        # The oldest device leads, and here that is an endpoint device.
        assert _indigo.device.getGroupList(dev_id)[0] == min(family) == temp_id


def test_second_migration_pass_does_nothing_at_all(ds, indigo_env, mock_logger):
    """Idempotence, counted rather than inferred: the pass after the migrating
    one must make ZERO grouping calls, change no name, and say NOTHING. The
    silence is load-bearing — a settled install reconciles on every reconnect,
    so anything said here is said forever."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
    before = {d.id: d.name for d in devices}
    _indigo.device.group_calls.clear()
    for dev in devices:
        dev.replaced_props = False
    mock_logger.warning.reset_mock()

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert _indigo.device.group_calls == []
    assert {d.id: d.name for d in devices} == before
    assert not any(dev.replaced_props for dev in devices), \
        "a settled node device must not be restamped (a props write costs a comm restart)"
    # Kills the mutation that drops _ensure_grouped's already-grouped
    # short-circuit: re-grouping an already-grouped device warns rather than
    # crashing, so the name/props assertions above would all still pass.
    assert mock_logger.warning.call_args_list == []


def test_migration_renames_a_bare_product_named_endpoint_device(ds, indigo_env):
    """The pre-stage-2 single-endpoint form: one device wearing the bare
    PRODUCT name, which is provably a name this plugin wrote."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "")                 # non-authoritative: product name
    relay_id = ds.lookup(42, 1, "matterRelay")
    devices[relay_id].name = "Tapo P125M"              # the pre-stage-2 bare form

    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")

    assert devices[relay_id].name == "Tapo P125M - Switch"
    # In ONE pass, even though the node device is now created before the
    # endpoint loop and was therefore born "Tapo P125M 2" while the relay
    # still held the bare name: the post-loop true-up re-runs once the bare
    # name is free (ADR-0009's cost, paid at the call site).
    assert devices[result["nodeDeviceId"]].name == "Tapo P125M"
    # Membership, not rooting — the relay predates the node device.
    assert _grouped(_indigo, relay_id) == {relay_id, result["nodeDeviceId"]}
    assert _indigo.device.getGroupList(relay_id)[0] == relay_id


def test_a_bare_user_named_single_endpoint_device_is_left_alone(ds, indigo_env):
    """The deliberate limitation, stated as a test: on a node whose ONLY
    device wears a bare name that is neither the product name nor a suffixed
    form, "the user renamed it" and "this plugin named it at commission time"
    are indistinguishable — so nothing is renamed, and the node device falls
    back to the product name. Never renaming a hand-renamed device wins."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "")
    relay_id = ds.lookup(42, 1, "matterRelay")
    devices[relay_id].name = "Desk lamp"

    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")

    assert devices[relay_id].name == "Desk lamp"
    # ...and it is still grouped: grouping is not conditional on naming.
    assert _grouped(_indigo, relay_id) == {relay_id, result["nodeDeviceId"]}


def test_hand_renamed_endpoint_device_is_never_renamed(ds, indigo_env):
    """One child of a family renamed by hand: its siblings still derive the
    family base and are trued up, and it is not touched."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    devices[hum_id].name = "Desk lamp"
    devices[temp_id].name = "Kitchen sensor - Temperature 9"   # a generated form

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert devices[hum_id].name == "Desk lamp"
    assert devices[temp_id].name == "Kitchen sensor - Temperature"
    assert devices[node_dev_id].name == "Kitchen sensor"


def test_a_node_with_no_derivable_base_renames_nothing_and_logs_debug(ds, indigo_env, mock_logger):
    """Two siblings whose generated-looking names disagree: there is no single
    family base, so every name — including the node device's — is left alone.

    There is deliberately no separate "majority wins" test: the votes are
    collected into a SET, so N devices agreeing on "Alpha" and one saying
    "Beta" is the same two-element set as one-vs-one. Any N-vs-1 case IS this
    case by construction."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    devices[temp_id].name = "Alpha - Temperature"
    devices[hum_id].name = "Beta - Humidity"
    mock_logger.debug.reset_mock()

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert devices[temp_id].name == "Alpha - Temperature"
    assert devices[hum_id].name == "Beta - Humidity"
    assert devices[node_dev_id].name == "FP300"
    assert devices[node_dev_id].pluginProps["nodeBaseName"] == "FP300"
    assert any("no single family base" in str(call.args)
               for call in mock_logger.debug.call_args_list)
    # The folder heal is independent of any name evidence and still happens.
    assert devices[node_dev_id].folderId == devices[temp_id].folderId != 0


def test_recreated_endpoint_device_takes_the_derived_family_base(ds, indigo_env):
    """The live ALPSTUGA regression: a device deleted by hand comes back on the
    next reconcile named from the PRODUCT name ("FP300 - Humidity") beside
    siblings called "Kitchen sensor - …", because a reconcile snapshot carries
    no suggested_name. It must come back into the family instead."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
    # The user deletes a GROUPED endpoint device in the UI. Indigo is assumed to
    # ungroup-and-delete (undocumented — see the fake's delete): a non-root
    # member simply goes, and the group survives without it.
    _indigo.device.delete(devices[hum_id])
    assert _grouped(_indigo, temp_id) == {temp_id, node_dev_id}

    ds.reconcile_all([_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE)])

    recreated = ds.lookup(0x63, 2, "matterHumiditySensor")
    assert recreated is not None and recreated != hum_id
    assert devices[recreated].name == "Kitchen sensor - Humidity"
    # It rejoins the family's existing group by accumulation — the third line
    # of the ADR-0009 experiment, and the reason no ordering rule is needed.
    assert _grouped(_indigo, recreated) == {temp_id, node_dev_id, recreated}


def test_a_taken_canonical_name_never_starts_a_rename_ping_pong(ds, indigo_env):
    """issue #204 review, fix A. When the canonical name belongs to something
    else — a foreign device, a second node of the same product — _apply_identity
    substitutes "<target> 2", and _is_generated_name's "{base} \\d+" arm re-admits
    THAT on the next pass, whose substitute is "<target> 3"… one real rename per
    reconcile, for as long as the plugin runs. Counted, not inferred: neither
    pass may rename anything at all."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    # A generated form, so the hand-rename gate lets the true-up through...
    devices[hum_id].name = "Kitchen sensor - Humidity 2"
    # ...but the name it wants is a non-Matter device's.
    _indigo.device.create(deviceTypeId="other", name="Kitchen sensor - Humidity")

    snapshots = []
    for _pass in range(2):
        for dev in devices:
            dev.replaced = False
        ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
        snapshots.append({d.id: d.name for d in devices})
        assert not devices[hum_id].replaced, "no rename may even be attempted"

    assert snapshots[0][hum_id] == "Kitchen sensor - Humidity 2"
    assert snapshots[0] == snapshots[1]


def test_a_refused_rename_never_advances_nodeBaseName(ds, indigo_env, mock_logger):
    """issue #204 review, fix B. _apply_identity catches and warns its own
    failures, so a refused replaceOnServer used to leave the node device wearing
    the old name with nodeBaseName already stamped to the new base — after which
    _is_generated_name never matches again and the device is stuck under the
    wrong name permanently, silently. The prop must follow the rename, not
    precede it, so that a later pass can retry."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    node_dev = devices[node_dev_id]

    # fail_replace gives real-Indigo semantics (discard-on-failure) from the
    # fake itself — no per-test compensation stub to forget.
    node_dev.fail_replace = True

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert node_dev.name == "FP300"
    assert node_dev.pluginProps["nodeBaseName"] == "FP300"
    # ...and the failure is said ONCE across both passes (fix E): a true-up
    # repeats on every reconnect, and the grouping side's dedup exists for
    # exactly this.
    assert sum("could not rename device" in str(call.args)
               for call in mock_logger.warning.call_args_list) == 1

    node_dev.fail_replace = False           # the server heals

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert node_dev.name == "Kitchen sensor"
    assert node_dev.pluginProps["nodeBaseName"] == "Kitchen sensor"


def test_a_hand_renamed_node_device_is_left_alone_and_not_restamped(ds, indigo_env):
    """A node device the user renamed is like any other hand-renamed device —
    and its nodeBaseName is not quietly restamped behind it either: a props
    write costs the device a comm restart, and the rename that prop exists to
    unlock is the very thing being declined. Folder adoption is independent
    evidence and still runs."""
    _indigo, devices = indigo_env
    node_dev_id, temp_id, hum_id = _fielded_install(ds, indigo_env)
    devices[node_dev_id].name = "My Renamed Hub"
    devices[node_dev_id].replaced_props = False

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    assert devices[node_dev_id].name == "My Renamed Hub"
    assert devices[node_dev_id].pluginProps["nodeBaseName"] == "FP300"
    assert not devices[node_dev_id].replaced_props
    assert devices[node_dev_id].folderId == devices[temp_id].folderId != 0
    # The siblings' own true-up is unaffected — the family base is theirs.
    assert devices[temp_id].name == "Kitchen sensor - Temperature"


# --- grouping mechanics -----------------------------------------------------

def test_a_user_built_group_is_left_alone_and_warned_once(ds, indigo_env, mock_logger):
    """A group the user built — one this node's node device is not part of —
    is never re-parented, and the warning about it does not nag once per
    reconnect.

    The test that the plugin has left it alone is MEMBERSHIP, not the root:
    under age ordering the root of a user-built group is often the plugin's
    own device (it is the older one here), which is exactly why the warning
    must not name a root at all.

    Also the family-aware branch's negative case: ``foreign`` is NOT one of
    this node's own device ids, so ``{relay_id, foreign.id}`` is not a
    subset of ``family`` and this must still hit the foreign warn-and-leave,
    not the new join-the-family path added alongside it."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "")
    relay_id = ds.lookup(42, 1, "matterRelay")
    foreign = _indigo.device.create(deviceTypeId="other", name="Someone else's device")
    _indigo.device.groupWithDevice(relay_id, foreign.id)   # the user groups them
    _indigo.device.group_calls.clear()

    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")
    ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")

    assert _grouped(_indigo, relay_id) == {relay_id, foreign.id}
    assert result["nodeDeviceId"] not in _grouped(_indigo, relay_id)
    # No grouping call was even attempted for it.
    assert _indigo.device.group_calls == []
    assert _indigo.device.ungroup_calls == []
    messages = [str(call.args) for call in mock_logger.warning.call_args_list]
    assert sum("already in a device group" in message for message in messages) == 1
    # ...and it never says "rooted at 1001" about device 1001 (ADR-0009).
    assert not any("rooted at" in message for message in messages)


def test_an_empty_group_list_is_treated_as_ungrouped(ds, indigo_env):
    """Indigo's answer for an UNGROUPED device is undocumented — [dev_id] is
    what the fake models, but an empty list must not be mistaken for a group
    the plugin has to leave alone."""
    _indigo, devices = indigo_env
    real_get = _indigo.device.getGroupList
    calls = {"n": 0}

    def empty_then_real(dev_or_id):
        calls["n"] += 1
        return [] if calls["n"] == 1 else real_get(dev_or_id)

    _indigo.device.getGroupList = empty_then_real
    try:
        result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")
    finally:
        _indigo.device.getGroupList = real_get
    relay_id = ds.lookup(42, 1, "matterRelay")
    assert _indigo.device.getGroupList(relay_id)[0] == result["nodeDeviceId"]


def test_a_partial_group_heals_by_accumulation_whatever_roots_it(ds, indigo_env, mock_logger):
    """The state every 2026.13.1 install was actually left in, and the reason
    ADR-0009 needs no ordering rule and no ungroup dance: the node device is
    in a two-member group with ONE of its children, rooted at whichever of
    them is older, and the rest of the family is ungrouped.

    One reconcile pass must simply add the missing members to that group —
    the third line of the experiment — leaving one group of N+1, silently,
    without ungrouping anything."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "Kitchen sensor")
    node_dev_id = result["nodeDeviceId"]
    temp_id = ds.lookup(0x63, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x63, 2, "matterHumiditySensor")
    # The leftover: node device + the LAST child only.
    _indigo.device.groups.clear()
    _indigo.device.groupWithDevice(node_dev_id, hum_id)
    _indigo.device.ungroup_calls.clear()
    mock_logger.warning.reset_mock()

    ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")

    family = {node_dev_id, temp_id, hum_id}
    for dev_id in family:
        assert _grouped(_indigo, dev_id) == family
    assert _indigo.device.ungroup_calls == [], "healing must not tear anything down"
    assert mock_logger.warning.call_args_list == []


def test_a_child_rooted_group_is_recognised_as_done_not_as_a_defect(
        ds, indigo_env, mock_logger):
    """The membership short-circuit, pinned against the shape the old code
    called a "misroot": a {child, node_dev} pair whose ROOT is the child.
    That is not a defect to fix — it is simply what Indigo does when the child
    is the older device, which on a fielded install it always is (ADR-0009) —
    so later passes must read it, find the node device already a member, and
    do nothing at all."""
    _indigo, devices = indigo_env
    ds.create_from_raw(RELAY_NODE, "")               # the relay exists first...
    relay_id = ds.lookup(42, 1, "matterRelay")
    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")   # ...node device joins it
    node_dev_id = result["nodeDeviceId"]
    assert _indigo.device.getGroupList(relay_id) == [relay_id, node_dev_id], \
        "sanity: the older endpoint device roots this group"
    _indigo.device.group_calls.clear()
    mock_logger.warning.reset_mock()

    ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")
    ds.create_from_raw(_with_node_evidence(RELAY_NODE), "")

    assert _indigo.device.getGroupList(relay_id) == [relay_id, node_dev_id]
    assert _indigo.device.group_calls == []
    assert _indigo.device.ungroup_calls == []
    assert mock_logger.warning.call_args_list == []


def test_grouping_n_children_sequentially_leaves_one_group_with_every_member(
        ds, indigo_env):
    """The regression the OLD fake's asymmetric merge made impossible to fail:
    on live jarvis each successive join accumulated into the last-joined
    child's group and silently dropped the previous child, so a 5-device node
    ended up as one pair rather than one group of N+1 (2026-08-12). Every
    member must share ONE group — and on a fresh commission the node device
    leads it, being the oldest."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(AQ_NODE), "Air Sensor")
    node_dev_id = result["nodeDeviceId"]
    children = result["indigoDeviceIds"]
    expected = {node_dev_id, *children}
    for dev_id in children:
        group = _indigo.device.getGroupList(dev_id)
        assert set(group) == expected
        assert group[0] == node_dev_id


def test_recreated_node_device_rejoins_its_surviving_family_group(ds, indigo_env, mock_logger):
    """The reviewer's reproduction: a node device deleted by hand and
    recreated via the "Recreate Matter node devices…" menu could never
    rejoin its own family before this fix. Real Indigo drops the deleted
    device OUT of its shared group rather than dissolving the whole group,
    so the node's own endpoint devices stay grouped with each other after
    the delete. The old, non-family-aware `_ensure_grouped` read that
    surviving group — every id its own — as a stranger's arrangement and
    warned-and-left it forever, because nothing in it was the (new) node
    device.

    Fielded-install shape deliberately (children created before the node
    device exists at all, same as `_with_node_evidence` arriving on a later
    pass elsewhere in this file): that makes an endpoint device the group's
    root under ADR-0009's age ordering, not the node device, so the
    hand-delete below is a plain non-root removal — exactly what a user
    clicking "Delete" on the node device in the Indigo UI does, and not the
    root-of-a-non-empty-group refusal `delete_node` has to dissolve around."""
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    result = ds.create_from_raw(AQ_NODE, "Air Sensor")   # children only: no node device yet
    children = set(result["indigoDeviceIds"])
    result_with_node = ds.create_from_raw(_with_node_evidence(AQ_NODE), "")
    node_dev_id = result_with_node["nodeDeviceId"]
    assert node_dev_id is not None
    expected = children | {node_dev_id}
    assert _grouped(_indigo, node_dev_id) == expected
    assert node_dev_id != min(expected), \
        "sanity: the node device is younger than its siblings, so it does not root the group"

    # The hand-delete: tombstone it (as plugin.deviceDeleted would), then
    # delete it through the fake's real delete() — NOT a raw dict pop — so
    # the group model behaves the way live Indigo does and drops the node
    # device out of the shared group rather than dissolving it.
    ds.note_node_device_deleted(30, node_dev_id)
    _indigo.device.delete(devices[node_dev_id])
    sample_child = next(iter(children))
    assert _grouped(_indigo, sample_child) == children, \
        "sanity: the family's own group survives the node device's delete"

    # The menu route back, then the reconcile it triggers.
    ds.node_tombstones.clear_all()
    result2 = ds.create_from_raw(_with_node_evidence(AQ_NODE), "")
    new_node_dev_id = result2["nodeDeviceId"]
    assert new_node_dev_id is not None
    assert new_node_dev_id != node_dev_id, "a fresh device, new id"

    expected = children | {new_node_dev_id}
    for dev_id in expected:
        assert _grouped(_indigo, dev_id) == expected
    assert mock_logger.warning.call_args_list == []


def test_two_siblings_pregrouped_without_a_node_device_are_joined_not_warned(
        ds, indigo_env, mock_logger):
    """A leftover arrangement: two of this node's OWN endpoint devices are
    already grouped with each other, but the node device was never part of
    it (predates ADR-0008, or the user's own tidying). Every id in that
    group still belongs to this node, so one reconcile pass must join the
    node device into it by ACCUMULATION — not read it as a foreign
    arrangement and warn-and-leave, and not tear anything down to do it."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(AQ_NODE, "Air Sensor")   # no evidence: no node device yet
    children = result["indigoDeviceIds"]
    sib_a, sib_b = children[0], children[1]
    _indigo.device.groupWithDevice(sib_a, sib_b)   # the leftover: two siblings, no node device
    _indigo.device.group_calls.clear()
    _indigo.device.ungroup_calls.clear()

    result2 = ds.create_from_raw(_with_node_evidence(AQ_NODE), "")
    node_dev_id = result2["nodeDeviceId"]
    assert node_dev_id is not None

    expected = set(children) | {node_dev_id}
    for dev_id in expected:
        assert _grouped(_indigo, dev_id) == expected
    assert _indigo.device.ungroup_calls == [], "healing must not tear anything down"
    assert mock_logger.warning.call_args_list == []


def test_an_endpoint_device_deleted_by_hand_is_not_a_warning(ds, indigo_env, mock_logger):
    """issue #204 review, fix D. plugin.deviceDeleted only prunes matterNode
    ids, so a hand-deleted ENDPOINT device leaves a dead id in the index.
    This test pins the EXISTENCE PRE-CHECK in the to_group collection: the
    dead id is skipped before _ensure_grouped ever runs, so no getGroupList
    read is attempted for it and nothing warns. (The getGroupList-failure
    demotion itself is pinned separately by the next test — a refactor
    removing either half fails one of the pair.)"""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(AQ_NODE), "Air Sensor")
    victim = result["indigoDeviceIds"][0]
    _indigo.device.delete(devices[victim])
    mock_logger.warning.reset_mock()

    ds.create_from_raw(_with_node_evidence(AQ_NODE), "")

    assert mock_logger.warning.call_args_list == []


def test_an_unreadable_device_group_is_debug_not_a_warning(ds, indigo_env, mock_logger):
    """The read failure itself is demoted as well (fix D). The identical
    condition in _derive_family_base has always been debug, and "Indigo would
    not tell me this device's group" is not something a user can act on."""
    _indigo, devices = indigo_env

    def boom(_dev_or_id):
        raise RuntimeError("no such device")
    _indigo.device.getGroupList = boom
    mock_logger.warning.reset_mock()

    ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")

    assert mock_logger.warning.call_args_list == []
    assert any("could not read the device group" in str(call.args)
               for call in mock_logger.debug.call_args_list)


def test_a_grouping_failure_never_sinks_the_pass(ds, indigo_env, mock_logger):
    """The docs warn twice against grouping while a device or device-factory
    dialog is open, and a plugin cannot detect either — so a refusal has to be
    a log line, not an exception out of create_devices."""
    _indigo, devices = indigo_env

    def boom(_a, _b):
        raise RuntimeError("a device dialog is open")
    _indigo.device.groupWithDevice = boom

    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")
    assert result["nodeDeviceId"] is not None
    assert ds.lookup(42, 1, "matterRelay") is not None
    assert any("could not group device" in str(call.args)
               for call in mock_logger.warning.call_args_list)


def test_the_read_back_verifies_membership_and_says_so_when_it_is_missing(
        ds, indigo_env, mock_logger):
    """The result is read back rather than assumed — but what is checked is
    MEMBERSHIP, not position (ADR-0009). A groupWithDevice that reports
    success and groups nothing is the one outcome left that a user can act
    on, so it is said out loud."""
    _indigo, devices = indigo_env
    _indigo.device.groupWithDevice = lambda _a, _b: None    # succeeds, does nothing

    ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")

    assert any("did not put them in one group" in str(call.args)
               for call in mock_logger.warning.call_args_list)


def test_the_two_grouping_warnings_dedup_independently(ds, indigo_env, mock_logger):
    """issue #218: the groupWithDevice-raised failure and the read-back
    membership failure used to share the "group" dedup key, so whichever
    fired first for a device silenced the other for the rest of the run —
    a device dialog left open early in a pass could hide a genuine
    read-back mismatch discovered later for the same device. They now
    dedup as "group" and "verify" respectively, so both can speak for the
    same device in one run."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)

    def boom(_a, _b):
        raise RuntimeError("a device dialog is open")
    _indigo.device.groupWithDevice = boom

    ds.create_from_raw(evidenced, "Office Plug")
    assert any("could not group device" in str(call.args)
               for call in mock_logger.warning.call_args_list)

    _indigo.device.groupWithDevice = lambda _a, _b: None    # succeeds, groups nothing
    ds.reconcile_all([evidenced])   # _ensure_grouped re-runs on every reconcile

    assert any("could not group device" in str(call.args)
               for call in mock_logger.warning.call_args_list)
    assert any("did not put them in one group" in str(call.args)
               for call in mock_logger.warning.call_args_list)


def test_grouping_leaves_the_index_and_the_orphan_sweep_alone(ds, indigo_env):
    """Grouping is Indigo-side metadata, not index state: a rebuild from
    pluginProps must find exactly the same devices, and a healthy reconcile
    must still clear rather than orphan them."""
    _indigo, devices = indigo_env
    evidenced = _with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE)
    result = ds.create_from_raw(evidenced, "Kitchen sensor")
    before = dict(ds._index)

    ds.rebuild_index()
    assert dict(ds._index) == before

    ds.reconcile_all([evidenced])
    assert dict(ds._index) == before
    for dev_id in result["indigoDeviceIds"] + [result["nodeDeviceId"]]:
        assert devices[dev_id].errorState == ""


# --- deletion, with the group real -----------------------------------------

def test_delete_node_dissolves_the_group_before_deleting_anything(ds, indigo_env):
    """End to end against a fake that REFUSES to delete the root of a
    non-empty group, exactly as Indigo does: delete_node ungroups every
    candidate first, so no delete ever meets a non-empty group."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(AQ_NODE), "Air Sensor")
    node_dev_id = result["nodeDeviceId"]
    assert _indigo.device.groups, "the group must actually exist for this to prove anything"

    deleted = ds.delete_node(0x1E)

    assert set(deleted) == set(result["indigoDeviceIds"]) | {node_dev_id}
    assert deleted[-1] == node_dev_id
    assert _indigo.device.groups == {}
    assert not devices._by_id
    # The docstring's actual claim, pinned: EVERY candidate was ungrouped —
    # not just enough of them that the fake's delete happened to drop the
    # rest via _drop_from_group's side effect of a passing delete loop.
    # Without this, the test passes on incidental ordering (delete drops
    # membership per device; node deleted last) rather than on the
    # dissolve-first shape it claims to verify.
    assert set(_indigo.device.ungroup_calls) == set(deleted)


def test_delete_node_survives_a_group_rooted_at_an_endpoint_device(ds, indigo_env):
    """The fielded-install shape, and the reason ordering alone cannot work
    (ADR-0009): here the group's root is the OLDEST ENDPOINT DEVICE, so the
    old children-before-node-device rule would have thrown on the very first
    child it tried to delete. Dissolving first is indifferent to which end
    Indigo picked."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_NODE_WIDE_NODE, "Kitchen sensor", "Kitchen")
    temp_id = ds.lookup(0x63, 1, "matterTemperatureSensor")
    result = ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
    node_dev_id = result["nodeDeviceId"]
    assert _indigo.device.getGroupList(node_dev_id)[0] == temp_id, \
        "sanity: an endpoint device roots this group"

    deleted = ds.delete_node(0x63)

    assert temp_id in deleted and node_dev_id in deleted
    assert _indigo.device.groups == {}
    assert not devices._by_id


def test_a_refused_ungroup_still_lets_delete_node_delete_what_it_can(ds, indigo_env, mock_logger):
    """The dissolve is guarded per device, for the same open-dialog reason the
    grouping calls are: a refused ungroup must be a debug line, not an
    exception out of a decommission — and every device Indigo will still let
    go of is still deleted and still reported."""
    _indigo, devices = indigo_env
    ds.create_from_raw(POWER_SOURCE_NODE_WIDE_NODE, "Kitchen sensor", "Kitchen")
    temp_id = ds.lookup(0x63, 1, "matterTemperatureSensor")
    hum_id = ds.lookup(0x63, 2, "matterHumiditySensor")
    result = ds.create_from_raw(_with_node_evidence(POWER_SOURCE_NODE_WIDE_NODE), "")
    node_dev_id = result["nodeDeviceId"]

    def boom(_dev):
        raise RuntimeError("a device dialog is open")
    _indigo.device.ungroupDevice = boom

    deleted = ds.delete_node(0x63)

    # With the group intact its ROOT — the oldest endpoint device — is the one
    # delete Indigo refuses. Everything else still goes, and the refusal is
    # reported rather than raised.
    assert set(deleted) == {hum_id, node_dev_id}
    assert temp_id not in deleted
    assert any("could not delete Indigo device" in str(call.args)
               for call in mock_logger.warning.call_args_list)
    assert any("could not ungroup Indigo device" in str(call.args)
               for call in mock_logger.debug.call_args_list)


def test_deleting_a_non_empty_groups_root_is_what_the_fake_refuses(ds, indigo_env):
    """The fake's teeth, asserted directly — otherwise the tests above could
    pass against a fake that refuses nothing. On a FRESH commission that root
    is the node device, because create_devices creates it first."""
    _indigo, devices = indigo_env
    result = ds.create_from_raw(_with_node_evidence(RELAY_NODE), "Office Plug")
    with pytest.raises(ValueError):
        _indigo.device.delete(devices[result["nodeDeviceId"]])


def test_ungroup_then_user_delete_still_tombstones_the_node(ds, indigo_env):
    """With grouping live, the realistic route to a hand-deleted node device is
    ungroup-then-delete (Indigo refuses to delete a non-empty group's root).
    That still has to reach deviceDeleted and still has to tombstone."""
    _indigo, devices = indigo_env
    import device_sync as device_sync_mod
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    node_dev_id = result["nodeDeviceId"]

    _indigo.device.ungroupDevice(node_dev_id)      # the user ungroups it first
    _indigo.device.delete(devices[node_dev_id])    # ...then deletes it
    ds.note_node_device_deleted(42, node_dev_id)   # plugin.deviceDeleted's route

    assert ds.node_tombstones.is_tombstoned(42)
    assert ds.create_from_raw(evidenced, "Office Plug")["nodeDeviceId"] is None
