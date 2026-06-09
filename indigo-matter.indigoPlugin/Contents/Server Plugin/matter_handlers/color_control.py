"""ColorControl cluster (0x0300) → Indigo color Dimmer.

Owns an endpoint that has OnOff + LevelControl + ColorControl. Inherits on/off and
brightness from :class:`LevelControlHandler` and adds colour.

Indigo expresses colour as redLevel/greenLevel/blueLevel (0–100) plus
whiteTemperature (Kelvin); Matter uses Hue/Saturation (0–254 each) or
ColorTemperatureMireds. ``colorsys`` bridges RGB ↔ HSV.

NOTE: colour mapping is implemented to spec but, unlike OnOff/brightness, is not
yet validated against a real device (the matter.js examples ship no colour
light). The conversions are unit-tested.
"""
from __future__ import annotations

import colorsys
from typing import Any, Optional

from .base import IndigoDeviceSpec, MatterCommand
from .level_control import LevelControlHandler

CLUSTER_COLOR_CONTROL = 0x0300


def rgb_to_matter_hs(red: float, green: float, blue: float) -> tuple[int, int]:
    """Indigo RGB levels (0–100) → Matter (hue, saturation) each 0–254."""
    hue, sat, _value = colorsys.rgb_to_hsv(red / 100.0, green / 100.0, blue / 100.0)
    return round(hue * 254), round(sat * 254)


def matter_hs_to_rgb(hue: int, sat: int, value: float = 1.0) -> tuple[int, int, int]:
    """Matter (hue, saturation) 0–254 → Indigo RGB levels (0–100)."""
    red, green, blue = colorsys.hsv_to_rgb(hue / 254.0, sat / 254.0, value)
    return round(red * 100), round(green * 100), round(blue * 100)


def mireds_to_kelvin(mireds: int) -> int:
    return round(1_000_000 / mireds) if mireds else 0


def kelvin_to_mireds(kelvin: float) -> int:
    return round(1_000_000 / kelvin) if kelvin else 0


class ColorControlHandler(LevelControlHandler):
    cluster_id = 0x0300
    cluster_name = "ColorControl"
    device_type_id = "matterColorDimmer"

    ATTR_CURRENT_HUE = 0x0000
    ATTR_CURRENT_SATURATION = 0x0001
    ATTR_COLOR_TEMP_MIREDS = 0x0007
    ATTR_COLOR_MODE = 0x0008

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return True  # ColorControl is the richest handler for the endpoint

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props={
                    "nodeId": str(node.node_id),
                    "endpointId": str(endpoint.endpoint_id),
                    "vendorName": node.vendor_name,
                    "productName": node.product_name,
                    # Color support must be set as device props at creation: Indigo
                    # does not apply the static <Supports*> Devices.xml elements to
                    # API-created devices, so without these the device has no real
                    # white-temperature support and rejects 'whiteTemperature' as an
                    # invalid color level key. (Matches the Indigo SDK's
                    # Relay-and-Dimmer example, which sets these via device props.)
                    "SupportsColor": True,
                    "SupportsRGB": True,
                    "SupportsWhite": True,
                    "SupportsWhiteTemperature": True,
                    "SupportsRGBandWhiteSimultaneously": False,
                    "WhiteTemperatureMin": 2000,
                    "WhiteTemperatureMax": 6500,
                },
                initial_states={"onOffState": False, "brightnessLevel": 0},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_CURRENT_HUE, self.ATTR_CURRENT_SATURATION, self.ATTR_COLOR_TEMP_MIREDS]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if value is None:
            return {}
        if attribute_id == self.ATTR_COLOR_TEMP_MIREDS:
            # whiteTemperature is only a valid color level when the device actually
            # advertises it (via the color-support props above). Guard so a
            # mis-provisioned device degrades quietly instead of erroring on every
            # update (mirrors the 'if channel in dev.states' SDK pattern).
            if "whiteTemperature" not in getattr(indigo_dev, "states", {}):
                return {}
            return {"whiteTemperature": mireds_to_kelvin(int(value))}
        if attribute_id not in (self.ATTR_CURRENT_HUE, self.ATTR_CURRENT_SATURATION):
            return {}
        # Hue/sat arrive one at a time; combine with the device's current RGB to
        # keep the other component and the brightness (value) coherent.
        states = getattr(indigo_dev, "states", {}) or {}
        red = float(states.get("redLevel", 0) or 0)
        green = float(states.get("greenLevel", 0) or 0)
        blue = float(states.get("blueLevel", 0) or 0)
        hue, sat, val = colorsys.rgb_to_hsv(red / 100.0, green / 100.0, blue / 100.0)
        if attribute_id == self.ATTR_CURRENT_HUE:
            hue = int(value) / 254.0
        else:
            sat = int(value) / 254.0
        new_red, new_green, new_blue = colorsys.hsv_to_rgb(hue, sat, val if val > 0 else 1.0)
        return {
            "redLevel": round(new_red * 100),
            "greenLevel": round(new_green * 100),
            "blueLevel": round(new_blue * 100),
        }

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        if action.deviceAction == indigo.kDeviceAction.SetColorLevels:
            return self._set_color(indigo_dev, action.actionValue)
        return super().handle_indigo_action(indigo_dev, action)

    def _set_color(self, indigo_dev: Any, values: dict) -> MatterCommand:
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        white_temp = values.get("whiteTemperature")
        if white_temp:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id, cluster=self.cluster_id,
                command="MoveToColorTemperature",
                args={"colorTemperatureMireds": kelvin_to_mireds(float(white_temp)),
                      "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0},
            )
        states = getattr(indigo_dev, "states", {}) or {}
        red = float(values.get("redLevel", states.get("redLevel", 0)) or 0)
        green = float(values.get("greenLevel", states.get("greenLevel", 0)) or 0)
        blue = float(values.get("blueLevel", states.get("blueLevel", 0)) or 0)
        hue, sat = rgb_to_matter_hs(red, green, blue)
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id, cluster=self.cluster_id,
            command="MoveToHueAndSaturation",
            args={"hue": hue, "saturation": sat,
                  "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0},
        )
