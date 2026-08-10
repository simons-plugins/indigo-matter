"""Writable device settings — the declarative registry (issue #186).

A *setting* is a writable Matter configuration attribute a user may want to
change: the Aqara FP300's presence hold time and motion sensitivity are the
first two. This is deliberately a different thing from a *reading* (which the
cluster handlers already surface as Indigo states) and from a *control*
(on/off, level, setpoint — Indigo's built-in device actions).

**Why a registry rather than a bespoke action per setting.** Issue #85 set the
first precedent — one custom Indigo action, one callback, one ad-hoc bounds
check, for one attribute. Several more settings are queued (#70 Pump
Configuration, #71 Mode Select, #175 appliance modes, thermostat config), and
that shape scales badly: each one would re-implement the same validation and,
worse, the same read-back verification. Here each setting is a DECLARATION and
the machinery is written once.

Adding a setting is therefore two steps, not two hundred lines:

1. Declare a :class:`DeviceSetting` in :data:`SETTINGS` below.
2. Add its field(s) to the owning device type's ConfigUI in ``Devices.xml``.

Step 2 is unavoidable and worth being honest about: Indigo builds ConfigUI
fields from static XML per device *type*, and unlike device states (which have
``getDeviceStateList``) there is no runtime field injection. So the XML cannot
be generated from these declarations. It is a small cost — settings are
inherently device-type-specific — and no *Python* is needed per setting.

Three rules are enforced here rather than left to call sites, because all three
come from live evidence rather than caution-in-principle (see the issue #186
research doc):

* **Bounds come from the device, never from a constant.** Every setting names
  the attribute its limits are read from (``HoldTimeLimits``,
  ``SupportedSensitivityLevels``) and how to parse it. A setting whose limits
  are unknown is not offered at all rather than offered and guessed at.
* **A setting the device does not implement is not offered.** The presence of
  the limits attribute IS the capability check: the Matter spec makes
  ``HoldTimeLimits`` mandatory where ``HoldTime`` is supported, so a pre-1.4
  unit with neither simply has no hold-time field. This is why the check is not
  a firmware-version test.
* **Every write is verified by reading back.** Aqara firmware can ACK a write
  and silently ignore it, so an unverified write is reported as a FAILURE. That
  logic lives in :func:`verify_write` and has exactly one implementation.

This module is Indigo-free and matter-server-free — pure declarations plus pure
functions over them — so the whole registry is unit-testable without a Matter
stack or an ``indigo`` module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

__all__ = [
    "Bounds", "DeviceSetting", "SETTINGS",
    "parse_limits_struct", "parse_level_count",
    "settings_for_type", "coerce_value", "verify_write",
    "CLUSTER_OCCUPANCY_SENSING", "ATTR_HOLD_TIME", "ATTR_HOLD_TIME_LIMITS",
]

# OccupancySensing (0x0406) — HoldTime and its limits. Matter 1.4 (cluster
# revision 5). NOT the deprecated 0x0010/0x0011 PIR delays: the FP300 does not
# implement those at all, so code written against them silently does nothing.
CLUSTER_OCCUPANCY_SENSING = 0x0406
ATTR_HOLD_TIME = 0x0003
ATTR_HOLD_TIME_LIMITS = 0x0004

# BooleanStateConfiguration (0x0080). Imported by value rather than from
# boolean_state_config to keep this module free of handler imports (the handlers
# import this one).
CLUSTER_BOOLEAN_STATE_CONFIG = 0x0080
ATTR_CURRENT_SENSITIVITY = 0x0000
ATTR_SUPPORTED_SENSITIVITY_LEVELS = 0x0001


@dataclass(frozen=True)
class Bounds:
    """The inclusive range a setting's value may take, per the device itself."""

    minimum: int
    maximum: int

    def contains(self, value: int) -> bool:
        """Whether *value* is within the device's own permitted range."""
        return self.minimum <= value <= self.maximum

    def describe(self) -> str:
        """The range as UI text, e.g. ``1-300``."""
        return f"{self.minimum}-{self.maximum}"


def parse_limits_struct(value: Any) -> Optional[Bounds]:
    """Bounds from a Matter limits STRUCT — OccupancySensing's HoldTimeLimits.

    **The wire shape is field-INDEX keys, not names.** The live FP300 reports:

        "1/1030/4": {"0": 1, "1": 300, "2": 10}

    which is ``HoldTimeLimitsStruct{HoldTimeMin=0, HoldTimeMax=1,
    HoldTimeDefault=2}`` — the same index-keyed serialisation matter-server uses
    for every struct (cf. the Descriptor's DeviceTypeList, ``[{"0": 263}]``).
    Verified against the raw ``get_node`` dump, which is the only trustworthy
    source for this: the issue #186 research doc renders the same attribute as
    ``{min: 1, max: 300, default: 10}``, and that is prose about what the fields
    MEAN, not what arrives. Coding against the prose shipped a hold-time field
    that never appeared, because the parse returned None and a setting whose
    bounds are unknown is deliberately not offered.

    Named keys are still accepted as a fallback in case a future matter-server
    resolves struct fields to names. Anything else yields None, which the caller
    turns into "do not offer this setting" rather than a guess.

    Indices 0/1 are min/max **for this struct**; a different limits struct would
    need its own parser rather than reusing this one on the assumption that
    every Matter struct orders its fields that way.
    """
    if not isinstance(value, dict):
        return None
    low = _struct_field(value, 0, "min", "holdTimeMin")
    high = _struct_field(value, 1, "max", "holdTimeMax")
    try:
        low_i, high_i = int(low), int(high)
    except (TypeError, ValueError):
        return None
    if low_i > high_i:
        return None  # a device contradicting itself is not a range to validate against
    return Bounds(low_i, high_i)


def _struct_field(struct: dict, index: int, *names: str) -> Any:
    """One field of a wire struct, by index first and then by any known name.

    JSON object keys are strings, but nothing guarantees a parser has not handed
    back real ints, so both are tried.
    """
    for key in (str(index), index):
        if key in struct:
            return struct[key]
    for name in names:
        if name in struct:
            return struct[name]
    return None


def parse_level_count(value: Any) -> Optional[Bounds]:
    """Bounds from a COUNT of levels — BooleanStateConfiguration's
    SupportedSensitivityLevels, which reads as ``3`` meaning indices 0..2.

    Spec §1.8.6.2 is normative on the ordering: levels *shall* run from 0 =
    least sensitive up to the highest supported value = most sensitive. That is
    a ``shall``, not a convention, which is why :func:`sensitivity_options` can
    label the endpoints without hardware confirmation.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    if count < 1:
        return None
    return Bounds(0, count - 1)


def sensitivity_options(bounds: Bounds) -> list[tuple[str, str]]:
    """Picker rows for a sensitivity level, driven by what the device reports.

    The spec fixes the ORDERING and the two endpoints, not names for anything in
    between — so a 3-level device gets Low/Standard/High (the middle name is an
    interpolation, but a harmless one), and any other count falls back to naming
    the endpoints and numbering the rest. Deliberately not hard-coded to three:
    a 5-level device has no spec-blessed wording for levels 1-3, and inventing
    some would be a lie about what the device means.
    """
    count = bounds.maximum - bounds.minimum + 1
    if count == 3:
        labels = ["Low (least sensitive)", "Standard", "High (most sensitive)"]
    else:
        labels = [f"Level {i}" for i in range(bounds.minimum, bounds.maximum + 1)]
        if labels:
            labels[0] += " (least sensitive)"
            labels[-1] += " (most sensitive)"
    return [(str(bounds.minimum + i), label) for i, label in enumerate(labels)]


@dataclass(frozen=True)
class DeviceSetting:
    """One writable configuration attribute, and everything needed to offer it.

    ``key`` is used as BOTH the ConfigUI field id in Devices.xml and the Indigo
    state id, so it must satisfy Indigo's strict state-id rules (ASCII letters
    and digits only, leading letter, no underscores — an underscore raises
    ``LowLevelBadParameterError`` from ``stateListOrDisplayStateIdChanged`` with
    no indication of which key was at fault).
    """

    key: str
    label: str
    cluster: int
    attribute: int
    #: The read-only attribute the bounds come from, and how to read it. Its
    #: presence on the node is also the capability check — see the module
    #: docstring.
    limits_attribute: int
    parse_limits: Callable[[Any], Optional[Bounds]]
    #: Indigo device types whose Devices.xml declares this setting's field. A
    #: setting is only ever offered on a type listed here, so the registry can
    #: never promise a field the XML does not have.
    device_types: frozenset
    unit: str = ""
    #: Builds picker rows, for settings rendered as a menu. None means a plain
    #: numeric text field validated against the bounds.
    options: Optional[Callable[[Bounds], list[tuple[str, str]]]] = None

    @property
    def is_choice(self) -> bool:
        """True when this renders as a picker rather than a numeric field."""
        return self.options is not None

    def describe_range(self, bounds: Bounds) -> str:
        """Human range hint, e.g. ``1-300 seconds``."""
        return f"{bounds.describe()}{self.unit}"


#: Every writable setting the plugin knows how to offer. See the module
#: docstring for how to add one.
SETTINGS: tuple = (
    DeviceSetting(
        key="holdTime",
        label="Presence Hold Time",
        cluster=CLUSTER_OCCUPANCY_SENSING,
        attribute=ATTR_HOLD_TIME,
        limits_attribute=ATTR_HOLD_TIME_LIMITS,
        parse_limits=parse_limits_struct,
        # OccupancySensing only — there is no hold time on a contact sensor.
        device_types=frozenset({"matterMotionSensor"}),
        unit=" seconds",
    ),
    DeviceSetting(
        key="sensitivityLevel",
        label="Sensitivity",
        cluster=CLUSTER_BOOLEAN_STATE_CONFIG,
        attribute=ATTR_CURRENT_SENSITIVITY,
        limits_attribute=ATTR_SUPPORTED_SENSITIVITY_LEVELS,
        parse_limits=parse_level_count,
        # The Matter spec pairs 0x0080 with BooleanState (contact); the FP300
        # co-locates it with OccupancySensing (motion) instead — issue #85. Both.
        device_types=frozenset({"matterMotionSensor", "matterContactSensor"}),
        options=sensitivity_options,
    ),
)


def settings_for_type(device_type_id: str) -> list:
    """Every setting declared for an Indigo device TYPE.

    Type-level, so it says what the XML has fields for — not what a particular
    unit implements. Deciding that needs the device's limits (see
    :meth:`DeviceSetting.parse_limits`) and is done by the plugin's ConfigUI
    layer, which is the only place that can look a node up.
    """
    return [s for s in SETTINGS if device_type_id in s.device_types]


def coerce_value(setting: DeviceSetting, raw: Any, bounds: Bounds) -> tuple[Optional[int], str]:
    """Validate a user-entered value against the DEVICE's own bounds.

    Returns ``(value, "")`` when it is safe to send, or ``(None, reason)`` when
    it is not — the reason being UI-ready text naming the real range.

    Rejecting here matters beyond tidiness: spec §1.8.6.1 says a device *shall*
    answer an unsupported sensitivity with CONSTRAINT_ERROR, so an out-of-range
    write is a round trip to a sleepy device to be told what the limits
    attribute already said.
    """
    text = str(raw).strip()
    if not text:
        return None, f"Enter a value for {setting.label}."
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None, f"{setting.label} must be a whole number."
    if not bounds.contains(value):
        return None, (f"{setting.label} must be between "
                      f"{bounds.minimum} and {bounds.maximum}{setting.unit}.")
    return value, ""


def verify_write(setting: DeviceSetting, intended: int, read_back: Any) -> tuple[bool, str]:
    """Did the write actually land? The single implementation of rule 1.

    A device that ACKs a write and silently ignores it is not a hypothetical:
    Aqara firmware does exactly this. So the ACK is worth nothing on its own and
    success is defined as "a subsequent read returns the value we asked for".
    A mismatch is reported as a FAILURE, never as success.

    An unreadable read-back is also a failure. It genuinely might be a healthy
    write whose verification timed out on a sleepy device — but reporting an
    unproven write as success is the one outcome that makes the check pointless,
    and the wording says which case the user is in.
    """
    if read_back is None:
        return False, (f"{setting.label} could not be confirmed — the device did not answer "
                       f"the read-back. The setting may or may not have been applied.")
    try:
        actual = int(read_back)
    except (TypeError, ValueError):
        return False, (f"{setting.label} could not be confirmed — the device answered the "
                       f"read-back with {read_back!r}, which is not a value.")
    if actual != intended:
        return False, (f"{setting.label} was NOT applied — the device still reports "
                       f"{actual}{setting.unit} after being asked for {intended}{setting.unit}. "
                       f"Some firmware acknowledges a write and ignores it.")
    return True, ""
