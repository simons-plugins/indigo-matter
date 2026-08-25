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
ROLE_SMOKE_ALARM = "smokeAlarm"
ROLE_CO_ALARM = "coAlarm"
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
    ROLE_SMOKE_ALARM: "Smoke alarm",
    ROLE_CO_ALARM: "Carbon monoxide alarm",
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
REASON_SENSOR_UNITS = "no faithful Matter sensor type"
#: Replaces the former ``REASON_NO_ROLE`` / ``REASON_SENSOR_NO_VALUE`` pair
#: (issue #252). Both used to say some version of "this device has no Matter
#: role", which was true of the *type* and misleading about the device: on the
#: reference database the largest group saying it — Texecom's 24 alarm zones —
#: were ordinary PIRs and door contacts whose reading simply lived in a
#: plugin-named state. Now a device with a boolean to map is
#: :class:`MappableDevice` instead, and these two reasons are what is left:
#: genuinely nothing to publish, split so a user can tell "never" from
#: "not yet".
REASON_NO_BINARY_STATE = "nothing here reports an on/off state Matter could publish"
REASON_ONLY_NUMERIC_STATES = ("only numeric states, which cannot be mapped yet — "
                              "mapping covers on/off states in this version")
REASON_MAPPED_STATE_GONE = "the mapped state is no longer reported by this device"
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
    "heatAlarm": "Matter has no heat-alarm device type; Smoke CO Alarm 0x0076 senses "
                 "smoke and CO only, and exporting a heat zone as either would be a "
                 "safety lie",
}

#: ``options`` key naming the state a mapped export reads (ADR-0012). Defined
#: here rather than imported from ``export_store`` because this module is
#: deliberately dependency-free — the store imports the catalog, not the
#: reverse — and ``tests/test_export_store.py`` pins the two spellings equal.
OPTION_STATE_KEY = "stateKey"
#: Companion to :data:`OPTION_STATE_KEY`: the device reports the opposite sense
#: (an alarm zone whose boolean is true when *healthy*, say).
OPTION_STATE_INVERT = "stateInvert"

#: Preference order for :func:`binary_state_keys`. Anything unlisted sorts
#: after these, alphabetically.
_STATE_KEY_RANK = {"onOffState": 0, "onOff": 1, "state": 2, "status": 3, "active": 4}

#: The binary (on/off) sensor roles. All are offered for any on/off sensor; the
#: name heuristic only picks the *default* (see :func:`_binary_sensor_role`),
#: exactly as the unit heuristic does for the numeric five.
#:
#: Occupancy leads because it is the safe fallback when nothing matches: Matter
#: has no motion device type, so Occupancy Sensor 0x0107 is what an Indigo
#: motion sensor — the commonest on/off sensor by a wide margin — has to be.
BINARY_SENSOR_ROLES = (
    ROLE_OCCUPANCY_SENSOR, ROLE_CONTACT_SENSOR, ROLE_WATER_LEAK_DETECTOR,
    ROLE_WATER_FREEZE_DETECTOR, ROLE_RAIN_SENSOR, ROLE_SMOKE_ALARM, ROLE_CO_ALARM,
)

#: Binary roles whose Matter polarity is the OPPOSITE of Indigo's, so a new
#: export of one starts with its inversion ticked (issue #252, reported live
#: 2026-08-19 against a Z-Wave door sensor).
#:
#: Only ``contactSensor``, and the asymmetry is Matter's own. Indigo's
#: ``onState`` means *tripped* for every binary sensor — motion detected, leak
#: detected, door OPEN. Matter agrees for all of them except this one:
#: BooleanState in a Contact Sensor is documented "FALSE=open or no contact,
#: TRUE=closed or contact", so a door reads true when it is **shut**. Exporting
#: a contact sensor straight through therefore reports every door as open when
#: it is closed, which is what a Z-Wave pantry door did.
#:
#: A default, not a rule: some plugins do report a contact the Matter way, so
#: the tick-box remains and this only decides how it starts.
ROLES_INVERTED_BY_DEFAULT = (ROLE_CONTACT_SENSOR,)


def default_invert_for(role: str) -> bool:
    """Whether a NEW export of ``role`` should start with polarity inverted."""
    return role in ROLES_INVERTED_BY_DEFAULT


#: Roles Alexa's Matter bridge support cannot model (issue #278). Amazon's
#: supported-device-types table maps ``waterLeakDetector`` to no capability
#: interface, and measured on the reference rig (issue #143), exporting even
#: one causes Alexa to either never subscribe to the WHOLE bridge — every
#: exported device goes stale in Alexa, not just this one — or silently drop
#: its own fabric — the same whole-bridge failure independently reproduced
#: by home-assistant-matter-hub#365 (one leak sensor → their bridge never
#: subscribes again; their v2.0.49 works around it by mapping leak/freeze/rain
#: to contactSensor by default). Apple Home is unaffected. matterbridge's own
#: README separately names ``waterFreezeDetector`` and ``rainSensor`` (not
#: just leak) as Alexa-unsupported; neither was independently reproduced on
#: our rig, so they carry the warning on that citation until proven otherwise.
#: Referenced by ``MenuItems.xml``'s ``exportAlexaLeakWarning`` label
#: (``visibleBindingValue``) — kept in sync by
#: ``tests/test_export_menu.py::test_alexa_unsupported_warning_matches_roles``.
ALEXA_UNSUPPORTED_ROLES = (ROLE_WATER_LEAK_DETECTOR, ROLE_WATER_FREEZE_DETECTOR, ROLE_RAIN_SENSOR)


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
#:
#: Needles that name a *thing* are pluralised (``doors?``, ``windows?``,
#: ``gates?``): real alarm zones are named for what they cover, and "Patio
#: Doors" and "Garden Doors" — both ordinary door contacts — fell through the
#: singular ``\bdoor\b`` to the occupancy fallback. Words that name a
#: *phenomenon* stay singular, because "motions" and "floods" are not how
#: anyone names a sensor.
_BINARY_PATTERNS = tuple(
    (role, tuple(re.compile(pattern) for pattern in patterns)) for role, patterns in (
        (ROLE_OCCUPANCY_SENSOR, (r"\bmotion\b", r"\bpirs?\b", r"\boccupancy\b",
                                 r"\boccupied\b", r"\bpresence\b")),
        (ROLE_WATER_LEAK_DETECTOR, (r"\bleaks?\b", r"\bflood\b", r"\bwater alarm\b",
                                    r"\bwater sensor\b")),
        (ROLE_WATER_FREEZE_DETECTOR, (r"\bfreeze\b", r"\bfreezing\b", r"\bfrost\b")),
        (ROLE_RAIN_SENSOR, (r"\brain\b",)),
        # CO before smoke: a combined "Smoke & CO Alarm" can only take one
        # role, and CO is the reading a user loses silently — smoke they can
        # also smell. The explicit phrases lead; bare `\bco\b` comes last
        # because real names often carry nothing else ("CO Sensor TZ20",
        # "Living Room CO"), and it is a default a user can override anyway.
        (ROLE_CO_ALARM, (r"\bcarbon monoxide\b", r"\bco alarm\b", r"\bco detector\b",
                         r"\bco sensor\b", r"\bco\b")),
        (ROLE_SMOKE_ALARM, (r"\bsmoke\b", r"\bfire alarm\b")),
        (ROLE_CONTACT_SENSOR, (r"\bcontacts?\b", r"\bdoors?\b", r"\bwindows?\b",
                               r"\bgates?\b", r"\breed\b", r"\bopen/closed\b",
                               r"\bskylights?\b")),
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
class MappableDevice:
    """A device with no Matter role of its own, but a boolean the user could map.

    Indigo's typed device classes (:class:`indigo.SensorDevice` and friends)
    are what :func:`classify` reasons from, and a plugin declaring
    ``<Device type="custom">`` gets none of them: its device is a plain
    ``indigo.Device`` whose ``supportsOnState`` is False and whose real reading
    lives in a plugin-named state. Texecom's alarm zones are the measured case
    — 24 of them on the reference database, every one a PIR, contact, smoke or
    CO zone that Matter could publish perfectly well — and they are far from
    alone: kind-less devices are the single largest exclusion bucket there is.

    This verdict is what lets the picker say "you can export this, tell me
    which state" instead of "no resolvable Matter role". It deliberately does
    **not** carry ``eligible_roles``: nothing but :class:`EligibleDevice` may
    have that attribute, so no caller can accidentally treat an unmapped
    device as exportable by duck-typing. Re-run :func:`classify` with the
    declared mapping in ``options`` to get the roles (ADR-0012).
    """

    #: Boolean states the user may choose between, best guess first.
    candidate_state_keys: tuple[str, ...]
    #: The reason shown in the picker (XAC9), phrased as an invitation.
    reason: str = "needs a state to be mapped — choose one below"

    @property
    def suggested_state_key(self) -> str:
        """The best guess, which the dialog pre-selects."""
        return self.candidate_state_keys[0]


@dataclass(frozen=True)
class Excluded:
    """A device v1 will not export, and the reason shown in the picker (XAC9)."""

    reason: str


Verdict = Union[EligibleDevice, MappableDevice, Excluded]


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


def classify(dev, plugin_id: str = DEFAULT_PLUGIN_ID, options: Optional[dict] = None) -> Verdict:
    """Classify one Indigo device for export (§5.2).

    ``plugin_id`` is the running plugin's own id — a device carrying it is
    excluded first, before any type reasoning, which is what makes the loop
    guard structural (XNG3/XAC6) rather than a downstream check that a future
    edit could route around.

    ``options`` is the export entry's options dict, or the mapping a dialog is
    in the middle of composing. It matters for exactly one class of device:
    one whose reading lives in a plugin-named state, where eligibility is the
    user's declaration rather than a property of the device (**ADR-0012**).
    Every caller holding an entry must pass its options, or a mapped export
    re-classifies as un-exportable the next time the guard runs. Callers that
    genuinely have no mapping — the picker sweep, which is asking "could this
    be exported at all?" — pass nothing and get :class:`MappableDevice`.

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
            return _custom(dev, options)
        return handler(dev, options)
    except Exception as exc:  # pylint: disable=broad-except
        _LOG.error("Matter bridge: could not classify a device for export — %s", exc,
                   exc_info=True)
        return Excluded(REASON_DEVICE_ERROR)


def is_exportable(dev, plugin_id: str = DEFAULT_PLUGIN_ID,
                  options: Optional[dict] = None) -> bool:
    """True if :func:`classify` yields an :class:`EligibleDevice`.

    A :class:`MappableDevice` is deliberately False: it is a device that
    *could* be exported once its state is declared, which is not the same
    thing as one that can be exported now.
    """
    return isinstance(classify(dev, plugin_id, options), EligibleDevice)


def role_label(role: str) -> str:
    """Human label for a §4.2 role (falls back to the role name itself)."""
    return ROLE_LABELS.get(role, role)


@dataclass(frozen=True)
class TargetRefusal:
    """Why :func:`eligible_target` refused ``dev`` as a migrate/re-adopt
    target, or why an already-saved export no longer holds up.

    Always carries the :func:`classify` verdict this came from — including an
    :class:`EligibleDevice` one, on a role mismatch — so a caller that needs
    more than the bare reason (e.g. the device's CURRENT ``eligible_roles``,
    to name what it offers instead) can still read it off ``.verdict``. This
    stays plain data on purpose: the callers that raise it word a refusal
    differently from each other (migrate vs re-adopt vs the startup
    reconcile's log line), so the message itself is never built here.
    """

    verdict: Verdict
    #: True when ``verdict`` IS an :class:`EligibleDevice` but not for the
    #: role asked of it — the one outcome that is not itself a `classify()`
    #: refusal.
    role_mismatch: bool = False


def eligible_target(dev, plugin_id: str, saved_options: Optional[dict],
                    required_role: str) -> Union[EligibleDevice, TargetRefusal]:
    """Classify ``dev`` for reuse as a migrate/re-adopt/reconcile target that
    must be able to take ``required_role``.

    This is the ~10-line dance that used to be hand-written at every call
    site that needs it (issue #246's migrate and re-adopt pickers, plus the
    startup export reconcile): call :func:`classify` with the device's own
    saved options, refuse anything that is not an :class:`EligibleDevice`,
    then refuse again if the role it is being asked to carry is not one of
    the roles offered. Returns the :class:`EligibleDevice` verdict on
    success, or a :class:`TargetRefusal` a caller can turn into its own
    wording.
    """
    verdict = classify(dev, plugin_id, saved_options)
    if not isinstance(verdict, EligibleDevice):
        return TargetRefusal(verdict)
    if required_role not in verdict.eligible_roles:
        return TargetRefusal(verdict, role_mismatch=True)
    return verdict


def excluded_row(verdict: Excluded, device_id, name: str, mark: str,
                 excluded_prefix: str) -> Optional[tuple[str, str]]:
    """The picker row for an :class:`Excluded` verdict (XAC9) — shared,
    byte-for-byte, by every device picker that lists exclusions with their
    reason (``ExportDialogMixin._candidate_row``,
    ``ExportRecoveryMenuMixin._readopt_device_row``).

    Returns ``None`` for the loop guard: XAC6 requires that row ABSENT, not
    merely unpickable, so the caller must omit it rather than show it
    excluded-with-a-reason. Every other exclusion gets the row.
    """
    if verdict.reason == REASON_LOOP_GUARD:
        return None
    return (f"{excluded_prefix}{device_id}", f"{mark}{name} — not exportable: {verdict.reason}")


# --------------------------------------------------------------------------
# Per-class rules (§5.2 table)
# --------------------------------------------------------------------------
def _relay(_dev, _options=None) -> Verdict:
    """Relay → plug (default), light or lock.

    Valve, garage door and fan are §5.2 exclusions and so are absent from the
    offered roles; :data:`EXCLUDED_ROLES` carries the reasons for the docs and
    for anyone re-deriving this list.
    """
    return EligibleDevice(
        eligible_roles=(ROLE_ON_OFF_PLUG, ROLE_ON_OFF_LIGHT, ROLE_DOOR_LOCK),
        default_role=ROLE_ON_OFF_PLUG,
    )


def _dimmer(dev, _options=None) -> Verdict:
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


def _sensor(dev, options=None) -> Verdict:
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
    # A SensorDevice that declares neither flag is in the same position as a
    # custom device: typed as a sensor, but with its reading somewhere only its
    # own plugin names. Measured on the reference database this is rare (3
    # devices, against 326 kind-less ones) — it shares the path because the
    # remedy is identical, not because the population matters.
    return _custom(dev, options)


def _thermostat(_dev, _options=None) -> Verdict:
    """Thermostat → Thermostat. No fan in v1 (the FanControl descope)."""
    return EligibleDevice(eligible_roles=(ROLE_THERMOSTAT,), default_role=ROLE_THERMOSTAT)


def _speed_control(_dev, _options=None) -> Verdict:
    return Excluded(REASON_FAN)


def _sprinkler(_dev, _options=None) -> Verdict:
    return Excluded(REASON_SPRINKLER)


def _multi_io(_dev, _options=None) -> Verdict:
    return Excluded(REASON_MULTI_IO)


def _custom(dev, options=None) -> Verdict:
    """A device with no IOM class of its own — eligible only once mapped.

    See :class:`MappableDevice` for why this population exists and how large
    it is. Three outcomes, and the order is the whole safety argument:

    * a declared mapping whose state is **still reported** → an ordinary
      :class:`EligibleDevice` over the binary roles;
    * a declared mapping whose state has **gone** → :class:`Excluded`, never
      eligible. A plugin that renamed or dropped a state must not have its old
      key silently read as absent-therefore-False, which would tell every
      ecosystem the leak sensor is dry;
    * no mapping → :class:`MappableDevice` if there is anything to map,
      otherwise :class:`Excluded`.
    """
    key = _mapped_state_key(options)
    candidates = binary_state_keys(dev)
    if key is not None:
        if key not in candidates:
            return Excluded(REASON_MAPPED_STATE_GONE)
        default = _binary_sensor_role(dev) or ROLE_OCCUPANCY_SENSOR
        return EligibleDevice(
            eligible_roles=_default_first(list(BINARY_SENSOR_ROLES), default),
            default_role=default,
        )
    if candidates:
        return MappableDevice(candidate_state_keys=candidates)
    return Excluded(REASON_ONLY_NUMERIC_STATES if _has_numeric_state(dev)
                    else REASON_NO_BINARY_STATE)


def _mapped_state_key(options) -> Optional[str]:
    """The declared state key in ``options``, or ``None``.

    Defensive about the whole shape rather than trusting the store: this is
    also handed dialog values mid-edit, and a blank menu selection arrives as
    an empty string, which must read as "not chosen yet" and not as a state
    named "".
    """
    if not isinstance(options, dict):
        return None
    key = options.get(OPTION_STATE_KEY)
    return key if isinstance(key, str) and key else None


def binary_state_keys(dev) -> tuple[str, ...]:
    """The device's boolean states, best guess first — the mapping candidates.

    Two filters and one ordering, all of them measured against a real database:

    * **Booleans only.** A plugin state holding 0/1 or "on"/"off" is not
      offered, because guessing which of those means true is exactly the sort
      of silent wrong answer this whole feature exists to avoid. Narrow on
      purpose; widening it is a decision with its own evidence.
    * **Enum expansions are dropped.** Indigo publishes a state with a value
      set as booleans named ``parent.Value`` alongside the parent — Texecom's
      ``statusText`` yields ``statusText.Healthy``, ``.Tamper``, ``.Shorted``
      and ``.Tripped``. Offering all four next to the real ``status`` boolean
      turns a one-choice question into a five-choice one with four wrong
      answers, so a dotted key whose parent is also a state is suppressed.
    * **``onOffState`` leads.** It is the name Indigo itself gives the state
      behind ``dev.onState``, so a custom device publishing one has, in
      effect, already said which boolean is the device's reading — and it is
      the commonest custom boolean there is (156 devices on the reference
      database, against 24 for the next).
    """
    try:
        states = dict(getattr(dev, "states", None) or {})
    except Exception:  # pylint: disable=broad-except
        # `states` is a live proxy like everything else on a device; an
        # unreadable one means no candidates, never a raised picker.
        return ()
    names = set(states)
    keys = [name for name, value in states.items()
            if isinstance(value, bool) and not _is_enum_expansion(name, names)]
    keys.sort(key=lambda name: (_STATE_KEY_RANK.get(name, len(_STATE_KEY_RANK)), name))
    return tuple(keys)


def _has_numeric_state(dev) -> bool:
    """True if the device publishes a number but no boolean.

    Only used to choose between two exclusion reasons, so that a plugin device
    full of readings hears "not yet" rather than the flat "nothing to publish"
    that fits a device with no states at all. ``bool`` is excluded explicitly:
    it is a subclass of ``int`` in Python, and without that the two reasons
    would never distinguish anything.
    """
    try:
        values = dict(getattr(dev, "states", None) or {}).values()
    except Exception:  # pylint: disable=broad-except
        return False
    return any(isinstance(value, (int, float)) and not isinstance(value, bool)
               for value in values)


def _is_enum_expansion(name: str, names: set) -> bool:
    """True for ``parent.Value`` where ``parent`` is itself a state."""
    parent, _, suffix = name.partition(".")
    return bool(suffix) and parent in names


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
