"""Tests for the ValveConfigurationAndControl cluster handler (0x0081).

Covers:
  - All three CurrentState enum values (Open/Closed/Transitioning) + None
  - Transitioning preserves onOffState (no onOffState key in returned dict)
  - Action mappings: TurnOn → Open, TurnOff → Close, Toggle resolves from state
  - create_indigo_devices spec shape
  - registry lookup by cluster id and device type
"""
from __future__ import annotations

import pytest

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.valve import (
    ATTR_CURRENT_STATE,
    CLUSTER_VALVE,
    CMD_CLOSE,
    CMD_OPEN,
    STATE_CLOSED,
    STATE_OPEN,
    STATE_TRANSITIONING,
    ValveHandler,
)


# ---------------------------------------------------------------------------
# Minimal node fixture for a valve endpoint
# ---------------------------------------------------------------------------

VALVE_NODE = {
    "node_id": 99,
    "available": True,
    "is_bridge": False,
    "attributes": {
        "0/40/1": "Moen",
        "0/40/2": 9999,
        "0/40/3": "Flo Valve",
        "0/40/4": 1234,
        "0/40/10": "1.0.0",
        # Endpoint 1 carries only the valve cluster (0x0081 = 129 decimal)
        "1/29/0": [{"0": 0x0042, "1": 1}],  # DeviceTypeList — WaterValve device type 0x0042
        "1/129/4": 0,                       # CurrentState → Closed
    },
}


def _endpoint(node, endpoint_id):
    return next(ep for ep in node.endpoints if ep.endpoint_id == endpoint_id)


# ---------------------------------------------------------------------------
# create_indigo_devices
# ---------------------------------------------------------------------------

def test_create_indigo_devices_spec():
    node = parse_node(VALVE_NODE, suggested_name="Garden Valve")
    handler = ValveHandler()
    ep1 = _endpoint(node, 1)
    specs = handler.create_indigo_devices(node, ep1)

    assert len(specs) == 1
    spec = specs[0]
    assert spec.device_type_id == "matterValve"
    assert spec.name == "Garden Valve"
    assert spec.props["nodeId"] == "99"
    assert spec.props["endpointId"] == "1"
    assert spec.props["vendorName"] == "Moen"
    assert spec.props["productName"] == "Flo Valve"
    # initial_states is empty — valve position unknown until first attribute update
    assert spec.initial_states == {}


def test_create_indigo_devices_falls_back_to_product_name_when_no_suggested_name():
    node = parse_node(VALVE_NODE)  # no suggested_name
    handler = ValveHandler()
    specs = handler.create_indigo_devices(node, _endpoint(node, 1))
    assert specs[0].name == "Flo Valve"


# ---------------------------------------------------------------------------
# attributes_to_subscribe
# ---------------------------------------------------------------------------

def test_attributes_to_subscribe():
    handler = ValveHandler()
    assert handler.attributes_to_subscribe() == [ATTR_CURRENT_STATE]


# ---------------------------------------------------------------------------
# on_attribute_update — CurrentState mappings
# ---------------------------------------------------------------------------

def test_open_state_sets_on_and_valve_state():
    handler = ValveHandler()
    result = handler.on_attribute_update(None, ATTR_CURRENT_STATE, STATE_OPEN)
    assert result == {"onOffState": True, "valveState": "open"}


def test_closed_state_sets_off_and_valve_state():
    handler = ValveHandler()
    result = handler.on_attribute_update(None, ATTR_CURRENT_STATE, STATE_CLOSED)
    assert result == {"onOffState": False, "valveState": "closed"}


def test_transitioning_state_sets_valve_state_only():
    """Transitioning must NOT include an onOffState key.

    The caller should keep the device's last stable on/off state intact while
    the valve is in motion, so we deliberately omit onOffState here.
    """
    handler = ValveHandler()
    result = handler.on_attribute_update(None, ATTR_CURRENT_STATE, STATE_TRANSITIONING)
    assert "onOffState" not in result
    assert result.get("valveState") == "transitioning"


def test_null_current_state_returns_empty_dict():
    """A nullable null must not fabricate state."""
    handler = ValveHandler()
    result = handler.on_attribute_update(None, ATTR_CURRENT_STATE, None)
    assert result == {}


def test_unknown_attribute_id_returns_empty_dict():
    handler = ValveHandler()
    assert handler.on_attribute_update(None, 0x9999, STATE_OPEN) == {}


def test_unknown_enum_value_returns_empty_dict():
    """Guard against future spec extensions with new enum values."""
    handler = ValveHandler()
    result = handler.on_attribute_update(None, ATTR_CURRENT_STATE, 99)
    assert result == {}


# ---------------------------------------------------------------------------
# handle_indigo_action
# ---------------------------------------------------------------------------

class _Dev:
    """Minimal Indigo device double."""
    def __init__(self, node_id: str, endpoint_id: str, on_state: bool = False):
        self.pluginProps = {"nodeId": node_id, "endpointId": endpoint_id}
        self.onState = on_state


class _Action:
    def __init__(self, device_action):
        self.deviceAction = device_action


def test_turn_on_sends_open_command(mock_indigo_base):
    import indigo
    handler = ValveHandler()
    dev = _Dev("99", "1")
    action = _Action(indigo.kDeviceAction.TurnOn)

    cmd = handler.handle_indigo_action(dev, action)
    assert cmd is not None
    assert cmd.node_id == 99
    assert cmd.endpoint == 1
    assert cmd.cluster == CLUSTER_VALVE
    assert cmd.command == CMD_OPEN
    assert cmd.args == {}


def test_turn_off_sends_close_command(mock_indigo_base):
    import indigo
    handler = ValveHandler()
    dev = _Dev("99", "1")
    action = _Action(indigo.kDeviceAction.TurnOff)

    cmd = handler.handle_indigo_action(dev, action)
    assert cmd.command == CMD_CLOSE


def test_toggle_from_open_sends_close(mock_indigo_base):
    """Toggle resolves via onState — open valve → send Close."""
    import indigo
    handler = ValveHandler()
    dev = _Dev("99", "1", on_state=True)
    action = _Action(indigo.kDeviceAction.Toggle)

    cmd = handler.handle_indigo_action(dev, action)
    assert cmd.command == CMD_CLOSE


def test_toggle_from_closed_sends_open(mock_indigo_base):
    """Toggle resolves via onState — closed valve → send Open."""
    import indigo
    handler = ValveHandler()
    dev = _Dev("99", "1", on_state=False)
    action = _Action(indigo.kDeviceAction.Toggle)

    cmd = handler.handle_indigo_action(dev, action)
    assert cmd.command == CMD_OPEN


def test_toggle_with_unknown_state_returns_none(mock_indigo_base):
    """Toggle must return None when onState is absent — never guess a valve's direction."""
    import indigo
    handler = ValveHandler()

    class _DevNoState:
        """Device double with no onState attribute at all."""
        pluginProps = {"nodeId": "99", "endpointId": "1"}

    dev = _DevNoState()
    action = _Action(indigo.kDeviceAction.Toggle)
    assert handler.handle_indigo_action(dev, action) is None


def test_toggle_with_none_state_returns_none(mock_indigo_base):
    """Toggle must return None when onState is explicitly None."""
    import indigo
    handler = ValveHandler()

    class _DevNoneState:
        pluginProps = {"nodeId": "99", "endpointId": "1"}
        onState = None

    dev = _DevNoneState()
    action = _Action(indigo.kDeviceAction.Toggle)
    assert handler.handle_indigo_action(dev, action) is None


def test_unmapped_action_returns_none(mock_indigo_base):
    import indigo
    handler = ValveHandler()
    dev = _Dev("99", "1")
    action = _Action(indigo.kDeviceAction.SetBrightness)

    assert handler.handle_indigo_action(dev, action) is None


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

def test_registry_finds_valve_handler_by_cluster():
    from matter_handlers.valve import ValveHandler as VH
    reg = HandlerRegistry()
    h = reg.handler_for_cluster(CLUSTER_VALVE)
    assert isinstance(h, VH)


def test_registry_finds_valve_handler_by_device_type():
    from matter_handlers.valve import ValveHandler as VH
    reg = HandlerRegistry()

    class Dev:
        deviceTypeId = "matterValve"

    assert isinstance(reg.handler_for_device(Dev()), VH)


def test_registry_creates_valve_spec_for_valve_endpoint():
    """End-to-end: parse a valve node → registry → one matterValve spec."""
    node = parse_node(VALVE_NODE, suggested_name="Water Main")
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, _endpoint(node, 1))
    assert len(specs) == 1
    assert specs[0].device_type_id == "matterValve"
    assert specs[0].name == "Water Main"
