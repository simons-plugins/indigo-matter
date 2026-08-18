"""Indigo device → exportable Matter role(s), per PRD-indigo-matter-export §5.2.

This is the inverse of the inbound mapping and it is **not symmetric**. Inbound,
Matter tells us what a device is. Outbound, Indigo does not: a `relay` may be a
lamp, a plug, a lock, a valve, a fan or a garage door and nothing in the device
model distinguishes them. So this module answers a narrower question than "what
is it?" — it answers **"which roles may the user legitimately declare for it,
and which is the safest default?"** (§5.2: "the role is user-declared in the
§5.1 detail pane, defaulting to the safest interpretation rather than
guessing"). Roles the v1 bridge cannot honour are simply never offered, and a
device with no honourable role is :class:`Excluded` **with a reason** — XAC9
requires the picker to show excluded devices, not hide them.

Two structural notes:

* **The loop guard is `pluginId`, nothing else** (XNG3/XAC6). A device this
  plugin created is excluded before its type is even looked at. The comparison
  is against a plugin id passed in by the caller, never a device-type-name
  string, because type names are the sort of thing that gets refactored.
* **Type dispatch is by class-name chain**, not ``isinstance``. The Indigo
  module is a MagicMock under test (``tests/conftest.py``) so ``isinstance``
  against ``indigo.RelayDevice`` is not merely wrong there, it raises. Walking
  ``type(dev).__mro__`` for the documented IOM class names works identically
  against real Indigo objects and the test doubles, and it resolves the
  Dimmer-is-a-Relay ambiguity by most-specific-first ordering.

* **:func:`classify` never raises.** It runs over every device in the database
  to build a picker, and Indigo device proxies are live objects — one being
  deleted underneath us can raise from any attribute access. A raising device
  becomes :data:`REASON_DEVICE_ERROR`, not a broken dialog, and the failure is
  fail-*closed*: if we could not even read ``pluginId`` we cannot prove the
  loop guard passed, so the only safe verdict is excluded.

Every role emitted here is in the BRIDGE_PROTOCOL §4.2 enum
(``bridge_protocol.ROLES``); ``tests/test_export_catalog.py`` pins that.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Union

#: Module logger. The catalog is pure and Indigo-free, so it logs through the
#: stdlib root configuration Indigo already installs rather than taking a
#: logger argument on every call site.
_LOG = logging.getLogger(__name__)

#: This plugin's bundle id (``Info.plist`` ``CFBundleIdentifier``). Only a
#: fallback: callers pass ``self.pluginId`` so the guard follows the running
#: plugin rather than a constant that could drift from the bundle.
DEFAULT_PLUGIN_ID = "com.simons-plugins.indigo-matter"

# --------------------------------------------------------------------------
# Roles (BRIDGE_PROTOCOL §4.2)
# --------------------------------------------------------------------------
ROLE_ON_OFF_PLUG = "onOffPlugInUnit"
ROLE_ON_OFF_LIGHT = "onOffLight"
ROLE_DOOR_LOCK = "doorLock"
ROLE_DIMMABLE_LIGHT = "dimmableLight"
ROLE_COLOR_TEMPERATURE_LIGHT = "colorTemperatureLight"
ROLE_EXTENDED_COLOR_LIGHT = "extendedColorLight"
ROLE_WINDOW_COVERING = "windowCovering"
ROLE_OCCUPANCY_SENSOR = "occupancySensor"
ROLE_CONTACT_SENSOR = "contactSensor"
ROLE_WATER_LEAK_DETECTOR = "waterLeakDetector"
ROLE_WATER_FREEZE_DETECTOR = "waterFreezeDetector"
ROLE_RAIN_SENSOR = "rainSensor"
ROLE_TEMPERATURE_SENSOR = "temperatureSensor"
ROLE_HUMIDITY_SENSOR = "humiditySensor"
ROLE_LIGHT_SENSOR = "lightSensor"
ROLE_PRESSURE_SENSOR = "pressureSensor"
ROLE_FLOW_SENSOR = "flowSensor"
ROLE_THERMOSTAT = "thermostat"

#: Human labels for the role picker. Keyed by the §4.2 role name.
#:
#: The occupancy label names PIR/motion deliberately (issue #252). Matter 1.x
#: has **no motion device type** — Occupancy Sensor 0x0107 *is* the Matter
#: export of a PIR, and OccupancySensing's sensor-type bitmap says so — but a
#: user exporting an Indigo motion sensor goes looking for "motion" in the
#: picker, finds nothing, and reads the absence as "my sensor cannot be
#: exported". Measured on live hardware 2026-08-18: Apple Home renders the
#: exported accessory as an Occupancy tile, so the label sets that expectation
#: at export time rather than leaving it to be discovered in Apple afterwards.
ROLE_LABELS = {
    ROLE_ON_OFF_PLUG: "Plug (On/Off Plug-in Unit)",
    ROLE_ON_OFF_LIGHT: "Light (On/Off)",
    ROLE_DOOR_LOCK: "Lock (Door Lock)",
    ROLE_DIMMABLE_LIGHT: "Light (Dimmable)",
    ROLE_COLOR_TEMPERATURE_LIGHT: "Light (Colour temperature)",
    ROLE_EXTENDED_COLOR_LIGHT: "Light (Full colour)",
    ROLE_WINDOW_COVERING: "Window covering",
    ROLE_OCCUPANCY_SENSOR: "Occupancy sensor (PIR/motion)",
    ROLE_CONTACT_SENSOR: "Contact sensor",
    ROLE_WATER_LEAK_DETECTOR: "Water leak sensor",
    ROLE_WATER_FREEZE_DETECTOR: "Freeze sensor",
    ROLE_RAIN_SENSOR: "Rain sensor",
    ROLE_TEMPERATURE_SENSOR: "Temperature sensor",
    ROLE_HUMIDITY_SENSOR: "Humidity sensor",
    ROLE_LIGHT_SENSOR: "Light (lux) sensor",
    ROLE_PRESSURE_SENSOR: "Pressure sensor",
    ROLE_FLOW_SENSOR: "Flow sensor",
    ROLE_THERMOSTAT: "Thermostat",
}

# --------------------------------------------------------------------------
# Exclusion reasons — user-facing strings; they are shown in the picker (XAC9)
# --------------------------------------------------------------------------
REASON_LOOP_GUARD = "created by this plugin (loop guard)"
REASON_FAN = "fan export is v2 (matter.js FanControl is a stub)"
REASON_SPRINKLER = "Matter has no irrigation type; per-zone valve export is v2"
REASON_MULTI_IO = "no coherent single-accessory representation"
REASON_NO_ROLE = "no resolvable Matter role"
REASON_SENSOR_UNITS = "no faithful Matter sensor type"
REASON_SENSOR_NO_VALUE = "sensor reports neither an on/off state nor a value"
REASON_DEVICE_ERROR = "error reading device — see Event Log"

#: Roles a user might reasonably expect and the reason v1 does not offer them
#: (§5.2 "Explicitly not exportable in v1"). Indigo cannot tell us a relay is a
#: valve or a garage door, so these are declined as *roles*, not as device
#: classes — the device itself stays exportable as a plug/light/lock.
EXCLUDED_ROLES = {
    "valve": "valve export is v2 (matter.js ValveConfigurationAndControl is an empty stub)",
    "garageDoor": "garage-door export is v2 (needs the catalog's role/polarity data; "
                  "mis-mapping is a physical-safety issue)",
    "fan": REASON_FAN,
}

#: The binary (on/off) sensor roles. All are offered for any on/off sensor; the
#: name heuristic only picks the *default* (see :func:`_binary_sensor_role`),
#: exactly as the unit heuristic does for the numeric five.
#:
#: Occupancy leads because it is the safe fallback when nothing matches: Matter
#: has no motion device type, so Occupancy Sensor 0x0107 is what an Indigo
#: motion sensor — the commonest on/off sensor by a wide margin — has to be.
BINARY_SENSOR_ROLES = (
    ROLE_OCCUPANCY_SENSOR, ROLE_CONTACT_SENSOR, ROLE_WATER_LEAK_DETECTOR,
    ROLE_WATER_FREEZE_DETECTOR, ROLE_RAIN_SENSOR,
)

#: Name hints → binary role as **regexes**, first match wins (issue #236/#252).
#:
#: Unlike the numeric table this leans on the device's *name*, because that is
#: usually the only evidence there is: a binary sensor publishes one bool and
#: no unit, so nothing but the prose the user wrote distinguishes a leak sensor
#: from a door contact. It sets a **default only** — every role in
#: :data:`BINARY_SENSOR_ROLES` stays selectable — which is what makes a name
#: heuristic acceptable here at all.
#:
#: Order is deliberate. Occupancy needles run first so "Back Door Motion" reads
#: as motion rather than as a door; a name with no motion word ("Front Door")
#: then falls through to contact. Every needle is ``\b``-anchored for the same
#: reason the unit table is: unanchored "rain" matches "Drainage", and "frost"
#: matches "Defrost".
_BINARY_PATTERNS = tuple(
    (role, tuple(re.compile(pattern) for pattern in patterns)) for role, patterns in (
        (ROLE_OCCUPANCY_SENSOR, (r"\bmotion\b", r"\bpir\b", r"\boccupancy\b",
                                 r"\boccupied\b", r"\bpresence\b")),
        (ROLE_WATER_LEAK_DETECTOR, (r"\bleak\b", r"\bflood\b", r"\bwater alarm\b",
                                    r"\bwater sensor\b")),
        (ROLE_WATER_FREEZE_DETECTOR, (r"\bfreeze\b", r"\bfreezing\b", r"\bfrost\b")),
        (ROLE_RAIN_SENSOR, (r"\brain\b",)),
        (ROLE_CONTACT_SENSOR, (r"\bcontact\b", r"\bdoor\b", r"\bwindow\b", r"\bgate\b",
                               r"\breed\b", r"\bopen/closed\b")),
    )
)

#: The numeric sensor roles, in §5.2 table order. All of them are offered for
#: any numeric sensor; the unit heuristic only picks the *default* (see
#: :func:`_numeric_sensor_role`).
NUMERIC_SENSOR_ROLES = (
    ROLE_TEMPERATURE_SENSOR, ROLE_HUMIDITY_SENSOR, ROLE_LIGHT_SENSOR,
    ROLE_PRESSURE_SENSOR, ROLE_FLOW_SENSOR,
)

#: Unit hints → role as **regexes**, first match wins within a tier. Ordered so
#: unambiguous words beat broad ones ("humidity" before "temp", which matches
#: half the sensors in a typical database).
#:
#: Every *prose* needle is anchored with ``\b`` because plain substring
#: matching turned ordinary device names into sensors: "Fluxcapacitor" contains
#: "lux", "Attempt Counter" contains "temp", "Epsilon Meter" contains "psi" and
#: "Overflow Alarm" contains "flow". Symbol needles that begin with punctuation
#: (``°c``, ``%rh``, ``l/min``) cannot carry a leading ``\b`` — there is no word
#: boundary before ``°`` at the start of a string — and do not need one: they
#: are already unambiguous. Indigo has no canonical sensor-unit property, so the
#: hints are gathered from pluginProps, the formatted UI value and, last, the
#: device's own naming — see :func:`_unit_texts`.
_UNIT_PATTERNS = tuple(
    (role, tuple(re.compile(pattern) for pattern in patterns)) for role, patterns in (
        (ROLE_HUMIDITY_SENSOR, (r"%\s?rh\b", r"\brh\s?%", r"\bhumidity\b", r"\bmoisture\b")),
        (ROLE_LIGHT_SENSOR, (r"\blux\b", r"\blx\b", r"\billuminance\b", r"\blight level\b",
                             r"\bluminance\b")),
        (ROLE_PRESSURE_SENSOR, (r"\bk?pa\b", r"\bhpa\b", r"\bmbar\b", r"\bmillibar\b",
                                r"\bpsi\b", r"\binhg\b", r"\bpressure\b")),
        (ROLE_FLOW_SENSOR, (r"\bm3/h", r"m³/h", r"\bl/min\b", r"\blpm\b", r"\bgpm\b",
                            r"\bflow rate\b", r"\bflow\b")),
        (ROLE_TEMPERATURE_SENSOR, (r"°\s?c\b", r"°\s?f\b", r"\bdeg\s?c\b", r"\bdeg\s?f\b",
                                   r"\bcelsius\b", r"\bfahrenheit\b", r"\btemperature\b",
                                   r"\btemp\b")),
    )
)

#: pluginProps keys plugins commonly use to record a sensor's unit.
_UNIT_PROP_KEYS = ("unit", "units", "sensorUnits", "unitOfMeasure", "uiUnits", "sensorType")

#: Device attributes searched for unit hints, **strongest evidence first**.
#: ``name`` sits in its own tier below the rest: a declared unit or a formatted
#: value is a statement about the unit, a name is a coincidence waiting to
#: happen. It is kept rather than dropped because plenty of real Indigo sensors
#: carry their unit nowhere else ("Greenhouse Temperature").
_UNIT_ATTRS_STRONG = ("displayStateValUi", "model", "subModel")
_UNIT_ATTRS_WEAK = ("name",)

# --------------------------------------------------------------------------
# Type dispatch — IOM class names, most specific first
# --------------------------------------------------------------------------
KIND_DIMMER = "DimmerDevice"
KIND_SENSOR = "SensorDevice"
KIND_THERMOSTAT = "ThermostatDevice"
KIND_SPRINKLER = "SprinklerDevice"
KIND_SPEED_CONTROL = "SpeedControlDevice"
KIND_MULTI_IO = "MultiIODevice"
KIND_RELAY = "RelayDevice"

#: Resolution order. ``RelayDevice`` is last because several IOM subclasses
#: inherit from it — a dimmer, and on some Indigo versions a speed control,
#: are relays too, and the specific reading must win.
_KIND_PRIORITY = (
    KIND_DIMMER, KIND_SENSOR, KIND_THERMOSTAT, KIND_SPRINKLER,
    KIND_SPEED_CONTROL, KIND_MULTI_IO, KIND_RELAY,
)


@dataclass(frozen=True)
class EligibleDevice:
    """A device the user may export, and the roles they may declare for it.

    ``eligible_roles`` is ordered with ``default_role`` first, so the role
    picker's first option is also its safest one.
    """

    eligible_roles: tuple[str, ...]
    default_role: str


@dataclass(frozen=True)
class Excluded:
    """A device v1 will not export, and the reason shown in the picker (XAC9)."""

    reason: str


Verdict = Union[EligibleDevice, Excluded]


def device_kind(dev) -> str:
    """The most specific known IOM class name in ``dev``'s ancestry, or ``""``."""
    try:
        names = {klass.__name__ for klass in type(dev).__mro__}
    except (AttributeError, TypeError):
        # An exotic proxy without a normal MRO is simply "unknown", not fatal.
        return ""
    for kind in _KIND_PRIORITY:
        if kind in names:
            return kind
    return ""


def classify(dev, plugin_id: str = DEFAULT_PLUGIN_ID) -> Verdict:
    """Classify one Indigo device for export (§5.2).

    ``plugin_id`` is the running plugin's own id — a device carrying it is
    excluded first, before any type reasoning, which is what makes the loop
    guard structural (XNG3/XAC6) rather than a downstream check that a future
    edit could route around.

    Never raises. Indigo device objects are live proxies, and one being deleted
    while the picker walks the database can raise from any attribute access —
    which would otherwise take out the whole dialog. Such a device is
    :data:`REASON_DEVICE_ERROR`, never eligible: if ``pluginId`` could not be
    read we cannot prove the loop guard passed, so the fail-safe answer is no.
    """
    try:
        if _plugin_id_of(dev) == plugin_id:
            return Excluded(REASON_LOOP_GUARD)
        handler = _BY_KIND.get(device_kind(dev))
        if handler is None:
            return Excluded(REASON_NO_ROLE)
        return handler(dev)
    except Exception as exc:  # pylint: disable=broad-except
        _LOG.error("Matter bridge: could not classify a device for export — %s", exc,
                   exc_info=True)
        return Excluded(REASON_DEVICE_ERROR)


def is_exportable(dev, plugin_id: str = DEFAULT_PLUGIN_ID) -> bool:
    """True if :func:`classify` yields an :class:`EligibleDevice`."""
    return isinstance(classify(dev, plugin_id), EligibleDevice)


def role_label(role: str) -> str:
    """Human label for a §4.2 role (falls back to the role name itself)."""
    return ROLE_LABELS.get(role, role)


# --------------------------------------------------------------------------
# Per-class rules (§5.2 table)
# --------------------------------------------------------------------------
def _relay(_dev) -> Verdict:
    """Relay → plug (default), light or lock.

    Valve, garage door and fan are §5.2 exclusions and so are absent from the
    offered roles; :data:`EXCLUDED_ROLES` carries the reasons for the docs and
    for anyone re-deriving this list.
    """
    return EligibleDevice(
        eligible_roles=(ROLE_ON_OFF_PLUG, ROLE_ON_OFF_LIGHT, ROLE_DOOR_LOCK),
        default_role=ROLE_ON_OFF_PLUG,
    )


def _dimmer(dev) -> Verdict:
    """Dimmer → dimmable light, upgraded by colour capability; or a covering."""
    supports_rgb = _flag(dev, "supportsRGB")
    supports_white_temp = _flag(dev, "supportsWhiteTemperature")
    if supports_rgb:
        default = ROLE_EXTENDED_COLOR_LIGHT
    elif supports_white_temp:
        default = ROLE_COLOR_TEMPERATURE_LIGHT
    else:
        default = ROLE_DIMMABLE_LIGHT
    roles = [ROLE_DIMMABLE_LIGHT]
    if supports_rgb:
        roles.append(ROLE_EXTENDED_COLOR_LIGHT)
    if supports_white_temp or supports_rgb:
        # An Extended Color Light is a superset; offering the colour-temp-only
        # role as well lets a user downgrade a bulb an ecosystem renders badly.
        roles.append(ROLE_COLOR_TEMPERATURE_LIGHT)
    roles.append(ROLE_WINDOW_COVERING)
    return EligibleDevice(eligible_roles=_default_first(roles, default), default_role=default)


def _sensor(dev) -> Verdict:
    """Sensor → binary (occupancy/contact) or numeric (by unit heuristic)."""
    if _flag(dev, "supportsOnState"):
        default = _binary_sensor_role(dev) or ROLE_OCCUPANCY_SENSOR
        return EligibleDevice(
            eligible_roles=_default_first(list(BINARY_SENSOR_ROLES), default),
            default_role=default,
        )
    if _flag(dev, "supportsSensorValue"):
        default = _numeric_sensor_role(dev)
        if default is None:
            return Excluded(REASON_SENSOR_UNITS)
        return EligibleDevice(
            eligible_roles=_default_first(list(NUMERIC_SENSOR_ROLES), default),
            default_role=default,
        )
    return Excluded(REASON_SENSOR_NO_VALUE)


def _thermostat(_dev) -> Verdict:
    """Thermostat → Thermostat. No fan in v1 (the FanControl descope)."""
    return EligibleDevice(eligible_roles=(ROLE_THERMOSTAT,), default_role=ROLE_THERMOSTAT)


def _speed_control(_dev) -> Verdict:
    return Excluded(REASON_FAN)


def _sprinkler(_dev) -> Verdict:
    return Excluded(REASON_SPRINKLER)


def _multi_io(_dev) -> Verdict:
    return Excluded(REASON_MULTI_IO)


_BY_KIND = {
    KIND_RELAY: _relay,
    KIND_DIMMER: _dimmer,
    KIND_SENSOR: _sensor,
    KIND_THERMOSTAT: _thermostat,
    KIND_SPEED_CONTROL: _speed_control,
    KIND_SPRINKLER: _sprinkler,
    KIND_MULTI_IO: _multi_io,
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _plugin_id_of(dev) -> str:
    value = getattr(dev, "pluginId", "")
    return value if isinstance(value, str) else ""


def _flag(dev, name: str) -> bool:
    """Read an Indigo capability flag defensively (absent → False).

    Only ever ``True`` for a real boolean-ish value: a MagicMock attribute is
    truthy for *everything*, so a bare ``bool()`` on an unset attribute of a
    mocked device would silently classify it as capable.
    """
    value = getattr(dev, name, False)
    return value is True or value == 1


def _default_first(roles: list[str], default: str) -> tuple[str, ...]:
    """``roles`` with ``default`` moved to the front, duplicates removed."""
    ordered = [default] + [role for role in roles if role != default]
    seen: list[str] = []
    for role in ordered:
        if role not in seen:
            seen.append(role)
    return tuple(seen)


def _unit_texts(dev) -> tuple[str, str]:
    """``(strong, weak)`` lower-cased haystacks for the unit heuristic.

    Indigo carries no canonical unit property on ``SensorDevice`` — plugins
    record it in their own props, and the only universal surface is the
    formatted display value (``displayStateValUi``, e.g. ``"72.3 °F"``). Those
    are the **strong** tier: a declared unit is a statement about the unit.
    The device's own **name** is the weak tier, consulted only when nothing
    declared a unit, because a name is prose the user wrote for themselves. It
    is kept rather than dropped because plenty of real Indigo sensors carry
    their unit nowhere else ("Greenhouse Temperature").
    """
    strong: list[str] = []
    props = getattr(dev, "pluginProps", None)
    if isinstance(props, dict):
        for key in _UNIT_PROP_KEYS:
            value = props.get(key)
            if isinstance(value, str):
                strong.append(value)
    for attr in _UNIT_ATTRS_STRONG:
        value = getattr(dev, attr, None)
        if isinstance(value, str):
            strong.append(value)
    weak = [value for value in (getattr(dev, attr, None) for attr in _UNIT_ATTRS_WEAK)
            if isinstance(value, str)]
    return " ".join(strong).lower(), " ".join(weak).lower()


def _role_for_text(text: str) -> Optional[str]:
    """First :data:`_UNIT_PATTERNS` role whose regex matches ``text``."""
    if not text:
        return None
    for role, needles in _UNIT_PATTERNS:
        for needle in needles:
            if needle.search(text):
                return role
    return None


def _binary_sensor_role(dev) -> Optional[str]:
    """Best-guess role for an on/off sensor from its name, or ``None``.

    ``None`` is not an exclusion here — unlike :func:`_numeric_sensor_role`,
    where an unrecognised unit means there is no faithful Matter sensor type.
    Every binary sensor has one (occupancy, the caller's fallback); this only
    says whether the *name* provided better evidence than that fallback.

    Reads the same two haystacks as the numeric guess (:func:`_unit_texts`),
    strong tier first, so a plugin that declares something useful in its props
    beats the prose in the device name — the same precedence, for the same
    reason.
    """
    strong, weak = _unit_texts(dev)
    return _binary_role_for_text(strong) or _binary_role_for_text(weak)


def _binary_role_for_text(text: str) -> Optional[str]:
    """First :data:`_BINARY_PATTERNS` role whose regex matches ``text``."""
    if not text:
        return None
    for role, needles in _BINARY_PATTERNS:
        for needle in needles:
            if needle.search(text):
                return role
    return None


def _numeric_sensor_role(dev) -> Optional[str]:
    """Best-guess role for a numeric sensor, or ``None`` if nothing matches.

    ``None`` is the §5.2 "sensors with units outside the table" exclusion. The
    guess is only a *default*: all five numeric roles are offered so a user can
    correct it, which is the same "user-declared role" posture §5.2 takes for
    relays.
    """
    strong, weak = _unit_texts(dev)
    return _role_for_text(strong) or _role_for_text(weak)
