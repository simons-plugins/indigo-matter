"""M5: LevelControl (dimmer) and ColorControl (colour dimmer) handlers."""
from __future__ import annotations

from types import SimpleNamespace

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.level_control import LevelControlHandler, level_to_pct, pct_to_level
from matter_handlers.color_control import (
    ColorControlHandler,
    kelvin_to_mireds,
    matter_hs_to_rgb,
    mireds_to_kelvin,
    rgb_to_matter_hs,
)

DIMMER_NODE = {
    "node_id": 8,
    "attributes": {
        "1/6/0": False, "1/8/0": 254, "1/3/0": 0,
        "1/29/0": [{"0": 257, "1": 2}],  # DimmableLight
    },
}
COLOR_NODE = {
    "node_id": 9,
    "attributes": {
        "1/6/0": False, "1/8/0": 254, "1/768/0": 0, "1/768/1": 0, "1/768/8": 0,
        "1/29/0": [{"0": 269, "1": 1}],  # ExtendedColorLight=269
    },
}


def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


# ---------------------------------------------------------------------------
# Registry routing
# ---------------------------------------------------------------------------
def test_dimmer_endpoint_makes_matter_dimmer():
    node = parse_node(DIMMER_NODE, "Lamp")
    specs = HandlerRegistry().handlers_for_endpoint(node, _ep(node, 1))
    assert [s.device_type_id for s in specs] == ["matterDimmer"]


def test_color_endpoint_makes_color_dimmer_only():
    node = parse_node(COLOR_NODE, "RGB Lamp")
    specs = HandlerRegistry().handlers_for_endpoint(node, _ep(node, 1))
    # ColorControl wins; LevelControl/OnOff defer → exactly one device
    assert [s.device_type_id for s in specs] == ["matterColorDimmer"]


def test_device_type_lookup():
    reg = HandlerRegistry()
    assert isinstance(reg.handler_for_device(SimpleNamespace(deviceTypeId="matterDimmer")), LevelControlHandler)
    assert isinstance(reg.handler_for_device(SimpleNamespace(deviceTypeId="matterColorDimmer")), ColorControlHandler)


# ---------------------------------------------------------------------------
# Brightness scaling
# ---------------------------------------------------------------------------
def test_level_pct_scaling():
    assert level_to_pct(254) == 100
    assert level_to_pct(0) == 0
    assert level_to_pct(127) == 50
    assert pct_to_level(100) == 254
    assert pct_to_level(0) == 0
    assert pct_to_level(50) == 127


def test_level_attribute_to_brightness():
    h = LevelControlHandler()
    assert h.on_attribute_update(None, 0x0000, 254) == {"brightnessLevel": 100}
    assert h.on_attribute_update(None, 0x0000, 0) == {"brightnessLevel": 0}
    assert h.on_attribute_update(None, 0x0000, None) == {}


def test_dimmer_actions(mock_indigo_base):
    import indigo
    h = LevelControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "8", "endpointId": "1"}, brightness=40)

    on = h.handle_indigo_action(dev, SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOn))
    assert on.cluster == 0x0006 and on.command == "On"

    setb = h.handle_indigo_action(dev, SimpleNamespace(deviceAction=indigo.kDeviceAction.SetBrightness, actionValue=50))
    assert setb.cluster == 0x0008 and setb.command == "MoveToLevelWithOnOff"
    assert setb.args["level"] == 127

    brighten = h.handle_indigo_action(dev, SimpleNamespace(deviceAction=indigo.kDeviceAction.BrightenBy, actionValue=10))
    assert brighten.args["level"] == pct_to_level(50)
    dim = h.handle_indigo_action(dev, SimpleNamespace(deviceAction=indigo.kDeviceAction.DimBy, actionValue=10))
    assert dim.args["level"] == pct_to_level(30)


# ---------------------------------------------------------------------------
# Colour conversions + handler
# ---------------------------------------------------------------------------
def test_color_conversions_round_trip():
    # pure red
    hue, sat = rgb_to_matter_hs(100, 0, 0)
    assert sat == 254 and hue == 0
    r, g, b = matter_hs_to_rgb(hue, sat, 1.0)
    assert (r, g, b) == (100, 0, 0)
    assert mireds_to_kelvin(370) == round(1_000_000 / 370)
    assert kelvin_to_mireds(2700) == round(1_000_000 / 2700)
    assert mireds_to_kelvin(0) == 0


def test_color_temp_attribute_to_white_temperature():
    h = ColorControlHandler()
    out = h.on_attribute_update(SimpleNamespace(states={}), 0x0007, 370)
    assert out == {"whiteTemperature": round(1_000_000 / 370)}


def test_color_hue_update_recomputes_rgb():
    h = ColorControlHandler()
    dev = SimpleNamespace(states={"redLevel": 100, "greenLevel": 0, "blueLevel": 0})
    # push saturation to full + a hue → produces a non-red RGB
    out = h.on_attribute_update(dev, 0x0000, 85)  # hue ~ 120deg (green) region
    assert set(out) == {"redLevel", "greenLevel", "blueLevel"}


def test_set_color_levels_builds_hue_sat_command(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"}, states={})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"redLevel": 0, "greenLevel": 0, "blueLevel": 100})
    cmd = h.handle_indigo_action(dev, action)
    assert cmd.cluster == 0x0300 and cmd.command == "MoveToHueAndSaturation"
    assert cmd.args["saturation"] == 254  # fully saturated blue


def test_set_color_temperature_command(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"}, states={})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"whiteTemperature": 2700})
    cmd = h.handle_indigo_action(dev, action)
    assert cmd.command == "MoveToColorTemperature"
    assert cmd.args["colorTemperatureMireds"] == round(1_000_000 / 2700)


def test_color_dimmer_inherits_onoff_and_brightness(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"}, brightness=0, states={})
    on = h.handle_indigo_action(dev, SimpleNamespace(deviceAction=indigo.kDeviceAction.TurnOn))
    assert on.cluster == 0x0006 and on.command == "On"
    setb = h.handle_indigo_action(dev, SimpleNamespace(deviceAction=indigo.kDeviceAction.SetBrightness, actionValue=100))
    assert setb.cluster == 0x0008 and setb.args["level"] == 254
