"""DoorLock cluster (0x0101) handler tests.

Covers:
- All LockState attribute mappings (Locked, Unlocked, NotFullyLocked, None/null)
- TurnOn / TurnOff / Toggle action dispatch (Toggle from both locked and unlocked state)
- create_indigo_devices spec shape
- Registry lookup by cluster id and device type
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from matter_model import parse_node
from matter_handlers.door_lock import DoorLockHandler, CLUSTER_DOOR_LOCK, ATTR_LOCK_STATE
from matter_handlers.registry import HandlerRegistry
from protocol import MatterCommand


# ---------------------------------------------------------------------------
# Minimal node fixture — DoorLock cluster (0x0101) on endpoint 1
# ---------------------------------------------------------------------------

LOCK_NODE = {
    "node_id": 50,
    "available": True,
    "is_bridge": False,
    "attributes": {
        "0/40/1": "Aqara",
        "0/40/3": "U200 Lock",
        "1/257/0": 1,    # DoorLock.LockState = Locked (cluster 257 = 0x0101)
        "1/29/0": [{"0": 11, "1": 1}],  # Door Lock device type id = 11 (0x000B)
    },
}


def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


def _dev(on_state=True):
    """Minimal Indigo device stub for action tests."""
    dev = SimpleNamespace(
        pluginProps={"nodeId": "50", "endpointId": "1"},
        onState=on_state,
    )
    return dev


def _action(device_action):
    return SimpleNamespace(deviceAction=device_action)


# ---------------------------------------------------------------------------
# create_indigo_devices
# ---------------------------------------------------------------------------

class TestCreateIndigoDevices:
    def test_spec_shape(self):
        node = parse_node(LOCK_NODE, "Front Door")
        handler = DoorLockHandler()
        specs = handler.create_indigo_devices(node, _ep(node, 1))
        assert len(specs) == 1
        spec = specs[0]
        assert spec.device_type_id == "matterLock"
        assert spec.name == "Front Door"
        assert spec.props["nodeId"] == "50"
        assert spec.props["endpointId"] == "1"
        assert spec.props["vendorName"] == "Aqara"
        assert spec.props["productName"] == "U200 Lock"

    def test_initial_states_deferred(self):
        # initial_states is empty — we wait for the first attribute_updated
        # rather than guessing the lock state at commissioning time.
        node = parse_node(LOCK_NODE)
        handler = DoorLockHandler()
        specs = handler.create_indigo_devices(node, _ep(node, 1))
        assert specs[0].initial_states == {}

    def test_fallback_name_uses_product_name(self):
        node = parse_node(LOCK_NODE)  # no suggested_name
        handler = DoorLockHandler()
        specs = handler.create_indigo_devices(node, _ep(node, 1))
        assert specs[0].name == "U200 Lock"


# ---------------------------------------------------------------------------
# attributes_to_subscribe
# ---------------------------------------------------------------------------

class TestAttributesSubscription:
    def test_subscribes_to_lock_state(self):
        handler = DoorLockHandler()
        assert ATTR_LOCK_STATE in handler.attributes_to_subscribe()


# ---------------------------------------------------------------------------
# on_attribute_update — all LockState mappings
# ---------------------------------------------------------------------------

class TestAttributeUpdate:
    def setup_method(self):
        self.handler = DoorLockHandler()

    def test_locked_maps_to_on_and_locked(self):
        result = self.handler.on_attribute_update(None, ATTR_LOCK_STATE, 1)
        assert result == {"onOffState": True, "lockState": "locked"}

    def test_unlocked_maps_to_off_and_unlocked(self):
        result = self.handler.on_attribute_update(None, ATTR_LOCK_STATE, 2)
        assert result == {"onOffState": False, "lockState": "unlocked"}

    def test_not_fully_locked_maps_to_off_and_jammed(self):
        # NotFullyLocked (0) is treated as off + jammed; surfacing as an Indigo
        # error state (setErrorStateOnServer) is deliberately deferred.
        result = self.handler.on_attribute_update(None, ATTR_LOCK_STATE, 0)
        assert result == {"onOffState": False, "lockState": "jammed"}

    def test_null_lock_state_returns_empty(self):
        # Nullable attribute — do NOT fabricate a state from a null value.
        result = self.handler.on_attribute_update(None, ATTR_LOCK_STATE, None)
        assert result == {}

    def test_unknown_attribute_id_returns_empty(self):
        result = self.handler.on_attribute_update(None, 0x9999, 1)
        assert result == {}


# ---------------------------------------------------------------------------
# handle_indigo_action
# ---------------------------------------------------------------------------

class TestHandleIndigoAction:
    def test_turn_on_sends_lock_door(self, mock_indigo_base):
        import indigo
        handler = DoorLockHandler()
        cmd = handler.handle_indigo_action(_dev(on_state=False), _action(indigo.kDeviceAction.TurnOn))
        assert isinstance(cmd, MatterCommand)
        assert cmd.node_id == 50
        assert cmd.endpoint == 1
        assert cmd.cluster == CLUSTER_DOOR_LOCK
        assert cmd.command == "LockDoor"
        assert cmd.args == {}

    def test_turn_off_sends_unlock_door(self, mock_indigo_base):
        import indigo
        handler = DoorLockHandler()
        cmd = handler.handle_indigo_action(_dev(on_state=True), _action(indigo.kDeviceAction.TurnOff))
        assert isinstance(cmd, MatterCommand)
        assert cmd.command == "UnlockDoor"
        assert cmd.args == {}

    def test_toggle_from_locked_sends_unlock(self, mock_indigo_base):
        import indigo
        handler = DoorLockHandler()
        # Device is currently locked (onState=True) → Toggle should unlock it.
        cmd = handler.handle_indigo_action(_dev(on_state=True), _action(indigo.kDeviceAction.Toggle))
        assert cmd.command == "UnlockDoor"

    def test_toggle_from_unlocked_sends_lock(self, mock_indigo_base):
        import indigo
        handler = DoorLockHandler()
        # Device is currently unlocked (onState=False) → Toggle should lock it.
        cmd = handler.handle_indigo_action(_dev(on_state=False), _action(indigo.kDeviceAction.Toggle))
        assert cmd.command == "LockDoor"

    def test_unmapped_action_returns_none(self, mock_indigo_base):
        import indigo
        handler = DoorLockHandler()
        result = handler.handle_indigo_action(_dev(), _action(indigo.kDeviceAction.SetBrightness))
        assert result is None

    def test_command_carries_correct_node_and_endpoint(self, mock_indigo_base):
        import indigo
        handler = DoorLockHandler()
        dev = SimpleNamespace(
            pluginProps={"nodeId": "99", "endpointId": "3"},
            onState=False,
        )
        cmd = handler.handle_indigo_action(dev, _action(indigo.kDeviceAction.TurnOn))
        assert cmd.node_id == 99 and cmd.endpoint == 3


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

class TestRegistryIntegration:
    def test_registry_lookup_by_cluster_id(self):
        reg = HandlerRegistry()
        handler = reg.handler_for_cluster(CLUSTER_DOOR_LOCK)
        assert isinstance(handler, DoorLockHandler)

    def test_registry_lookup_by_device_type(self):
        reg = HandlerRegistry()

        class Dev:
            deviceTypeId = "matterLock"

        handler = reg.handler_for_device(Dev())
        assert isinstance(handler, DoorLockHandler)

    def test_registry_creates_lock_spec_for_door_lock_endpoint(self):
        node = parse_node(LOCK_NODE, "Front Door")
        reg = HandlerRegistry()
        specs = reg.handlers_for_endpoint(node, _ep(node, 1))
        assert len(specs) == 1
        assert specs[0].device_type_id == "matterLock"
