"""Tests for standalone FanControl (0x0202) → matterFan dimmer device.

Covers:
- FanControlHandler.is_primary_for() — True when no Thermostat co-located,
  False when Thermostat is present (merged case).
- create_indigo_devices() — returns one matterFan spec when primary, [] otherwise.
- attributes_to_subscribe() — exposes FanMode, PercentSetting, PercentCurrent.
- on_attribute_update() for matterFan target device:
    FanMode 0 → onOffState False; non-zero → onOffState True.
    PercentCurrent value → brightnessLevel; None → {}.
- on_attribute_update() for matterThermostat target — existing hvacFanMode
  mapping unchanged (regression guard).
- handle_indigo_action() TurnOn / TurnOff / Toggle / SetBrightness / BrightenBy / DimBy.
- Registry picks up matterFan for handler_for_device().
- Existing thermostat-merge tests in test_thermostat.py are unaffected (those
  tests run independently; this file adds 0 risk of collision).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.thermostat import (
    ATTR_FAN_MODE,
    ATTR_PERCENT_CURRENT,
    ATTR_PERCENT_SETTING,
    CLUSTER_FAN_CONTROL,
    CLUSTER_THERMOSTAT,
    FAN_AUTO,
    FAN_ON,
    FanControlHandler,
)
from protocol import MatterWrite

# ---------------------------------------------------------------------------
# Fake node dicts (flat attribute map, matter-server shape)
# ---------------------------------------------------------------------------

# Fan-only endpoint: FanControl cluster, no Thermostat
FAN_ONLY_NODE = {
    "node_id": 50,
    "attributes": {
        "0/40/1": "Dreo",           # BasicInformation.VendorName
        "0/40/3": "Tower Fan",      # ProductName
        "1/514/0": 4,               # FanControl.FanMode = On (514 = 0x202)
        "1/514/2": 60,              # PercentSetting = 60%
        "1/514/3": 58,              # PercentCurrent = 58%
        "1/29/0": [{"0": 43, "1": 1}],  # FanDevice type
    },
}

# Thermostat endpoint that also carries FanControl (merged case)
THERMO_WITH_FAN_NODE = {
    "node_id": 30,
    "attributes": {
        "1/513/0": 2050,    # Thermostat.LocalTemperature (513 = 0x201)
        "1/513/18": 2100,   # OccupiedHeatingSetpoint
        "1/514/0": 5,       # FanControl.FanMode = Auto
        "1/29/0": [{"0": 769}],
    },
}


def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


# ---------------------------------------------------------------------------
# is_primary_for / create_indigo_devices
# ---------------------------------------------------------------------------

def test_is_primary_when_no_thermostat():
    node = parse_node(FAN_ONLY_NODE, "Tower Fan")
    ep = _ep(node, 1)
    assert ep.has(CLUSTER_FAN_CONTROL)
    assert not ep.has(CLUSTER_THERMOSTAT)
    assert FanControlHandler().is_primary_for(node, ep) is True


def test_not_primary_when_thermostat_present():
    node = parse_node(THERMO_WITH_FAN_NODE, "Hall Stat")
    ep = _ep(node, 1)
    assert ep.has(CLUSTER_FAN_CONTROL)
    assert ep.has(CLUSTER_THERMOSTAT)
    assert FanControlHandler().is_primary_for(node, ep) is False


def test_create_devices_returns_matterfan_spec_when_primary():
    node = parse_node(FAN_ONLY_NODE, "Tower Fan")
    ep = _ep(node, 1)
    h = FanControlHandler()
    specs = h.create_indigo_devices(node, ep)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.device_type_id == "matterFan"
    assert spec.name == "Tower Fan"
    assert spec.props["nodeId"] == "50"
    assert spec.props["endpointId"] == "1"
    assert spec.props["vendorName"] == "Dreo"
    assert spec.props["productName"] == "Tower Fan"
    assert spec.initial_states == {}


def test_create_devices_returns_empty_when_not_primary():
    node = parse_node(THERMO_WITH_FAN_NODE, "Hall Stat")
    ep = _ep(node, 1)
    h = FanControlHandler()
    assert h.create_indigo_devices(node, ep) == []


def test_registry_makes_matterfan_for_fan_only_endpoint():
    node = parse_node(FAN_ONLY_NODE, "Tower Fan")
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, _ep(node, 1))
    assert len(specs) == 1
    assert specs[0].device_type_id == "matterFan"


def test_registry_still_makes_thermostat_only_for_thermo_with_fan():
    node = parse_node(THERMO_WITH_FAN_NODE, "Hall Stat")
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, _ep(node, 1))
    assert [s.device_type_id for s in specs] == ["matterThermostat"]


# ---------------------------------------------------------------------------
# attributes_to_subscribe
# ---------------------------------------------------------------------------

def test_attributes_to_subscribe_includes_all_three():
    attrs = FanControlHandler().attributes_to_subscribe()
    assert ATTR_FAN_MODE in attrs
    assert ATTR_PERCENT_SETTING in attrs
    assert ATTR_PERCENT_CURRENT in attrs


# ---------------------------------------------------------------------------
# on_attribute_update — matterFan device
# ---------------------------------------------------------------------------

def _fan_dev():
    """Minimal matterFan device stub."""
    return SimpleNamespace(deviceTypeId="matterFan", onState=False, brightness=0)


def test_fan_mode_off_sets_onoffstate_false():
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), ATTR_FAN_MODE, 0) == {"onOffState": False}


def test_fan_mode_on_sets_onoffstate_true():
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), ATTR_FAN_MODE, FAN_ON) == {"onOffState": True}
    assert h.on_attribute_update(_fan_dev(), ATTR_FAN_MODE, FAN_AUTO) == {"onOffState": True}
    # Any non-zero FanMode → on (covers Low/Medium/High/Smart)
    assert h.on_attribute_update(_fan_dev(), ATTR_FAN_MODE, 1) == {"onOffState": True}


def test_fan_mode_none_returns_empty():
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), ATTR_FAN_MODE, None) == {}


def test_percent_current_sets_brightness_level():
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), ATTR_PERCENT_CURRENT, 75) == {"brightnessLevel": 75}
    assert h.on_attribute_update(_fan_dev(), ATTR_PERCENT_CURRENT, 0) == {"brightnessLevel": 0}


def test_percent_current_none_returns_empty():
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), ATTR_PERCENT_CURRENT, None) == {}


def test_percent_setting_attribute_returns_empty_on_update():
    # PercentSetting is a write target; reads on PercentCurrent. An update
    # event for 0x0002 on a matterFan device should produce no state change.
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), ATTR_PERCENT_SETTING, 60) == {}


def test_unknown_attribute_returns_empty():
    h = FanControlHandler()
    assert h.on_attribute_update(_fan_dev(), 0x9999, 42) == {}


# ---------------------------------------------------------------------------
# on_attribute_update — matterThermostat (regression guard)
# ---------------------------------------------------------------------------

def test_fan_mode_on_thermostat_dev_maps_hvacfanmode(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    thermo_dev = SimpleNamespace(deviceTypeId="matterThermostat")
    assert h.on_attribute_update(thermo_dev, ATTR_FAN_MODE, FAN_AUTO) == {
        "hvacFanMode": indigo.kFanMode.Auto
    }
    assert h.on_attribute_update(thermo_dev, ATTR_FAN_MODE, FAN_ON) == {
        "hvacFanMode": indigo.kFanMode.AlwaysOn
    }


def test_percent_current_on_thermostat_dev_returns_empty():
    # PercentCurrent updates on a thermostat device should be ignored.
    h = FanControlHandler()
    thermo_dev = SimpleNamespace(deviceTypeId="matterThermostat")
    assert h.on_attribute_update(thermo_dev, ATTR_PERCENT_CURRENT, 60) == {}


# ---------------------------------------------------------------------------
# handle_indigo_action — matterFan actions
# ---------------------------------------------------------------------------

def _fan_action_dev(on=False, brightness=0):
    return SimpleNamespace(
        deviceTypeId="matterFan",
        pluginProps={"nodeId": "50", "endpointId": "1"},
        onState=on,
        brightness=brightness,
    )


def test_action_turn_on_writes_fan_mode_on(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev(on=False)
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOn)
    w = h.handle_indigo_action(dev, action)
    assert isinstance(w, MatterWrite)
    assert (w.node_id, w.endpoint, w.cluster, w.attribute, w.value) == (
        50, 1, CLUSTER_FAN_CONTROL, ATTR_FAN_MODE, FAN_ON
    )


def test_action_turn_off_writes_fan_mode_zero(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev(on=True)
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOff)
    w = h.handle_indigo_action(dev, action)
    assert (w.cluster, w.attribute, w.value) == (CLUSTER_FAN_CONTROL, ATTR_FAN_MODE, 0)


def test_action_toggle_from_off_writes_fan_on(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev(on=False)
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.Toggle)
    w = h.handle_indigo_action(dev, action)
    assert w.value == FAN_ON


def test_action_toggle_from_on_writes_fan_off(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev(on=True)
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.Toggle)
    w = h.handle_indigo_action(dev, action)
    assert w.value == 0


def test_action_set_brightness_writes_percent_setting(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev()
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetBrightness, actionValue=70)
    w = h.handle_indigo_action(dev, action)
    assert (w.cluster, w.attribute, w.value) == (CLUSTER_FAN_CONTROL, ATTR_PERCENT_SETTING, 70)


def test_action_brighten_by_clamps_to_100(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev(brightness=90)
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.BrightenBy, actionValue=20)
    w = h.handle_indigo_action(dev, action)
    assert w.value == 100


def test_action_dim_by_clamps_to_zero(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev(brightness=10)
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.DimBy, actionValue=30)
    w = h.handle_indigo_action(dev, action)
    assert w.value == 0


def test_action_unknown_returns_none(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    dev = _fan_action_dev()
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.RequestStatus)
    assert h.handle_indigo_action(dev, action) is None


def test_action_on_thermostat_dev_returns_none(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    thermo_dev = SimpleNamespace(
        deviceTypeId="matterThermostat",
        pluginProps={"nodeId": "30", "endpointId": "1"},
        onState=False,
        brightness=0,
    )
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOn)
    assert h.handle_indigo_action(thermo_dev, action) is None


# ---------------------------------------------------------------------------
# Registry: handler_for_device finds FanControlHandler for matterFan
# ---------------------------------------------------------------------------

def test_registry_handler_for_matterfan_device():
    reg = HandlerRegistry()

    class FanDev:
        deviceTypeId = "matterFan"

    assert isinstance(reg.handler_for_device(FanDev()), FanControlHandler)


def test_device_type_id_set_on_handler():
    assert FanControlHandler().device_type_id == "matterFan"
