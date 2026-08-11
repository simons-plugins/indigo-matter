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

Adding a setting is therefore three steps, not two hundred lines:

1. Declare a :class:`DeviceSetting` in :data:`SETTINGS` below.
2. Add its field(s) to the owning device type's ConfigUI in ``Devices.xml``.
3. Declare a ``<State id="…">`` in the same type's ``<States>`` whose id is the
   setting's ``key``. Since #190 that correspondence is load-bearing rather than
   conventional: ``getDeviceStateList`` withdraws states BY SETTING KEY, so a
   key with no matching state silently gates nothing. A parity test enforces it.

Step 2 is unavoidable and worth being honest about: Indigo builds ConfigUI
fields from static XML per device *type*, and unlike device states (which have
``getDeviceStateList``) there is no runtime field injection. So the XML cannot
be generated from these declarations. It is a small cost — settings are
inherently device-type-specific — and no *Python* is needed per setting.

Three rules are enforced here rather than left to call sites, because all three
come from live evidence rather than caution-in-principle (see the issue #186
research doc):

* **Bounds are never a guess.** Every setting declares WHERE its permitted
  values come from — :class:`FromAttribute` (the device reports its own limits),
  :class:`StaticRange` (the spec fixes them) or :class:`EnumValues`. Bounds that
  cannot be resolved mean the setting is not offered at all.
* **A setting the device does not implement is not offered.** The capability
  check is the device's own **AttributeList** (0xFFFB), not a firmware-version
  test and not the presence of a limits attribute — that stood in for it while
  both shipped settings happened to have one, but most settings take their
  bounds from the spec, so "are the limits readable?" cannot answer "does this
  unit have the attribute?". Of four plugs on the dev fabric, two implement
  OnOff's Lighting attributes and two do not; the list is what tells them apart.
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
    "FromAttribute", "StaticRange", "EnumValues",
    "parse_limits_struct", "parse_level_count",
    "settings_for_type", "coerce_value", "verify_write", "implements", "NO_ANSWER",
    "CLUSTER_OCCUPANCY_SENSING", "ATTR_HOLD_TIME", "ATTR_HOLD_TIME_LIMITS",
    "CLUSTER_ON_OFF", "ATTR_START_UP_ON_OFF", "ATTR_ON_TIME", "ATTR_OFF_WAIT_TIME",
    "ATTR_ATTRIBUTE_LIST",
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

# OnOff (0x0006). All three are conformance LT — mandatory if the Lighting
# feature is supported — so a compliant plug implements all three or none. The
# AttributeList gate decides per unit anyway, because compliance is a claim and
# the device's own list is evidence.
#
# Only StartUpOnOff is a SETTING. OnTime and OffWaitTime are the mandatory
# OnTime/OffWaitTime fields of OnWithTimedOff (command 0x42). The load-bearing
# fact, in matter.js's own words at OnOffServer.js:38-40: "Writes never start a
# countdown — only OnWithTimedOff does." So a pre-set value does nothing at all;
# off() then zeroes OnTime, inside an `if (this.features.lighting)` guard that
# every device carrying the attribute satisfies (#197, ADR-0005).
#
# Both ids are kept — deliberately unused for now — because implementing
# OnWithTimedOff as an action would need them, and because a bare 0x4001 in a
# future diff should resolve to a name that carries this warning with it.
CLUSTER_ON_OFF = 0x0006
ATTR_ON_TIME = 0x4001
ATTR_OFF_WAIT_TIME = 0x4002
ATTR_START_UP_ON_OFF = 0x4003

#: AttributeList (0xFFFB) — the device's own list of the attribute ids it
#: implements on a cluster. THE capability check: it is what the device says it
#: has, rather than what the spec says it might, and unlike a limits attribute
#: it exists on every cluster so it works for settings whose bounds are fixed by
#: the spec rather than reported per unit.
ATTR_ATTRIBUTE_LIST = 0xFFFB

#: "The device did not answer the read-back at all", kept DISTINCT from ``None``
#: because ``None`` is a legitimate attribute value — StartUpOnOff is nullable
#: ("restore the previous state", quality X). Conflating them reported a write
#: that provably did not land as merely unconfirmed, which is the one firmware
#: behaviour this whole feature exists to expose.
NO_ANSWER = object()


@dataclass(frozen=True)
class Bounds:
    """The values a setting may take.

    A contiguous range by default. ``allowed`` additionally restricts it to a
    specific set, for an enum whose values are not contiguous — validating such
    an attribute on range alone would accept a gap the device will reject.
    """

    minimum: int
    maximum: int
    allowed: Optional[frozenset] = None

    def contains(self, value: int) -> bool:
        """Whether *value* is one the device will accept."""
        if self.allowed is not None:
            return value in self.allowed
        return self.minimum <= value <= self.maximum

    def describe(self) -> str:
        """The range as UI text, e.g. ``1-300``."""
        return f"{self.minimum}-{self.maximum}"


# ---------------------------------------------------------------------------
# Where a setting's bounds come from
#
# Enumerating the Matter data model (tools/enumerate_settings.py) showed the
# FP300's pattern is the RARE one: of 57 writable attributes on the clusters
# this plugin handles, exactly 2 take their bounds from another attribute on the
# device — and both are already shipped. Most are a range fixed by the spec, an
# enum, or unbounded. So bounds are a declared STRATEGY rather than always a
# device read, and the capability question ("does this unit implement the
# attribute at all?") is answered separately, by the device's own AttributeList.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FromAttribute:
    """Bounds read from another attribute on the device (HoldTimeLimits…).

    The authoritative case: the device states its own limits, so nothing is
    assumed. Unresolvable limits mean the setting is not offered.
    """

    attribute: int
    parse: Callable[[Any], Optional[Bounds]]

    def resolve(self, cluster: int, limits: Callable) -> Optional[Bounds]:
        """Read and parse the device's limits attribute."""
        raw = limits(cluster, self.attribute)
        return None if raw is None else self.parse(raw)


@dataclass(frozen=True)
class StaticRange:
    """A range fixed by the Matter spec (e.g. WindowCovering Mode, max 15).

    No device read: the spec constrains every conformant implementation, so
    there is no per-unit limits attribute to consult. Weaker evidence than
    :class:`FromAttribute` — a non-conformant device could still reject a value
    in range — which is why the write is verified by read-back regardless.
    """

    minimum: int
    maximum: int

    # pylint: disable=unused-argument
    def resolve(self, cluster: int, limits: Callable) -> Optional[Bounds]:
        """The spec's range — same for every conformant device, so no read."""
        return Bounds(self.minimum, self.maximum)


@dataclass(frozen=True)
class EnumValues:
    """A spec-defined enum — fixed values with fixed meanings.

    Carries its own labels, unlike :attr:`DeviceSetting.options`, which builds
    them from something the device reports (sensitivity's level count).
    """

    values: tuple

    # pylint: disable=unused-argument
    def resolve(self, cluster: int, limits: Callable) -> Optional[Bounds]:
        """The enum's values — fixed by the spec, so no read."""
        numbers = [int(value) for value, _label in self.values]
        if not numbers:
            return None
        return Bounds(min(numbers), max(numbers), frozenset(numbers))

    # pylint: disable=unused-argument
    def options(self, bounds: Bounds) -> list:
        """Picker rows. The labels are the enum's own, not derived."""
        return [(str(value), label) for value, label in self.values]


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
    #: Where the permitted values come from: FromAttribute (the device states
    #: its own limits), StaticRange (the spec fixes them), or EnumValues.
    #: NOT the capability check — that is the device's AttributeList, so a
    #: setting with spec-fixed bounds is still only offered to a unit that
    #: actually implements the attribute.
    bounds: Any
    #: Indigo device types whose Devices.xml declares this setting's field. A
    #: setting is only ever offered on a type listed here. The XML is kept in
    #: step BY HAND, so that correspondence is enforced by a parity test
    #: (tests/test_plugin_module.py) rather than by this declaration alone.
    device_types: frozenset
    unit: str = ""
    #: Builds picker rows from the resolved bounds, for a menu whose LABELS
    #: depend on what the device reported (sensitivity's level count). An
    #: EnumValues bounds carries its own labels and needs nothing here.
    options: Optional[Callable[[Bounds], list[tuple[str, str]]]] = None

    def resolve_bounds(self, limits: Callable) -> Optional[Bounds]:
        """The permitted values for this setting on one device, or None."""
        return self.bounds.resolve(self.cluster, limits)

    def option_rows(self, bounds: Bounds) -> Optional[list]:
        """Picker rows, or None when this renders as a numeric field."""
        if self.options is not None:
            return self.options(bounds)
        builder = getattr(self.bounds, "options", None)
        return builder(bounds) if builder is not None else None

    @property
    def is_choice(self) -> bool:
        """True when this renders as a picker rather than a numeric field."""
        return self.options is not None or hasattr(self.bounds, "options")

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
        bounds=FromAttribute(ATTR_HOLD_TIME_LIMITS, parse_limits_struct),
        # OccupancySensing only — there is no hold time on a contact sensor.
        device_types=frozenset({"matterMotionSensor"}),
        unit=" seconds",
    ),
    DeviceSetting(
        key="sensitivityLevel",
        label="Sensitivity",
        cluster=CLUSTER_BOOLEAN_STATE_CONFIG,
        attribute=ATTR_CURRENT_SENSITIVITY,
        bounds=FromAttribute(ATTR_SUPPORTED_SENSITIVITY_LEVELS, parse_level_count),
        # The Matter spec pairs 0x0080 with BooleanState (contact); the FP300
        # co-locates it with OccupancySensing (motion) instead — issue #85. Both.
        device_types=frozenset({"matterMotionSensor", "matterContactSensor"}),
        options=sensitivity_options,
    ),
    # --- OnOff's Lighting-feature setting (conformance LT) -------------------
    # Present only on devices implementing the Lighting feature — which is NOT
    # the same as "is a light". Of four plugs on the dev fabric two implement it
    # and two do not, so the AttributeList gate is doing real work here rather
    # than guarding a theoretical case.
    #
    # StartUpOnOff is the ONLY genuine setting on this cluster. OnTime (0x4001)
    # and OffWaitTime (0x4002) are writable and look like siblings, but they are
    # parameters of OnWithTimedOff — declaring one shipped a dialog field that
    # could never work (#197, ADR-0005). Do not add them back.
    DeviceSetting(
        key="startUpOnOff",
        label="After a power cut",
        cluster=CLUSTER_ON_OFF,
        attribute=ATTR_START_UP_ON_OFF,
        # StartUpOnOffEnum: spec-fixed values, spec-fixed meanings. The spec
        # also allows null ("restore the previous state"), which is deliberately
        # NOT offered: null is a distinct wire value this write path has no way
        # to express, and silently mapping it to something else would misreport
        # what the device is set to.
        bounds=EnumValues(((0, "Off"), (1, "On"), (2, "Toggle (opposite of before)"))),
        device_types=frozenset({"matterRelay"}),
    ),
)


def settings_for_type(device_type_id: str) -> list:
    """Every setting declared for an Indigo device TYPE.

    Type-level, so it says what the XML has fields for — not what a particular
    unit implements. That question is answered by the device's own
    AttributeList, in the plugin's Indigo-facing layer — the ConfigUI callbacks
    for whether a setting is offered, and ``getDeviceStateList`` for whether its
    state exists — those being the only places that can look a node up.
    """
    return [s for s in SETTINGS if device_type_id in s.device_types]


def implements(attribute_list: Any, attribute: int) -> Optional[bool]:
    """Does a device implement *attribute*, per its own AttributeList?

    Returns True/False when the list is readable, and **None when it is
    unknown** — an unreconciled node, or a cluster whose AttributeList was not
    captured.

    None is not False. But there is deliberately **no single policy for what
    None means** — read the consumer, do not assume:

    * ``device_settings.unimplemented_states`` keeps the state (withdrawing one
      breaks triggers bound to it, so it needs a positive NO).
    * ``device_settings.offered_settings`` still WITHHOLDS a spec-bounded
      setting on None, because those bounds resolve unconditionally and
      "unknown" would otherwise mean "offer it to everything".

    Assuming the first policy held everywhere is exactly how the #192 eviction
    path came to claim it could never hide a setting.
    """
    if not isinstance(attribute_list, (list, tuple, set, frozenset)):
        return None
    ids = set()
    for entry in attribute_list:
        try:
            ids.add(int(entry))
        except (TypeError, ValueError):
            # A list we could only partly read cannot support a confident NO.
            # Claiming absence from data we know we failed to parse is worse
            # than admitting we do not know.
            return None
    if not ids:
        return None
    return int(attribute) in ids


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

    A read-back of ``None`` is NOT that case: it is the device answering with a
    null, which for a nullable attribute means it holds no value. That is a
    definite failure, not an unproven one.
    """
    if read_back is NO_ANSWER:
        return False, (f"{setting.label} could not be confirmed — the device did not answer "
                       f"the read-back. The setting may or may not have been applied.")
    if read_back is None:
        return False, (f"{setting.label} was NOT applied — the device answered the read-back "
                       f"with no value (null) after being asked for {intended}{setting.unit}.")
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
