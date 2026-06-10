"""WindowCovering cluster (0x0102) handler tests.

Covers:
  - Lift percent100ths ↔ brightness inversion (0/5000/10000 ↔ 100/50/0)
  - None value handling (nullable attribute → no state update)
  - All supported Indigo action mappings
  - Device spec creation (nodeId, endpointId, vendorName, productName)
  - Registry routing: a window-covering endpoint produces matterWindowCovering
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.window_covering import (
    ATTR_CURRENT_LIFT_PERCENT100THS,
    CLUSTER_WINDOW_COVERING,
    WindowCoveringHandler,
    brightness_to_lift_pct100ths,
    lift_pct100ths_to_brightness,
)

# ---------------------------------------------------------------------------
# Minimal node fixture: WindowCovering cluster (0x0102) on endpoint 1.
# ---------------------------------------------------------------------------
WINDOW_COVERING_NODE = {
    "node_id": 20,
    "attributes": {
        "0/40/1": "Eve Systems",         # BasicInformation.VendorName
        "0/40/2": 4874,                  # VendorID
        "0/40/3": "Eve MotionBlinds",    # ProductName
        "1/29/0": [{"0": 515, "1": 2}],  # Descriptor.DeviceTypeList (WindowCovering=515)
        "1/258/0": 0,                    # WindowCovering cluster (0x0102=258) present
        f"1/{CLUSTER_WINDOW_COVERING}/{ATTR_CURRENT_LIFT_PERCENT100THS}": 0,
    },
}


def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


# ---------------------------------------------------------------------------
# Inversion helpers
# ---------------------------------------------------------------------------
def test_fully_open_lift_zero_to_brightness_100():
    """Matter 0 (fully open) → Indigo brightness 100."""
    assert lift_pct100ths_to_brightness(0) == 100


def test_midpoint_lift_5000_to_brightness_50():
    """Matter 5000 (half-closed) → Indigo brightness 50."""
    assert lift_pct100ths_to_brightness(5000) == 50


def test_fully_closed_lift_10000_to_brightness_0():
    """Matter 10000 (fully closed) → Indigo brightness 0."""
    assert lift_pct100ths_to_brightness(10000) == 0


def test_brightness_100_to_lift_0():
    """Indigo brightness 100 (fully open) → Matter 0."""
    assert brightness_to_lift_pct100ths(100) == 0


def test_brightness_50_to_lift_5000():
    """Indigo brightness 50 (half-open) → Matter 5000."""
    assert brightness_to_lift_pct100ths(50) == 5000


def test_brightness_0_to_lift_10000():
    """Indigo brightness 0 (fully closed) → Matter 10000."""
    assert brightness_to_lift_pct100ths(0) == 10000


# ---------------------------------------------------------------------------
# Attribute update
# ---------------------------------------------------------------------------
def test_attribute_update_fully_open():
    h = WindowCoveringHandler()
    out = h.on_attribute_update(None, ATTR_CURRENT_LIFT_PERCENT100THS, 0)
    assert out == {"brightnessLevel": 100, "onOffState": True}


def test_attribute_update_fully_closed():
    h = WindowCoveringHandler()
    out = h.on_attribute_update(None, ATTR_CURRENT_LIFT_PERCENT100THS, 10000)
    assert out == {"brightnessLevel": 0, "onOffState": False}


def test_attribute_update_midpoint():
    h = WindowCoveringHandler()
    out = h.on_attribute_update(None, ATTR_CURRENT_LIFT_PERCENT100THS, 5000)
    assert out == {"brightnessLevel": 50, "onOffState": True}


def test_attribute_update_none_returns_empty():
    """Nullable attribute arriving as None must not blow up or produce stale state."""
    h = WindowCoveringHandler()
    out = h.on_attribute_update(None, ATTR_CURRENT_LIFT_PERCENT100THS, None)
    assert out == {}


def test_attribute_update_unknown_attr_returns_empty():
    h = WindowCoveringHandler()
    assert h.on_attribute_update(None, 0x9999, 5000) == {}


# ---------------------------------------------------------------------------
# Action mapping
# ---------------------------------------------------------------------------

class _Dev:
    pluginProps = {"nodeId": "20", "endpointId": "1"}
    brightness = 60


def test_turn_on_sends_up_or_open(mock_indigo_base):
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(_Dev(), SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOn))
    assert cmd is not None
    assert cmd.node_id == 20 and cmd.endpoint == 1
    assert cmd.cluster == CLUSTER_WINDOW_COVERING
    assert cmd.command == "UpOrOpen"
    assert cmd.args == {}


def test_turn_off_sends_down_or_close(mock_indigo_base):
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(_Dev(), SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOff))
    assert cmd is not None
    assert cmd.command == "DownOrClose"
    assert cmd.args == {}


def test_set_brightness_sends_go_to_lift_percentage(mock_indigo_base):
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.SetBrightness, actionValue=75),
    )
    assert cmd is not None
    assert cmd.command == "GoToLiftPercentage"
    # brightness 75 → lift = (100 - 75) * 100 = 2500
    assert cmd.args == {"liftPercent100thsValue": 2500}


def test_set_brightness_zero_sends_fully_closed(mock_indigo_base):
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.SetBrightness, actionValue=0),
    )
    assert cmd.args == {"liftPercent100thsValue": 10000}


def test_set_brightness_100_sends_fully_open(mock_indigo_base):
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.SetBrightness, actionValue=100),
    )
    assert cmd.args == {"liftPercent100thsValue": 0}


def test_brighten_by_relative(mock_indigo_base):
    """BrightenBy adds to current brightness and clamps at 100."""
    import indigo
    h = WindowCoveringHandler()
    # _Dev.brightness = 60; brighten by 20 → 80 → lift 2000
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.BrightenBy, actionValue=20),
    )
    assert cmd.command == "GoToLiftPercentage"
    assert cmd.args == {"liftPercent100thsValue": brightness_to_lift_pct100ths(80)}


def test_brighten_by_clamps_at_100(mock_indigo_base):
    """BrightenBy cannot exceed 100 (fully open)."""
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.BrightenBy, actionValue=50),
    )
    # 60 + 50 = 110 → clamp to 100 → lift 0
    assert cmd.args == {"liftPercent100thsValue": 0}


def test_dim_by_relative(mock_indigo_base):
    """DimBy subtracts from current brightness and clamps at 0."""
    import indigo
    h = WindowCoveringHandler()
    # _Dev.brightness = 60; dim by 20 → 40 → lift 6000
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.DimBy, actionValue=20),
    )
    assert cmd.command == "GoToLiftPercentage"
    assert cmd.args == {"liftPercent100thsValue": brightness_to_lift_pct100ths(40)}


def test_dim_by_clamps_at_0(mock_indigo_base):
    """DimBy cannot go below 0 (fully closed)."""
    import indigo
    h = WindowCoveringHandler()
    cmd = h.handle_indigo_action(
        _Dev(),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.DimBy, actionValue=80),
    )
    # 60 - 80 = -20 → clamp to 0 → lift 10000
    assert cmd.args == {"liftPercent100thsValue": 10000}


def test_toggle_returns_none(mock_indigo_base):
    """Toggle has no meaningful window-covering analogue — returns None."""
    import indigo
    h = WindowCoveringHandler()
    assert h.handle_indigo_action(_Dev(), SimpleNamespace(deviceAction=indigo.kDeviceAction.Toggle)) is None


def test_unknown_action_returns_none(mock_indigo_base):
    import indigo
    h = WindowCoveringHandler()
    assert h.handle_indigo_action(_Dev(), SimpleNamespace(deviceAction=indigo.kDeviceAction.RequestStatus)) is None


# ---------------------------------------------------------------------------
# Device spec creation
# ---------------------------------------------------------------------------
def test_create_device_spec_fields():
    node = parse_node(WINDOW_COVERING_NODE, suggested_name="Bedroom Blind")
    ep = _ep(node, 1)
    specs = WindowCoveringHandler().create_indigo_devices(node, ep)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.device_type_id == "matterWindowCovering"
    assert spec.name == "Bedroom Blind"
    assert spec.props["nodeId"] == "20"
    assert spec.props["endpointId"] == "1"
    assert spec.props["vendorName"] == "Eve Systems"
    assert spec.props["productName"] == "Eve MotionBlinds"
    assert spec.initial_states == {}


def test_create_device_spec_falls_back_to_product_name():
    node = parse_node(WINDOW_COVERING_NODE)  # no suggested_name
    ep = _ep(node, 1)
    spec = WindowCoveringHandler().create_indigo_devices(node, ep)[0]
    assert spec.name == "Eve MotionBlinds"


# ---------------------------------------------------------------------------
# Registry routing
# ---------------------------------------------------------------------------
def test_registry_creates_window_covering_device():
    node = parse_node(WINDOW_COVERING_NODE, suggested_name="Living Room Blind")
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, _ep(node, 1))
    device_type_ids = [s.device_type_id for s in specs]
    assert "matterWindowCovering" in device_type_ids


def test_registry_lookup_by_cluster_and_device_type():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_WINDOW_COVERING)
    assert isinstance(handler, WindowCoveringHandler)

    handler_by_type = reg.handler_for_device(SimpleNamespace(deviceTypeId="matterWindowCovering"))
    assert isinstance(handler_by_type, WindowCoveringHandler)


def test_subscribe_list():
    h = WindowCoveringHandler()
    assert ATTR_CURRENT_LIFT_PERCENT100THS in h.attributes_to_subscribe()
