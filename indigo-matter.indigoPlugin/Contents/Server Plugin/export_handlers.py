"""Per-role **outbound** handlers — the mirror image of ``matter_handlers/``.

Inbound, a handler is keyed by Matter *cluster* because that is what the wire
gives us. Outbound there is no cluster: the plugin has an Indigo device and a
user-declared **role** (``export_catalog``/BRIDGE_PROTOCOL §4.2), so the
registry here is keyed by role. That is the whole of PRD §XG7 — "adding a
device-type mapping in future is an isolated, mechanical change": a new role is
a new class plus one line in :data:`HANDLERS`, and the zoo test in
``tests/test_export_handlers.py`` fails until its §4.2 vocabularies are covered
in both directions.

Each handler owns three things, and no more:

* :meth:`ExportHandler.states_for` — the §4.2 ``set_state`` snapshot for a
  device (what the ecosystem should be showing);
* :meth:`ExportHandler.diff` — the *changed* subset of that snapshot, which is
  what ``deviceUpdated`` actually pushes. An empty dict means "nothing to say";
* :meth:`ExportHandler.dispatch` — one §4.2 ``command`` event turned into a real
  ``indigo.*`` call.

**Only the E3 roles live here.** ``doorLock``, ``windowCovering``, the sensors
and ``thermostat`` are E4; the allow-list can already hold them (the §5.1 dialog
offers them as roles) but :func:`handler_for` returns ``None``, and
``export_bridge`` skips those exports with a warning rather than sending the
bridge node a role it cannot serve. That is deliberate: a role the node rejects
fails the **whole** ``attach`` with ``internal`` (E3a), so one E4 export would
otherwise take every E3 export down with it.

**Destructive commands are not implemented here and that is not an oversight.**
``doorLock``'s ``lock``/``unlock`` are E4's, and PRD §7 requires that we never
auto-confirm a destructive state change; the seam for that gate is
:meth:`ExportHandler.dispatch`, where an E4 lock handler must consult the
inbound lock conventions before it calls ``indigo.device.lock``.

Units are Indigo-natural at this boundary (§4.2): brightness and saturation are
0–100 in **both** directions, hue is 0–360, colour temperature is mireds on the
wire and Kelvin in Indigo. The node owns every Matter wire conversion.
"""
from __future__ import annotations

import colorsys
import logging
from typing import Any, Callable, Optional

import indigo  # provided by the Indigo runtime

import export_catalog
from matter_handlers.color_control import kelvin_to_mireds, mireds_to_kelvin

#: Handlers are stateless singletons in :data:`HANDLERS` with no plugin logger
#: injected, so the one line they need to say goes to the module logger — the
#: same posture ``export_catalog`` takes. It is debug-only by design: everything
#: a user must act on is logged by ``export_bridge``, which knows the device.
_LOG = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# §4.2 state keys and command names. Spelled once here; the zoo test pins each
# against ``bridge_protocol.ROLE_STATE_KEYS`` / ``ROLE_COMMANDS`` so a typo is a
# test failure rather than a silently ignored key on the node.
# --------------------------------------------------------------------------
STATE_ON_OFF = "onOff"
STATE_LEVEL = "level"
STATE_COLOR_TEMP_MIREDS = "colorTempMireds"
STATE_HUE = "hue"
STATE_SATURATION = "saturation"

COMMAND_ON_OFF = "onOff"
COMMAND_SET_LEVEL = "setLevel"
COMMAND_SET_COLOR_TEMP = "setColorTemp"
COMMAND_SET_COLOR = "setColor"

#: §4.2 bounds for ``colorTempMireds``. The node clamps too, but sending a value
#: outside the declared domain would be *us* breaking the contract — and a
#: device reporting ``whiteTemperature`` 0 (the "unknown" spelling several
#: plugins use) converts to an infinite mired value if left unguarded.
MIREDS_MIN = 153
MIREDS_MAX = 500

#: Hue tolerance, in degrees, below which :meth:`ExportHandler.diff` treats a
#: change as noise. The node converts 0–360 to Matter's 0–254 hue and back, and
#: 360/254 ≈ 1.4°, so a colour we pushed can come back one degree different
#: through no one's fault. Without this, every ecosystem-originated colour
#: change would echo a spurious ``set_state`` straight back out.
#:
#: Saturation deliberately has NO tolerance: 0–100 → 0–254 → 0–100 is a
#: widening then a narrowing, so it round-trips exactly.
HUE_TOLERANCE_DEGREES = 1

#: Saturation below which ``hue`` is OMITTED from the state snapshot entirely.
#:
#: Indigo stores colour as three integer 0–100 channels, so hue is recovered
#: from them rather than stored — and near the grey axis those integers carry
#: almost no angular information. Measured worst-case round-trip error over all
#: 360 degrees (``rgb → hue,sat → rgb → hue``):
#:
#: ===========  =====  ====  ====  ====  ====  ====  ====  =====
#: saturation       0     1     2     3     5    10    15    20+
#: max hue error  180°   30°   15°   10°    6°    3°    2°     1°
#: ===========  =====  ====  ====  ====  ====  ====  ====  =====
#:
#: :data:`HUE_TOLERANCE_DEGREES` only absorbs the last column, so below 20 every
#: pastel change pushed a ``hue`` the ecosystem never asked for. It converges —
#: ``setColorLevels`` is absolute — but the Home colour wheel visibly jumps
#: while it does. At saturation 0 hue is not merely noisy but undefined (grey
#: reads back as hue 0 whatever was sent), so omitting is the honest answer:
#: §3.4 state maps are partial by design, and an absent key means "not telling
#: you", which is exactly the truth here.
SATURATION_HUE_FLOOR = 20

#: Sentinel for "this key was absent before", which is a change, not a match.
_MISSING = object()


# --------------------------------------------------------------------------
# Colour conversion (pure)
# --------------------------------------------------------------------------
def rgb_to_hue_saturation(red: float, green: float, blue: float) -> tuple[int, int]:
    """Indigo RGB levels (0–100 each) → §4.2 ``(hue 0-360, saturation 0-100)``."""
    hue, saturation, _value = colorsys.rgb_to_hsv(red / 100.0, green / 100.0, blue / 100.0)
    return round(hue * 360) % 360, round(saturation * 100)


def hue_saturation_to_rgb(hue: float, saturation: float) -> tuple[int, int, int]:
    """§4.2 ``(hue, saturation)`` → Indigo RGB levels (0–100 each).

    Value is pinned to 1.0 — full vibrance. Brightness is a separate §4.2 key
    (``level`` → ``brightnessLevel``), so folding it into RGB here would make a
    dim red and a bright dark-red indistinguishable. This matches the inbound
    convention in ``matter_handlers.color_control.matter_xy_to_rgb``.
    """
    red, green, blue = colorsys.hsv_to_rgb((hue % 360) / 360.0,
                                           _clamp(saturation, 0, 100) / 100.0, 1.0)
    return round(red * 100), round(green * 100), round(blue * 100)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(dev: Any, name: str) -> Optional[float]:
    """A numeric device attribute, or ``None`` if it is absent/unset/not a number.

    Indigo's colour attributes are ``None`` on a device that has no colour
    channel, and a MagicMock device (tests, and any exotic proxy) answers every
    attribute truthily — so the type check is load-bearing, not defensive
    padding.
    """
    value = getattr(dev, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
class ExportHandler:
    """One §4.2 role's outbound vocabulary."""

    #: The §4.2 role this handler serves.
    role: str = ""
    #: Per-key diff tolerances; anything absent compares exactly.
    tolerances: dict[str, float] = {}

    # -- state (plugin → node) ------------------------------------------
    def states_for(self, dev: Any) -> dict:
        """The full §4.2 state snapshot for ``dev``.

        Keys whose value the device cannot supply are **omitted**, never
        defaulted: ``set_state`` args are a partial map by design (§3.4), and a
        fabricated 0 would be pushed to every ecosystem as fact.
        """
        raise NotImplementedError

    def diff(self, orig_dev: Any, new_dev: Any) -> dict:
        """The changed §4.2 keys only. Empty dict = nothing to push."""
        before = self.states_for(orig_dev)
        after = self.states_for(new_dev)
        return {
            key: value for key, value in after.items()
            if self._changed(key, before.get(key, _MISSING), value)
        }

    def _changed(self, key: str, before: Any, after: Any) -> bool:
        if before is _MISSING:
            return True
        tolerance = self.tolerances.get(key)
        if tolerance is not None and isinstance(before, (int, float)) \
                and isinstance(after, (int, float)):
            return abs(after - before) > tolerance
        return before != after

    # -- commands (node → plugin) ---------------------------------------
    def commands(self) -> dict[str, Callable[[dict, Any], None]]:
        """``command name → handler``. Subclasses extend, never replace."""
        return {}

    def dispatch(self, command: str, args: dict, dev: Any) -> bool:
        """Run one §4.2 command against ``dev``. ``False`` = not our command.

        The caller logs the ``False`` case: only it knows which device and which
        role were involved, which is the version of that line worth having.
        """
        handler = self.commands().get(command)
        if handler is None:
            return False
        handler(dict(args or {}), dev)
        return True


class OnOffExport(ExportHandler):
    """``onOffPlugInUnit`` / ``onOffLight`` — the whole of the relay export."""

    role = export_catalog.ROLE_ON_OFF_PLUG

    def states_for(self, dev: Any) -> dict:
        return {STATE_ON_OFF: bool(getattr(dev, "onState", False))}

    def commands(self) -> dict[str, Callable[[dict, Any], None]]:
        return {**super().commands(), COMMAND_ON_OFF: self._on_off}

    @staticmethod
    def _on_off(args: dict, dev: Any) -> None:
        if bool(args.get("value")):
            indigo.device.turnOn(dev)
        else:
            indigo.device.turnOff(dev)


class OnOffLightExport(OnOffExport):
    """Same vocabulary as the plug; a different Matter device type on the node."""

    role = export_catalog.ROLE_ON_OFF_LIGHT


class DimmableLightExport(OnOffExport):
    """``dimmableLight`` — adds ``level`` (0–100 both sides, no conversion)."""

    role = export_catalog.ROLE_DIMMABLE_LIGHT

    def states_for(self, dev: Any) -> dict:
        states = super().states_for(dev)
        brightness = _number(dev, "brightness")
        if brightness is not None:
            states[STATE_LEVEL] = int(_clamp(round(brightness), 0, 100))
        return states

    def commands(self) -> dict[str, Callable[[dict, Any], None]]:
        return {**super().commands(), COMMAND_SET_LEVEL: self._set_level}

    @staticmethod
    def _set_level(args: dict, dev: Any) -> None:
        level = args.get(STATE_LEVEL)
        if not isinstance(level, (int, float)) or isinstance(level, bool):
            raise ValueError(f"setLevel without a numeric level: {args!r}")
        indigo.dimmer.setBrightness(dev, value=int(_clamp(round(level), 0, 100)))


class ColorTemperatureLightExport(DimmableLightExport):
    """``colorTemperatureLight`` — adds ``colorTempMireds`` over Indigo's Kelvin."""

    role = export_catalog.ROLE_COLOR_TEMPERATURE_LIGHT

    def states_for(self, dev: Any) -> dict:
        states = super().states_for(dev)
        kelvin = _number(dev, "whiteTemperature")
        if kelvin:  # 0 and None both mean "this device is not telling us"
            states[STATE_COLOR_TEMP_MIREDS] = int(
                _clamp(kelvin_to_mireds(kelvin), MIREDS_MIN, MIREDS_MAX))
        return states

    def commands(self) -> dict[str, Callable[[dict, Any], None]]:
        return {**super().commands(), COMMAND_SET_COLOR_TEMP: self._set_color_temp}

    @staticmethod
    def _set_color_temp(args: dict, dev: Any) -> None:
        """Set white temperature, preserving the white channel's own level.

        ``setColorLevels`` documents whiteTemperature as used *in combination
        with* whiteLevel, so sending the temperature alone risks a driver
        reading whiteLevel as 0 and turning the lamp off — a colour tweak that
        blacks out the room is a worse bug than a colour tweak that misses.

        Three guards, all about not depending on someone else to be careful:

        * **the mireds are clamped here.** The node clamps too, but that is the
          *other process*: an unclamped 1 mired is a 1,000,000 K write and 10000
          mireds is 100 K, both outside Indigo's own 1200–15000 domain, and both
          would reach the driver if the node ever stopped clamping or a command
          arrived from anywhere else. ``round`` rather than ``int`` because
          truncating 369.9 to 369 is a different colour, not a rounding detail;
        * **no white channel means no command.** ``whiteLevel is None`` is real
          Indigo for "this device has no white channel at all" — inventing 100
          for it asks its driver to drive something it does not have. A
          whiteLevel of *0* is different: the channel exists and is off, and
          that IS the blacked-out-room case the default is for;
        * **RGB is zeroed alongside.** Matter's ``colorMode`` is one-of, and the
          node's push side picks hue/saturation over colour temperature when a
          device reports both (``bridge-node/src/endpoints.ts`` ``colorPatch``).
          An RGBW lamp left holding stale RGB levels would therefore report a
          colour we did not set, and the node would believe it over the
          temperature we just wrote. Only touched when the device actually has
          RGB channels — an all-zero RGB write to a CT-only driver is its own
          way to black out the room.
        """
        mireds = args.get(STATE_COLOR_TEMP_MIREDS)
        if not isinstance(mireds, (int, float)) or isinstance(mireds, bool) or not mireds:
            raise ValueError(f"setColorTemp without usable mireds: {args!r}")
        white_level = _number(dev, "whiteLevel")
        if white_level is None:
            _LOG.debug("setColorTemp skipped: device %s has no white channel",
                       getattr(dev, "id", "?"))
            return
        levels: dict[str, int] = {
            "whiteLevel": int(_clamp(round(white_level or 100), 0, 100)),
            "whiteTemperature": mireds_to_kelvin(
                int(_clamp(round(mireds), MIREDS_MIN, MIREDS_MAX))),
        }
        if _number(dev, "redLevel") is not None:
            levels.update(redLevel=0, greenLevel=0, blueLevel=0)
        indigo.dimmer.setColorLevels(dev, **levels)


class ExtendedColorLightExport(ColorTemperatureLightExport):
    """``extendedColorLight`` — adds ``hue``/``saturation`` over Indigo's RGB."""

    role = export_catalog.ROLE_EXTENDED_COLOR_LIGHT
    tolerances = {STATE_HUE: HUE_TOLERANCE_DEGREES}

    def states_for(self, dev: Any) -> dict:
        states = super().states_for(dev)
        red = _number(dev, "redLevel")
        green = _number(dev, "greenLevel")
        blue = _number(dev, "blueLevel")
        if None not in (red, green, blue):
            hue, saturation = rgb_to_hue_saturation(red, green, blue)
            states[STATE_SATURATION] = saturation
            # Below the floor the recovered hue is noise, not colour — see
            # SATURATION_HUE_FLOOR for the measured error table.
            if saturation >= SATURATION_HUE_FLOOR:
                states[STATE_HUE] = hue
        return states

    def commands(self) -> dict[str, Callable[[dict, Any], None]]:
        return {**super().commands(), COMMAND_SET_COLOR: self._set_color}

    @staticmethod
    def _set_color(args: dict, dev: Any) -> None:
        """Set hue/saturation as Indigo RGB levels.

        Ecosystems send ``setColor`` **twice with identical values** (E3a, seen
        live) because Matter carries hue and saturation as separate attribute
        writes. No de-duplication is attempted: ``setColorLevels`` takes
        absolute values, so the second call is a no-op at the lamp, and
        suppressing it on "we already believe that" would silently skip the
        first call too whenever Indigo's belief has drifted from the hardware.
        Idempotent beats clever.

        The white channel is zeroed with the same colour-mode reasoning as
        :meth:`ColorTemperatureLightExport._set_color_temp` — an RGBW lamp
        holding both a colour and a white level reports both, and the node has
        to pick. It picks the colour, so leaving white lit means the lamp and
        the ecosystem disagree about what "the colour" is. Only sent when the
        device has a white channel to zero.
        """
        hue = args.get(STATE_HUE)
        saturation = args.get(STATE_SATURATION)
        for name, value in ((STATE_HUE, hue), (STATE_SATURATION, saturation)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"setColor without a numeric {name}: {args!r}")
        red, green, blue = hue_saturation_to_rgb(hue, saturation)
        levels = {"redLevel": red, "greenLevel": green, "blueLevel": blue}
        if _number(dev, "whiteLevel") is not None:
            levels["whiteLevel"] = 0
        indigo.dimmer.setColorLevels(dev, **levels)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
#: role → handler. **E3 roles only** — see the module docstring on why an
#: unimplemented role must be skipped by the caller rather than sent anyway.
HANDLERS: dict[str, ExportHandler] = {
    handler.role: handler for handler in (
        OnOffExport(),
        OnOffLightExport(),
        DimmableLightExport(),
        ColorTemperatureLightExport(),
        ExtendedColorLightExport(),
    )
}

#: The roles this plugin can actually bridge today.
BRIDGEABLE_ROLES = frozenset(HANDLERS)


def handler_for(role: str) -> Optional[ExportHandler]:
    """The handler for ``role``, or ``None`` if v1 cannot bridge it yet."""
    return HANDLERS.get(role)


def is_bridgeable(role: str) -> bool:
    """True if an allow-list entry with ``role`` can be exported today."""
    return role in HANDLERS
