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
    # device that advertises white temperature (states include the key)
    dev = SimpleNamespace(states={"whiteTemperature": 0})
    out = h.on_attribute_update(dev, 0x0007, 370)
    assert out == {"whiteTemperature": round(1_000_000 / 370)}


def test_color_temp_skipped_when_device_lacks_white_temp_state():
    # a device with no whiteTemperature state must not be sent the key (Indigo
    # rejects it as an invalid color level) — degrade quietly instead.
    h = ColorControlHandler()
    assert h.on_attribute_update(SimpleNamespace(states={}), 0x0007, 370) == {}


def test_color_spec_sets_color_support_props():
    node = parse_node(COLOR_NODE, "RGB Lamp")
    spec = HandlerRegistry().handlers_for_endpoint(node, _ep(node, 1))[0]
    assert spec.props["SupportsColor"] is True
    assert spec.props["SupportsRGB"] is True
    assert spec.props["SupportsWhite"] is True
    assert spec.props["SupportsWhiteTemperature"] is True
    assert spec.props["WhiteTemperatureMin"] == 2000
    assert spec.props["WhiteTemperatureMax"] == 6500


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


# ---------------------------------------------------------------------------
# Edge cases surfaced by review
# ---------------------------------------------------------------------------
def test_kelvin_to_mireds_zero_guard():
    assert kelvin_to_mireds(0) == 0


def test_set_color_falls_back_to_device_states_when_no_rgb(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"},
                          states={"redLevel": 100, "greenLevel": 0, "blueLevel": 0})
    cmd = h.handle_indigo_action(dev, SimpleNamespace(
        deviceAction=indigo.kDeviceAction.SetColorLevels, actionValue={}))
    assert cmd.command == "MoveToHueAndSaturation"
    assert cmd.args["saturation"] == 254  # pure red from cached state


def test_brighten_and_dim_clamp_to_range(mock_indigo_base):
    import indigo
    h = LevelControlHandler()
    up = h.handle_indigo_action(
        SimpleNamespace(pluginProps={"nodeId": "8", "endpointId": "1"}, brightness=80),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.BrightenBy, actionValue=50))
    assert up.args["level"] == pct_to_level(100)  # clamped at 100
    down = h.handle_indigo_action(
        SimpleNamespace(pluginProps={"nodeId": "8", "endpointId": "1"}, brightness=20),
        SimpleNamespace(deviceAction=indigo.kDeviceAction.DimBy, actionValue=50))
    assert down.args["level"] == pct_to_level(0)  # clamped at 0


# ---------------------------------------------------------------------------
# fix/#60 — XY fallback when the device lacks the optional HueSaturation feature
# ---------------------------------------------------------------------------

def test_xy_only_device_gets_move_to_color(mock_indigo_base):
    """An XY+CT light (caps 0x18 — the spec-minimum Extended Color Light,
    e.g. matter.js's ExtendedColorLightDevice) must get MoveToColor, never
    MoveToHueAndSaturation (which it rejects with UNSUPPORTED_COMMAND)."""
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"},
                          states={"colorCapabilities": 0x18})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"redLevel": 100, "greenLevel": 0, "blueLevel": 0})
    cmd = h.handle_indigo_action(dev, action)
    assert cmd.command == "MoveToColor"
    # sRGB red sits around CIE (0.64, 0.33)
    assert abs(cmd.args["colorX"] / 65536 - 0.64) < 0.01
    assert abs(cmd.args["colorY"] / 65536 - 0.33) < 0.01
    assert 0 <= cmd.args["colorX"] <= 0xFEFF and 0 <= cmd.args["colorY"] <= 0xFEFF


def test_hs_capable_device_keeps_hue_sat(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"},
                          states={"colorCapabilities": 0x19})  # HS + XY + CT
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"redLevel": 0, "greenLevel": 0, "blueLevel": 100})
    cmd = h.handle_indigo_action(dev, action)
    assert cmd.command == "MoveToHueAndSaturation"


def test_unknown_capabilities_keeps_historical_hs(mock_indigo_base):
    """Capabilities not yet primed (state absent/0) → historical behaviour."""
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"}, states={})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"redLevel": 0, "greenLevel": 100, "blueLevel": 0})
    cmd = h.handle_indigo_action(dev, action)
    assert cmd.command == "MoveToHueAndSaturation"


def test_color_capabilities_attribute_is_cached_as_state():
    h = ColorControlHandler()
    dev = SimpleNamespace(states={"colorCapabilities": 0})
    assert h.on_attribute_update(dev, 0x400A, 0x18) == {"colorCapabilities": 0x18}


def test_color_capabilities_skipped_when_state_missing():
    """A device whose state list hasn't been refreshed yet (pre-#60 creation)
    must degrade quietly instead of logging an Indigo error per report."""
    h = ColorControlHandler()
    assert h.on_attribute_update(SimpleNamespace(states={}), 0x400A, 0x18) == {}


def test_xy_attribute_updates_recompute_rgb():
    h = ColorControlHandler()
    dev = SimpleNamespace(states={"colorX": 0, "colorY": 0, "brightnessLevel": 100,
                                  "redLevel": 0, "greenLevel": 0, "blueLevel": 0})
    # X arrives first — stored, no RGB yet (no Y)
    out = h.on_attribute_update(dev, 0x0003, 41942)  # ~0.64
    assert out["colorX"] == 41942 and "redLevel" not in out
    dev.states.update(out)
    # Y arrives — both known, RGB recomputed: should be strongly red
    out = h.on_attribute_update(dev, 0x0004, 21626)  # ~0.33
    assert out["colorY"] == 21626
    assert out["redLevel"] > 90 and out["blueLevel"] < 20


def test_xy_update_skipped_when_states_missing():
    """Pre-#60 device without colorX/colorY states degrades quietly."""
    h = ColorControlHandler()
    out = h.on_attribute_update(SimpleNamespace(states={"redLevel": 0}), 0x0003, 41942)
    assert out == {}


def test_rgb_xy_round_trip_primaries():
    from matter_handlers.color_control import matter_xy_to_rgb, rgb_to_matter_xy
    for rgb in ((100, 0, 0), (0, 100, 0), (0, 0, 100), (100, 100, 100)):
        x, y = rgb_to_matter_xy(*rgb)
        back = matter_xy_to_rgb(x, y)
        # Chromaticity round-trip: dominant channel survives
        assert max(range(3), key=lambda i: back[i]) == max(range(3), key=lambda i: rgb[i]) or rgb == (100, 100, 100)


def test_black_rgb_maps_to_white_point():
    from matter_handlers.color_control import rgb_to_matter_xy
    x, y = rgb_to_matter_xy(0, 0, 0)
    assert abs(x / 65536 - 0.3127) < 0.01 and abs(y / 65536 - 0.3290) < 0.01


# ---------------------------------------------------------------------------
# W slider (whiteLevel) — Matter has no white channel; white mode IS CT mode
# ---------------------------------------------------------------------------

def _color_dev(states=None):
    from unittest.mock import MagicMock
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"},
                          states=states or {})
    dev.updateStatesOnServer = MagicMock()
    return dev


def test_white_level_slider_becomes_ct_plus_level(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = _color_dev({"whiteTemperature": 4000})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"whiteLevel": 30, "redLevel": 0,
                                          "greenLevel": 0, "blueLevel": 0})
    commands = h.handle_indigo_action(dev, action)
    assert isinstance(commands, list) and len(commands) == 2
    ct, level = commands
    assert ct.command == "MoveToColorTemperature"
    assert ct.args["colorTemperatureMireds"] == round(1_000_000 / 4000)  # stored temp
    assert level.command == "MoveToLevelWithOnOff" and level.cluster == 0x0008
    assert level.args["level"] == round(30 * 254 / 100)
    # Optimistic echo: W slider has no Matter attribute to report back.
    kv = {item["key"]: item["value"] for item in dev.updateStatesOnServer.call_args[0][0]}
    assert kv["whiteLevel"] == 30 and kv["redLevel"] == 0


def test_white_level_zero_turns_white_off(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = _color_dev()
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"whiteLevel": 0})
    commands = h.handle_indigo_action(dev, action)
    assert commands[-1].command == "MoveToLevelWithOnOff"
    assert commands[-1].args["level"] == 0


def test_white_temp_without_level_stays_single_command(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = _color_dev()
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"whiteTemperature": 2700})
    cmd = h.handle_indigo_action(dev, action)
    assert not isinstance(cmd, list)
    assert cmd.command == "MoveToColorTemperature"


def test_rgb_request_zeroes_white_level_optimistically(mock_indigo_base):
    import indigo
    h = ColorControlHandler()
    dev = _color_dev({"colorCapabilities": 0x18})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"redLevel": 100, "greenLevel": 0,
                                          "blueLevel": 0, "whiteLevel": 0})
    cmd = h.handle_indigo_action(dev, action)
    assert cmd.command == "MoveToColor"
    kv = {item["key"]: item["value"] for item in dev.updateStatesOnServer.call_args[0][0]}
    assert kv == {"whiteLevel": 0}


def test_optimistic_echo_skipped_on_devices_without_updater(mock_indigo_base):
    """Test doubles / odd device objects without updateStatesOnServer must not
    break the command path."""
    import indigo
    h = ColorControlHandler()
    dev = SimpleNamespace(pluginProps={"nodeId": "9", "endpointId": "1"}, states={})
    action = SimpleNamespace(deviceAction=indigo.kDeviceAction.SetColorLevels,
                             actionValue={"whiteLevel": 50})
    commands = h.handle_indigo_action(dev, action)
    assert commands[-1].args["level"] == round(50 * 254 / 100)
