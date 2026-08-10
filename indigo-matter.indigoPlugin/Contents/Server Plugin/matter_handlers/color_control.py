"""ColorControl cluster (0x0300) → Indigo color Dimmer.

Owns an endpoint that has OnOff + LevelControl + ColorControl. Inherits on/off and
brightness from :class:`LevelControlHandler` and adds colour.

Indigo expresses colour as redLevel/greenLevel/blueLevel (0–100) plus
whiteTemperature (Kelvin); Matter uses Hue/Saturation (0–254 each), CIE xy
(0–65535 each), or ColorTemperatureMireds. ``colorsys`` bridges RGB ↔ HSV;
the sRGB↔xy helpers below bridge RGB ↔ CIE xy.

Which colour command a device accepts is governed by its ColorCapabilities
bitmap (attr 0x400A): HueSaturation is OPTIONAL per the Matter spec — even an
Extended Color Light is only required to implement XY + ColorTemperature
(issue #60, found live: matter.js's ExtendedColorLightDevice rejects
MoveToHueAndSaturation with UNSUPPORTED_COMMAND). The capabilities are cached
in the ``colorCapabilities`` device state and the RGB path picks
MoveToHueAndSaturation or MoveToColor accordingly.
"""
from __future__ import annotations

import colorsys
from typing import Any, Optional

from .base import IndigoDeviceSpec, MatterCommand
from .electrical import CLUSTER_ELECTRICAL_ENERGY, CLUSTER_ELECTRICAL_POWER
from .level_control import LevelControlHandler

CLUSTER_COLOR_CONTROL = 0x0300

# ColorCapabilities (0x400A) bits — Matter spec.
CAP_HUE_SATURATION = 0x01
CAP_XY = 0x08

# Matter encodes CIE x/y as value*65536, capped at 0xFEFF per spec.
_XY_SCALE = 65536
_XY_MAX = 0xFEFF


def rgb_to_matter_hs(red: float, green: float, blue: float) -> tuple[int, int]:
    """Indigo RGB levels (0–100) → Matter (hue, saturation) each 0–254."""
    hue, sat, _value = colorsys.rgb_to_hsv(red / 100.0, green / 100.0, blue / 100.0)
    return round(hue * 254), round(sat * 254)


def matter_hs_to_rgb(hue: int, sat: int, value: float = 1.0) -> tuple[int, int, int]:
    """Matter (hue, saturation) 0–254 → Indigo RGB levels (0–100)."""
    red, green, blue = colorsys.hsv_to_rgb(hue / 254.0, sat / 254.0, value)
    return round(red * 100), round(green * 100), round(blue * 100)


def rgb_to_matter_xy(red: float, green: float, blue: float) -> tuple[int, int]:
    """Indigo RGB levels (0–100) → Matter CIE (x, y) ints (0–0xFEFF).

    Standard sRGB → linear → XYZ (D65) → chromaticity. Black has no
    chromaticity; return D65 white (0.3127, 0.3290) for it.
    """
    def linearise(channel: float) -> float:
        c = max(0.0, min(1.0, channel / 100.0))
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = linearise(red), linearise(green), linearise(blue)
    big_x = 0.4124 * r + 0.3576 * g + 0.1805 * b
    big_y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    big_z = 0.0193 * r + 0.1192 * g + 0.9505 * b
    total = big_x + big_y + big_z
    if total <= 0.0:
        x, y = 0.3127, 0.3290  # D65 white point
    else:
        x, y = big_x / total, big_y / total
    return (min(_XY_MAX, round(x * _XY_SCALE)), min(_XY_MAX, round(y * _XY_SCALE)))


def matter_xy_to_rgb(raw_x: int, raw_y: int, brightness: float = 1.0) -> tuple[int, int, int]:
    """Matter CIE (x, y) ints → Indigo RGB levels (0–100).

    xyY → XYZ → linear sRGB → gamma, normalised so the brightest channel is
    full scale (chromaticity carries no luminance; Indigo brightness lives in
    brightnessLevel, so RGB here encodes the hue at full vibrance scaled by
    ``brightness``).
    """
    x = max(1e-6, raw_x / _XY_SCALE)
    y = max(1e-6, raw_y / _XY_SCALE)
    big_y = 1.0
    big_x = (x / y) * big_y
    big_z = ((1.0 - x - y) / y) * big_y
    r = 3.2406 * big_x - 1.5372 * big_y - 0.4986 * big_z
    g = -0.9689 * big_x + 1.8758 * big_y + 0.0415 * big_z
    b = 0.0557 * big_x - 0.2040 * big_y + 1.0570 * big_z
    peak = max(r, g, b)
    if peak > 0:
        r, g, b = r / peak, g / peak, b / peak

    def delinearise(c: float) -> float:
        c = max(0.0, min(1.0, c))
        return c * 12.92 if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055

    brightness = max(0.0, min(1.0, brightness))
    return (
        round(delinearise(r) * 100 * brightness),
        round(delinearise(g) * 100 * brightness),
        round(delinearise(b) * 100 * brightness),
    )


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
    ATTR_CURRENT_X = 0x0003
    ATTR_CURRENT_Y = 0x0004
    ATTR_COLOR_TEMP_MIREDS = 0x0007
    ATTR_COLOR_MODE = 0x0008
    ATTR_COLOR_CAPABILITIES = 0x400A

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return True  # ColorControl is the richest handler for the endpoint

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        props: dict = {
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
        }
        # Energy support must be set as device props at creation: Indigo does not
        # apply static <Supports*> Devices.xml elements to API-created devices
        # (same lesson as colour support; issue #56). When these
        # props are True, Indigo automatically adds curEnergyLevel / accumEnergyTotal
        # states that ElectricalPowerHandler / ElectricalEnergyHandler then update.
        if endpoint.has(CLUSTER_ELECTRICAL_POWER):
            props["SupportsPowerMeter"] = True
        if endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
            props["SupportsEnergyMeter"] = True
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props=props,
                initial_states={"onOffState": False, "brightnessLevel": 0},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [
            self.ATTR_CURRENT_HUE, self.ATTR_CURRENT_SATURATION,
            self.ATTR_CURRENT_X, self.ATTR_CURRENT_Y,
            self.ATTR_COLOR_TEMP_MIREDS, self.ATTR_COLOR_CAPABILITIES,
        ]

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
        if attribute_id == self.ATTR_COLOR_CAPABILITIES:
            # Cached for handle_indigo_action's HS-vs-XY command choice; primed
            # from the node snapshot at reconcile, refreshed on report. Guarded
            # like the other states: a device whose state list hasn't been
            # refreshed yet (deviceStartComm does that on upgrade) must degrade
            # quietly, not log an Indigo error per report.
            if "colorCapabilities" not in (getattr(indigo_dev, "states", {}) or {}):
                return {}
            return {"colorCapabilities": int(value)}
        if attribute_id in (self.ATTR_CURRENT_X, self.ATTR_CURRENT_Y):
            # XY-mode devices report CIE x/y instead of hue/sat. One axis
            # arrives at a time; combine with the stored other axis. Skip when
            # the colorX/colorY states don't exist (device created before the
            # issue #60 fix added them — degrade quietly, same SDK pattern as
            # whiteTemperature above).
            states = getattr(indigo_dev, "states", {}) or {}
            if "colorX" not in states or "colorY" not in states:
                return {}
            raw_x = int(value) if attribute_id == self.ATTR_CURRENT_X else int(states.get("colorX", 0) or 0)
            raw_y = int(value) if attribute_id == self.ATTR_CURRENT_Y else int(states.get("colorY", 0) or 0)
            updates: dict = {"colorX": raw_x, "colorY": raw_y}
            if raw_x and raw_y:
                brightness = float(states.get("brightnessLevel", 100) or 100) / 100.0
                red, green, blue = matter_xy_to_rgb(raw_x, raw_y, brightness)
                updates.update({"redLevel": red, "greenLevel": green, "blueLevel": blue})
            return updates
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

    @staticmethod
    def _apply_optimistic(indigo_dev: Any, states: dict) -> None:
        """Echo channels Matter can't report back (whiteLevel has no Matter
        attribute — white mode IS colour-temperature mode), so the Indigo UI
        slider doesn't snap back. Cosmetic only: a failure here must never
        block the command (the real state echo arrives from the device for
        everything that has a Matter attribute)."""
        updater = getattr(indigo_dev, "updateStatesOnServer", None)
        if updater is None:
            return
        try:
            updater([{"key": key, "value": value} for key, value in states.items()])
        except Exception:  # noqa: BLE001 - cosmetic echo only
            pass

    def _set_color(self, indigo_dev: Any, values: dict):
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        states = getattr(indigo_dev, "states", {}) or {}
        white_temp = values.get("whiteTemperature")
        white_level = values.get("whiteLevel")
        rgb_requested = any(
            float(values.get(key, 0) or 0) > 0
            for key in ("redLevel", "greenLevel", "blueLevel")
        )

        def ct_command(kelvin: float) -> MatterCommand:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id, cluster=self.cluster_id,
                command="MoveToColorTemperature",
                args={"colorTemperatureMireds": kelvin_to_mireds(kelvin),
                      "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0},
            )

        # The white side of Indigo's colour picker: whiteTemperature is the
        # temperature control, whiteLevel is the W slider (white intensity).
        # Matter colour lights have no separate white channel — white mode IS
        # colour-temperature mode, and intensity is LevelControl — so the W
        # slider becomes CT(current/requested temperature) + MoveToLevelWithOnOff.
        if (white_temp or white_level is not None) and not rgb_requested:
            kelvin = float(white_temp) if white_temp else float(
                states.get("whiteTemperature", 0) or 2700
            )
            commands: list = [ct_command(kelvin)]
            optimistic: dict = {"redLevel": 0, "greenLevel": 0, "blueLevel": 0}
            if white_level is not None:
                commands.append(self._set_level(node_id, endpoint_id, float(white_level)))
                optimistic["whiteLevel"] = round(float(white_level))
            self._apply_optimistic(indigo_dev, optimistic)
            return commands if len(commands) > 1 else commands[0]
        if white_temp:
            return ct_command(float(white_temp))
        red = float(values.get("redLevel", states.get("redLevel", 0)) or 0)
        green = float(values.get("greenLevel", states.get("greenLevel", 0)) or 0)
        blue = float(values.get("blueLevel", states.get("blueLevel", 0)) or 0)
        # HueSaturation is OPTIONAL per spec (issue #60): pick the command from
        # the cached ColorCapabilities bitmap. Unknown capabilities (state
        # absent/0 — pre-fix device or snapshot not yet primed) keep the
        # historical HS behaviour rather than guessing.
        self._apply_optimistic(indigo_dev, {"whiteLevel": 0})
        capabilities = int(states.get("colorCapabilities", 0) or 0)
        if capabilities and not capabilities & CAP_HUE_SATURATION and capabilities & CAP_XY:
            x, y = rgb_to_matter_xy(red, green, blue)
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id, cluster=self.cluster_id,
                command="MoveToColor",
                args={"colorX": x, "colorY": y,
                      "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0},
            )
        hue, sat = rgb_to_matter_hs(red, green, blue)
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id, cluster=self.cluster_id,
            command="MoveToHueAndSaturation",
            args={"hue": hue, "saturation": sat,
                  "transitionTime": 0, "optionsMask": 0, "optionsOverride": 0},
        )
