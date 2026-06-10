"""Tests for Matter bridge support: BridgedDeviceBasicInformation (0x0039) naming,
endpoint_added/endpoint_removed event parsing, targeted endpoint unreachability,
and the per-endpoint Reachable (0x0039/0x0011) attribute path.

VERIFIED wire shapes (ws-controller v0.6.2):
  @matter-server/ws-controller/src/server/WebSocketControllerHandler.ts
  lines 288-309 (nodeEndpointAdded / nodeEndpointRemoved observers):
    endpoint_added:   {"event": "endpoint_added",   "data": {"node_id": N, "endpoint_id": E}}
    endpoint_removed: {"event": "endpoint_removed",  "data": {"node_id": N, "endpoint_id": E}}
  Both ids are plain integers (TypeScript NodeId / EndpointNumber).

  PairedNode.ts #triggerNodeStructureChanges (line 954) always emits
  structureChanged → node_updated AFTER nodeEndpointAdded/Removed, so a
  node_updated reliably follows every endpoint_added.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import protocol
from protocol import MatterEvent, Protocol
from matter_model import (
    BBRIDGE_ATTR_REACHABLE,
    CLUSTER_BRIDGED_BASIC,
    EndpointInfo,
    parse_node,
)


@dataclass
class FakeFolder:
    """Minimal folder stub (same two fields as test_device_sync.FakeFolder)."""
    id: int
    name: str


# ---------------------------------------------------------------------------
# Helpers shared across bridge tests
# ---------------------------------------------------------------------------

# A bridge node (e.g. Philips Hue) with two bridged child endpoints.
# Endpoint 0: root / BasicInformation for the bridge hub itself.
# Endpoint 1: Hue bulb child — has 0x0039 (BridgedDeviceBasicInformation)
#             + 0x0006 (OnOff) + 0x001D (Descriptor).
# Endpoint 2: Another bulb child — has 0x0039 + 0x0006 + 0x001D.
# 0x0039 attr keys use decimal cluster id 57.
_BRIDGE_NODE_RAW = {
    "node_id": 200,
    "available": True,
    "attributes": {
        # Hub identity on endpoint 0
        "0/40/1": "Signify Netherlands B.V.",
        "0/40/3": "Hue Bridge",
        # Child 1 — "Hue bulb bedroom" (NodeLabel set by the user in Hue app)
        "1/57/1": "Signify Netherlands B.V.",   # VendorName   (0x0039/0x0001)
        "1/57/3": "Hue color lamp",              # ProductName  (0x0039/0x0003)
        "1/57/5": "Bedroom bulb",                # NodeLabel    (0x0039/0x0005)
        "1/57/17": True,                         # Reachable    (0x0039/0x0011)
        "1/6/0": True,                           # OnOff state
        "1/29/0": [{"0": 266}],                  # Descriptor → DeviceType 266 (OnOff Light)
        # Child 2 — product name only (no NodeLabel set)
        "2/57/1": "Signify Netherlands B.V.",
        "2/57/3": "Hue white lamp",
        "2/57/17": True,
        "2/6/0": False,
        "2/29/0": [{"0": 266}],
    },
}

# Same bridge node but child 1 is unreachable
_BRIDGE_NODE_EP1_UNREACHABLE = {
    **_BRIDGE_NODE_RAW,
    "attributes": {
        **_BRIDGE_NODE_RAW["attributes"],
        "1/57/17": False,
    },
}


# ===========================================================================
# 1. matter_model: parse_node populates 0x0039 identity on EndpointInfo
# ===========================================================================

def test_parse_node_bridged_endpoint_label_extracted():
    """NodeLabel and ProductName from 0x0039 populate EndpointInfo fields."""
    node = parse_node(_BRIDGE_NODE_RAW)
    ep1 = next(e for e in node.endpoints if e.endpoint_id == 1)
    assert ep1.node_label == "Bedroom bulb"
    assert ep1.product_name == "Hue color lamp"
    assert ep1.vendor_name == "Signify Netherlands B.V."


def test_parse_node_bridged_endpoint_no_label_product_name_only():
    """Endpoint 2 has no NodeLabel — node_label is empty, product_name populated."""
    node = parse_node(_BRIDGE_NODE_RAW)
    ep2 = next(e for e in node.endpoints if e.endpoint_id == 2)
    assert ep2.node_label == ""
    assert ep2.product_name == "Hue white lamp"


def test_parse_node_non_bridged_endpoint_all_defaults():
    """A plain non-bridged endpoint (no 0x0039 cluster) has empty bridge fields."""
    raw = {
        "node_id": 1,
        "attributes": {
            "1/6/0": False,
            "1/29/0": [{"0": 266}],
        },
    }
    node = parse_node(raw)
    ep1 = next(e for e in node.endpoints if e.endpoint_id == 1)
    assert ep1.node_label == ""
    assert ep1.vendor_name == ""
    assert ep1.product_name == ""


def test_parse_node_bridge_identity_absent_fields_default_to_empty():
    """0x0039 cluster present but optional attrs missing → empty string defaults."""
    raw = {
        "node_id": 5,
        "attributes": {
            # Only Reachable attr (0x0011) present; no label/name attrs
            "1/57/17": True,
            "1/6/0": False,
            "1/29/0": [{"0": 266}],
        },
    }
    node = parse_node(raw)
    ep1 = next(e for e in node.endpoints if e.endpoint_id == 1)
    assert ep1.node_label == ""
    assert ep1.product_name == ""
    assert ep1.vendor_name == ""


def test_endpoint_info_has_cluster_bridged_basic():
    """EndpointInfo.has() reports cluster 0x0039 for bridged endpoints."""
    node = parse_node(_BRIDGE_NODE_RAW)
    ep1 = next(e for e in node.endpoints if e.endpoint_id == 1)
    assert ep1.has(CLUSTER_BRIDGED_BASIC)


# ===========================================================================
# 2. Naming: bridge label wins; suffix skipped; fallback unchanged
# ===========================================================================

# FakeIndigo infrastructure (shared with test_device_sync.py pattern)

class _FakeDev:
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
        self.replaced = True


class _FakeDevices:
    def __init__(self):
        self._by_id = {}
        self._counter = 3000
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


class _FakeFolderFactory:
    def __init__(self, devices):
        self.devices = devices

    def create(self, name):
        return self.devices.add_folder(name)


class _FakeDeviceFactory:
    def __init__(self, devices):
        self.devices = devices
        self.created = []

    def create(self, protocol=None, deviceTypeId="", name="", props=None, folder=0, **kwargs):
        dev = _FakeDev(self.devices.next_id(), name, deviceTypeId, dict(props or {}))
        if isinstance(folder, int) and folder:
            dev.folderId = folder
        self.devices.add(dev)
        self.created.append(dev)
        return dev

    def moveToFolder(self, dev_or_id, value=None):
        dev = dev_or_id if hasattr(dev_or_id, "folderId") else self.devices[dev_or_id]
        dev.folderId = value


@pytest.fixture
def bridge_indigo_env(mock_indigo_base):
    indigo = mock_indigo_base
    devices = _FakeDevices()
    indigo.devices = devices
    indigo.devices.folder = _FakeFolderFactory(devices)
    indigo.device = _FakeDeviceFactory(devices)
    indigo.kProtocol = SimpleNamespace(Plugin="plugin")
    return indigo, devices


@pytest.fixture
def bds(bridge_indigo_env, mock_logger):
    import device_sync
    importlib.reload(device_sync)
    from matter_handlers.registry import HandlerRegistry
    return device_sync.DeviceSync(HandlerRegistry(), mock_logger)


def test_bridged_endpoint_name_uses_node_label(bds, bridge_indigo_env):
    """When an endpoint has 0x0039 NodeLabel, it is used as the device name."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    dev_id1 = bds.lookup(200, 1)
    assert dev_id1 is not None
    assert devices[dev_id1].name == "Bedroom bulb"


def test_bridged_endpoint_no_label_uses_product_name(bds, bridge_indigo_env):
    """When NodeLabel is absent but ProductName present, ProductName is used."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    dev_id2 = bds.lookup(200, 2)
    assert dev_id2 is not None
    assert devices[dev_id2].name == "Hue white lamp"


def test_bridged_endpoint_name_skips_endpoint_suffix(bds, bridge_indigo_env):
    """Bridged children with 0x0039 identity never get '(endpoint N)' appended."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    dev_id1 = bds.lookup(200, 1)
    dev_id2 = bds.lookup(200, 2)
    assert "(endpoint" not in devices[dev_id1].name
    assert "(endpoint" not in devices[dev_id2].name


def test_non_bridged_multi_endpoint_keeps_suffix(bds, bridge_indigo_env):
    """Non-bridged multi-endpoint nodes still get '(endpoint N)' suffix."""
    _indigo, devices = bridge_indigo_env
    multi_node = {
        "node_id": 99,
        "attributes": {
            "1/6/0": False, "1/29/0": [{"0": 266}],
            "2/6/0": False, "2/29/0": [{"0": 266}],
        },
    }
    bds.create_from_raw(multi_node, "Patio")
    id1 = bds.lookup(99, 1)
    id2 = bds.lookup(99, 2)
    assert devices[id1].name == "Patio (endpoint 1)"
    assert devices[id2].name == "Patio (endpoint 2)"


def test_authoritative_rename_wins_over_bridge_label(bds, bridge_indigo_env):
    """A user-chosen name (authoritative commission pass) overrides the bridge label."""
    _indigo, devices = bridge_indigo_env
    # First pass without name (node_added / reconcile) → bridge label used
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    dev_id1 = bds.lookup(200, 1)
    assert devices[dev_id1].name == "Bedroom bulb"

    # Commission pass with user-chosen name → overrides
    bds.create_from_raw(_BRIDGE_NODE_RAW, "My Bedroom Light")
    assert devices[dev_id1].name == "My Bedroom Light"


def test_bridge_unique_name_deduplication(bds, bridge_indigo_env):
    """Collision on the bridge label falls back to _unique_name suffixing."""
    _indigo, devices = bridge_indigo_env
    # Create an existing device with the same name as the label
    existing = _FakeDev(9999, "Bedroom bulb", "matterRelay", {})
    devices.add(existing)

    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    dev_id1 = bds.lookup(200, 1)
    assert devices[dev_id1].name == "Bedroom bulb 2"


# ===========================================================================
# 3. Protocol: endpoint_added / endpoint_removed golden-frame parsing
# ===========================================================================

# GOLDEN FRAMES — verified vs. ws-controller v0.6.2 source:
# WebSocketControllerHandler.ts lines 288-309
GOLDEN_ENDPOINT_ADDED_FRAME = {
    "event": "endpoint_added",
    "data": {"node_id": 200, "endpoint_id": 3},
}

GOLDEN_ENDPOINT_REMOVED_FRAME = {
    "event": "endpoint_removed",
    "data": {"node_id": 200, "endpoint_id": 1},
}


@pytest.fixture
def proto():
    return Protocol()


def test_parse_endpoint_added_golden_frame(proto):
    """endpoint_added: node_id and endpoint_id are correctly extracted."""
    evt = proto.parse_event(GOLDEN_ENDPOINT_ADDED_FRAME)
    assert evt.kind == protocol.EVT_ENDPOINT_ADDED
    assert evt.node_id == 200
    assert evt.endpoint == 3


def test_parse_endpoint_removed_golden_frame(proto):
    """endpoint_removed: node_id and endpoint_id are correctly extracted."""
    evt = proto.parse_event(GOLDEN_ENDPOINT_REMOVED_FRAME)
    assert evt.kind == protocol.EVT_ENDPOINT_REMOVED
    assert evt.node_id == 200
    assert evt.endpoint == 1


def test_parse_endpoint_added_hex_string_ids(proto):
    """endpoint_added: hex-string ids are coerced to int (same contract as node_event)."""
    frame = {
        "event": "endpoint_added",
        "data": {"node_id": "0xC8", "endpoint_id": "0x03"},
    }
    evt = proto.parse_event(frame)
    assert evt.node_id == 200
    assert evt.endpoint == 3


def test_parse_endpoint_removed_hex_string_ids(proto):
    """endpoint_removed: hex-string ids tolerated."""
    frame = {
        "event": "endpoint_removed",
        "data": {"node_id": "0xC8", "endpoint_id": "0x01"},
    }
    evt = proto.parse_event(frame)
    assert evt.node_id == 200
    assert evt.endpoint == 1


def test_parse_endpoint_added_malformed_non_dict(proto):
    """endpoint_added with non-dict data → node_id/endpoint remain None."""
    frame = {"event": "endpoint_added", "data": "unexpected-string"}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_ENDPOINT_ADDED
    assert evt.node_id is None
    assert evt.endpoint is None


def test_parse_endpoint_removed_malformed_non_dict(proto):
    """endpoint_removed with non-dict data → node_id/endpoint remain None."""
    frame = {"event": "endpoint_removed", "data": 42}
    evt = proto.parse_event(frame)
    assert evt.kind == protocol.EVT_ENDPOINT_REMOVED
    # bare int data → not a dict → falls into fallback (no node_id key matched)
    assert evt.node_id is None
    assert evt.endpoint is None


def test_parse_endpoint_added_missing_endpoint_id(proto):
    """endpoint_added dict missing endpoint_id → endpoint is None."""
    frame = {"event": "endpoint_added", "data": {"node_id": 5}}
    evt = proto.parse_event(frame)
    assert evt.node_id == 5
    assert evt.endpoint is None


def test_evt_endpoint_constants():
    assert protocol.EVT_ENDPOINT_ADDED == "endpoint_added"
    assert protocol.EVT_ENDPOINT_REMOVED == "endpoint_removed"


# ===========================================================================
# 4. DeviceSync: endpoint_removed → targeted unreachable
# ===========================================================================

def test_endpoint_removed_marks_only_that_endpoint_unreachable(bds, bridge_indigo_env):
    """endpoint_removed marks only the specified endpoint's device unreachable."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    id1 = bds.lookup(200, 1)
    id2 = bds.lookup(200, 2)
    assert id1 and id2

    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ENDPOINT_REMOVED,
        node_id=200, endpoint=1,
        raw=GOLDEN_ENDPOINT_REMOVED_FRAME,
    ))
    assert devices[id1].errorState == "unreachable"
    assert devices[id2].errorState == ""   # untouched


def test_endpoint_removed_unknown_endpoint_is_no_op(bds, bridge_indigo_env):
    """endpoint_removed for an endpoint with no Indigo device is a no-op (no crash)."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    # endpoint 99 doesn't exist
    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ENDPOINT_REMOVED,
        node_id=200, endpoint=99,
        raw={"event": "endpoint_removed", "data": {"node_id": 200, "endpoint_id": 99}},
    ))  # must not raise


def test_endpoint_removed_malformed_frame_is_logged(bds, bridge_indigo_env, mock_logger):
    """endpoint_removed with node_id=None logs a warning and is dropped."""
    _indigo, _devices = bridge_indigo_env
    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ENDPOINT_REMOVED,
        node_id=None, endpoint=None,
        raw={"event": "endpoint_removed", "data": "bad"},
    ))
    assert mock_logger.warning.called


def test_mark_endpoint_unreachable_helper(bds, bridge_indigo_env):
    """mark_endpoint_unreachable targets a single (node, endpoint) pair."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    id1 = bds.lookup(200, 1)
    id2 = bds.lookup(200, 2)

    bds.mark_endpoint_unreachable(200, 1)
    assert devices[id1].errorState == "unreachable"
    assert devices[id2].errorState == ""


def test_mark_endpoint_unreachable_missing_device_is_no_op(bds, bridge_indigo_env):
    """mark_endpoint_unreachable for an unregistered (node, endpoint) is silent."""
    bds.mark_endpoint_unreachable(999, 1)  # no device → must not raise


# ===========================================================================
# 5. DeviceSync: endpoint_added → debug log + await node_updated
# ===========================================================================

def test_endpoint_added_logs_debug_and_does_not_create(bds, bridge_indigo_env, mock_logger):
    """endpoint_added: a debug log is emitted; no Indigo device is created."""
    _indigo, devices = bridge_indigo_env
    count_before = len(list(devices))
    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ENDPOINT_ADDED,
        node_id=200, endpoint=3,
        raw=GOLDEN_ENDPOINT_ADDED_FRAME,
    ))
    assert mock_logger.debug.called
    assert len(list(devices)) == count_before  # no new device


def test_endpoint_added_malformed_frame_is_logged(bds, bridge_indigo_env, mock_logger):
    """endpoint_added with node_id=None logs a warning and is dropped."""
    _indigo, _devices = bridge_indigo_env
    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ENDPOINT_ADDED,
        node_id=None, endpoint=None,
        raw={"event": "endpoint_added", "data": "bad"},
    ))
    assert mock_logger.warning.called


# ===========================================================================
# 6. DeviceSync: per-endpoint Reachable (0x0039/0x0011) via attribute_updated
# ===========================================================================

def test_reachable_false_sets_error_state(bds, bridge_indigo_env):
    """cluster 0x0039 attr 0x0011 = False → setErrorStateOnServer('unreachable')."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    id1 = bds.lookup(200, 1)

    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=200, endpoint=1,
        cluster=CLUSTER_BRIDGED_BASIC, attribute=BBRIDGE_ATTR_REACHABLE,
        value=False,
        raw={"event": "attribute_updated", "data": [200, "1/57/17", False]},
    ))
    assert devices[id1].errorState == "unreachable"


def test_reachable_true_clears_error_state(bds, bridge_indigo_env):
    """cluster 0x0039 attr 0x0011 = True → clearErrorStateOnServer."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    id1 = bds.lookup(200, 1)
    devices[id1].setErrorStateOnServer("unreachable")  # pre-set stale error

    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=200, endpoint=1,
        cluster=CLUSTER_BRIDGED_BASIC, attribute=BBRIDGE_ATTR_REACHABLE,
        value=True,
        raw={"event": "attribute_updated", "data": [200, "1/57/17", True]},
    ))
    assert devices[id1].errorState == ""


def test_reachable_attr_does_not_dispatch_to_handler(bds, bridge_indigo_env):
    """cluster 0x0039 / attr 0x0011 must NOT reach handler dispatch path."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    id1 = bds.lookup(200, 1)
    initial_states = dict(devices[id1].states)

    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=200, endpoint=1,
        cluster=CLUSTER_BRIDGED_BASIC, attribute=BBRIDGE_ATTR_REACHABLE,
        value=False,
    ))
    # Reachable event must not change any Indigo states (only error flag)
    assert devices[id1].states == initial_states


def test_reachable_attr_unknown_device_is_no_op(bds, bridge_indigo_env):
    """Reachable attr for an unknown endpoint is silently ignored."""
    _indigo, _devices = bridge_indigo_env
    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=200, endpoint=99,
        cluster=CLUSTER_BRIDGED_BASIC, attribute=BBRIDGE_ATTR_REACHABLE,
        value=False,
    ))  # must not raise


def test_reachable_none_value_leaves_device_untouched(bds, bridge_indigo_env):
    """Reachable attr value=None (absent/null) must not mark device unreachable."""
    _indigo, devices = bridge_indigo_env
    bds.create_from_raw(_BRIDGE_NODE_RAW, "")
    id1 = bds.lookup(200, 1)
    # Pre-set a clean error state so we can detect any unwanted change.
    devices[id1].setErrorStateOnServer("")

    bds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED,
        node_id=200, endpoint=1,
        cluster=CLUSTER_BRIDGED_BASIC, attribute=BBRIDGE_ATTR_REACHABLE,
        value=None,
        raw={"event": "attribute_updated", "data": [200, "1/57/17", None]},
    ))
    # Neither cleared (still "") nor set to "unreachable" — state is unchanged.
    assert devices[id1].errorState == ""
