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
  ``indigo.*`` call. Its return is a **tri-state** (``False`` / ``True`` / a
  reason string) so a command this role accepts but cannot act on is neither
  reported as a protocol error nor swallowed — see the method.

**E4 completed the table**: every role in ``bridge_protocol.ROLES`` now has a
handler, so :data:`BRIDGEABLE_ROLES` and that set are the same thing. The
``None`` return from :func:`handler_for` survives as the guard for a role a
*future* protocol version adds, which is not dead code — an unknown role fails
the **whole** ``attach`` on the node (E3a), so the caller must be able to ask
before it sends.

Each seam takes the export's ``options`` (§4.1) alongside the device. Only
``windowCovering`` reads them today — polarity is a covering concept
(``export_store.INVERTIBLE_ROLES``) — but the argument is threaded uniformly
rather than special-cased, because "this role's vocabulary depends on how the
user configured *this* export" is a property of the seam, not of one handler.

**Destructive commands never auto-confirm** (PRD §7). ``doorLock``'s
``lock``/``unlock`` call ``indigo.device.lock``/``unlock`` and stop there: no
optimistic state write, no synthesised confirmation, no read-back. The Matter
``lockState`` an ecosystem shows moves only when Indigo's own ``onState`` moves
and ``deviceUpdated`` pushes it — which is the whole point, because the bolt is
the authority and Indigo is the only thing that watches it. The node half
enforces the same rule from the other end (its ``lockDoor`` override does not
write state either).

Units are Indigo-natural at this boundary (§4.2): brightness, saturation and
covering position are 0–100 in **both** directions, hue is 0–360, colour
temperature is mireds on the wire and Kelvin in Indigo. The node owns every
Matter wire conversion.

**The unit assumption, stated once because it is a real limitation.** Indigo has
no canonical unit on any device: ``sensorValue``, ``temperatures`` and the
thermostat setpoints are whatever number the owning plugin wrote, and the IOM
records nothing about what it means. §4.2 names its keys in °C, %RH, lux, kPa
and m³/h, so this module **treats the Indigo reading as already being in the
protocol's unit** and converts nothing — the same assumption the inbound half
already documents ("Indigo values are treated as Celsius",
``matter_handlers/thermostat.py``). A °F thermostat will therefore export a
wrong-looking number. That is not fixable from the device model: it needs a
declared unit per export, which belongs with the role/polarity data
``indigo-device-catalog`` is the long-term home for (PRD §5.2 cross-repo note).
It is deliberately NOT inferred from ``export_catalog``'s unit heuristic — that
regex is a *default for the role picker*, and silently rescaling someone's
reading because their device is named "Attic Temp" would be far worse than a
number they can see is wrong.

**Pressure is the one documented exception, and it is not an exception to that
rule — it is the rule applied honestly.** The claim "both directions agree" was
true of every key but this one. Indigo's convention for a barometer, set by the
*inbound* half of this same plugin (``matter_handlers/sensors.py`` writes
``sensorValue`` in hPa and labels it " hPa") is hPa, and ``export_catalog``'s
own role heuristic routes ``\\bhpa\\b``/``\\bmbar\\b`` device names here. §4.2
names the wire key ``pressureKPa``. Those differ by exactly 10, so passing the
reading through unconverted was a factor-of-ten error that still renders
plausibly in an ecosystem — 1013 hPa arriving as 1013 kPa, ten atmospheres. See
:class:`PressureSensorExport`.
"""
from __future__ import annotations

import colorsys
import logging
from typing import Any, Callable, NamedTuple, Optional, Union

import indigo  # provided by the Indigo runtime

import export_catalog
from matter_handlers.color_control import kelvin_to_mireds, mireds_to_kelvin

#: Handlers are stateless singletons in :data:`HANDLERS` with no plugin logger
#: injected, so the one line they need to say goes to the module logger — the
#: same posture ``export_catalog`` takes. It is debug-only by design: everything
#: a user must act on is logged by ``export_bridge``, which knows the device.
#:
#: ``DoorLockExport`` is the one exception on both counts (issue #289 review
#: findings 2/3): it is not stateless — it holds a per-device relay-fallback
#: latch — and its fallback notice IS something a user must act on (it names
#: a device that cannot actually be locked/unlocked the way its export
#: implies). ``_LOG`` reaches no handler in production — live-verified:
#: root at WARNING with no handlers installed, so ``isEnabledFor(INFO)`` is
#: ``False`` and an INFO line through it is discarded before any handler ever
#: sees it (a WARNING-or-above line at least reaches the interpreter's
#: last-resort stderr handler, which is the only reason the other lines in
#: this module have ever appeared to work). ``DoorLockExport`` therefore logs
#: through :data:`_PLUGIN_LOG` instead — see its own comment.
_LOG = logging.getLogger(__name__)

#: The real plugin logger (Indigo SDK reference, "Logging from Another Class
#: or Submodule": ``self.logger``/``self.indigo_log_handler`` is always
#: ``logging.getLogger("Plugin")`` — the SDK's own documented way for a
#: submodule to reach the SAME Event Log handler ``self.logger`` uses,
#: without ``export_bridge`` threading a logger argument through every
#: handler for the sake of one line). Reserved for ``DoorLockExport``'s
#: relay-fallback notice (issue #289 review finding 2) — nothing else in
#: this module needs to reach a user this way.
_PLUGIN_LOG = logging.getLogger("Plugin")

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
STATE_POSITION = "position"
STATE_LOCKED = "locked"
STATE_OCCUPIED = "occupied"
STATE_CONTACT = "contact"
STATE_LEAK = "leak"
STATE_FREEZE = "freeze"
STATE_RAIN = "rain"
STATE_SMOKE = "smoke"
STATE_CO = "co"
STATE_TEMPERATURE_C = "temperatureC"
STATE_HUMIDITY_PCT = "humidityPct"
STATE_LUX = "lux"
STATE_PRESSURE_KPA = "pressureKPa"
STATE_FLOW_M3H = "flowM3h"
STATE_LOCAL_TEMPERATURE_C = "localTemperatureC"
STATE_HEATING_SETPOINT_C = "heatingSetpointC"
STATE_COOLING_SETPOINT_C = "coolingSetpointC"
STATE_SYSTEM_MODE = "systemMode"
#: Role-independent (§4.2 ``bridge_protocol.SHARED_STATE_KEYS``, issue #220) —
#: added by :meth:`ExportHandler.published_states`, never by a role's own
#: ``states_for``.
STATE_BATTERY_LEVEL = "batteryLevel"

COMMAND_ON_OFF = "onOff"
COMMAND_SET_LEVEL = "setLevel"
COMMAND_SET_COLOR_TEMP = "setColorTemp"
COMMAND_SET_COLOR = "setColor"
COMMAND_GO_TO_POSITION = "goToPosition"
COMMAND_STOP_MOTION = "stopMotion"
COMMAND_LOCK = "lock"
COMMAND_UNLOCK = "unlock"
COMMAND_SET_HEATING_SETPOINT = "setHeatingSetpoint"
COMMAND_SET_COOLING_SETPOINT = "setCoolingSetpoint"
COMMAND_SET_SYSTEM_MODE = "setSystemMode"

#: §4.1 ``options`` key carrying window-covering polarity. Spelled here rather
#: than imported from ``export_store`` so this module stays free of the store —
#: ``tests/test_export_handlers.py`` pins the two against each other.
OPTION_INVERT = "invert"

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

#: §4.2 ``pressureKPa`` per Indigo hPa. Indigo's barometer convention is hPa
#: (set by this plugin's own inbound half, ``matter_handlers/sensors.py``) and
#: the wire key is kPa; 1 hPa is 0.1 kPa. See the module docstring.
HPA_PER_KPA = 10.0


class StateDiff(NamedTuple):
    """What :meth:`ExportHandler.diff_with_gaps` found between two snapshots.

    ``changed`` is what goes on the wire. ``stopped`` is the keys the device
    *used* to answer and no longer does, which is not a value and so cannot be
    pushed — see :meth:`ExportHandler.diff_with_gaps` for why that is a
    decision rather than an omission.
    """

    changed: dict
    stopped: frozenset


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


def _position_from_brightness(brightness: float, options: Optional[dict]) -> int:
    """Indigo brightness ⇄ §4.2 ``position``, honouring the ``invert`` option.

    Its own inverse, which is why one function serves both directions: without
    ``invert`` it is the identity, and with it, ``100 - x`` applied twice is
    ``x``. Writing it once means the push and the command path cannot end up
    disagreeing about polarity — the failure that would leave a blind opening
    when the ecosystem says close.
    """
    value = int(_clamp(round(brightness), 0, 100))
    return 100 - value if (options or {}).get(OPTION_INVERT) is True else value


def _setpoint_of(args: dict) -> float:
    """The ``valueC`` of a setpoint command, or a ``ValueError`` naming the args."""
    value = args.get("valueC")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"setpoint command without a numeric valueC: {args!r}")
    return float(value)


def _first_number(values: Any) -> Optional[float]:
    """The first usable number in an Indigo list attribute, or ``None``.

    ``dev.temperatures`` is a list on a real thermostat and a MagicMock on a
    mocked one, so the iteration is guarded rather than assumed — the same
    reason :func:`_number` type-checks instead of calling ``bool``.

    **A literal ``[0.0]`` is reported, not treated as "no sensor".** It was
    proposed that it should be: some drivers do leave ``temperatures`` at
    ``[0.0]`` before their first poll, and exporting that reads as a house at
    freezing. The trade-off runs the other way, though — 0 °C is a real reading
    a real outdoor or unheated-room thermostat gives every winter, and
    suppressing it would silently freeze the ecosystem's display at whatever it
    last saw on exactly the days the number matters. A pre-poll zero corrects
    itself on the next update; a value this module refuses to report never
    arrives at all. Distinguishing the two needs a "has this device reported
    yet" signal the IOM does not offer.
    """
    if not isinstance(values, (list, tuple)):
        return None
    for value in values:
        if not isinstance(value, bool) and isinstance(value, (int, float)):
            return float(value)
    return None


def system_mode_of(dev: Any) -> Optional[str]:
    """``dev.hvacMode`` → the §4.2 ``systemMode`` string, or ``None``.

    The ``Program*`` variants collapse onto their base mode: Matter's
    ``SystemModeEnum`` has no "running its own schedule" value, and an ecosystem
    showing a thermostat as *off* because Indigo said ``ProgramHeat`` would be a
    worse lie than showing it as heating — which is what it is doing.

    Compared against ``indigo.kHvacMode`` members rather than against strings:
    the enum's ``str()`` is not part of the documented API, and the members are
    what ``setHvacMode`` takes back.
    """
    mode = getattr(dev, "hvacMode", None)
    if mode is None:
        return None
    hvac = indigo.kHvacMode
    for name, values in (
        ("off", (hvac.Off,)),
        ("heat", (hvac.Heat, hvac.ProgramHeat)),
        ("cool", (hvac.Cool, hvac.ProgramCool)),
        ("auto", (hvac.HeatCool, hvac.ProgramHeatCool)),
    ):
        if any(mode is value or mode == value for value in values):
            return name
    return None


def hvac_mode_for(mode: Any) -> Optional[Any]:
    """§4.2 ``systemMode`` → the ``indigo.kHvacMode`` member, or ``None``.

    ``"auto"`` becomes ``HeatCool``, not ``ProgramHeatCool``: the user asked for
    both setpoints to be honoured, not for the thermostat's built-in programme
    to take over. The ``Program*`` modes are readable (:func:`system_mode_of`)
    but never *written* — nothing in Matter asks for one.
    """
    hvac = indigo.kHvacMode
    return {
        "off": hvac.Off,
        "heat": hvac.Heat,
        "cool": hvac.Cool,
        "auto": hvac.HeatCool,
    }.get(mode) if isinstance(mode, str) else None


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


#: Devices whose ``battery_percent`` has already warned about a >100 raw
#: reading. Once per device rather than once per call: a stuck driver reports
#: the same bad value on every poll, and a per-poll warning would be the
#: loudest line in the log for a condition that never changes. Module-level
#: rather than a handler field because :func:`battery_percent` is a free
#: function several handlers' ``published_states`` call directly, with no
#: shared instance to hold it on.
_overrange_warned: set[int] = set()


def battery_percent(dev: Any) -> Optional[int]:
    """``dev.batteryLevel`` as the §4.2 ``batteryLevel`` percentage, or ``None``.

    Rounded FIRST, suppressed second: a value that rounds to 0 or below is
    ``None``, whatever it was before rounding. ``None`` for a non-numeric
    value too. The suppression is the deliberate opposite of
    :func:`_first_number`'s temperature ruling elsewhere in this module, and
    not an accident. Indigo initialises every declared Integer state to 0 at
    device creation (issue #190 — see ``device_sync.py``'s
    ``_warn_of_newly_excluded_battery_devices`` docstring for where that was
    established), so a bare 0 on a freshly commissioned device is
    indistinguishable from "never polled". Unlike 0 °C — a real reading a
    working outdoor thermostat gives every winter — 0% is not a reading a
    working device CAN give: a cell that
    flat has stopped talking, not reported one last data point. Reporting a
    fresh device's placeholder 0 as a battery level is exactly the false "your
    device's battery is critically low" alarm issue #220 exists to avoid, and
    that reasoning covers a negative reading at least as strongly — a driver's
    "unknown" sentinel (e.g. -1) is even less a real percentage than 0 is.

    Rounding before suppressing (rather than after) closes a real gap: the
    old order compared the RAW value to 0, so only an exact ``0`` was
    suppressed — 0.4, 0.49 and 0.5 all failed that comparison, rounded down
    (or to even) to 0 anyway, and still reached the wire as a literal ``0``,
    which is exactly the false-alarm value this function exists to keep off
    it. Rounding first makes "is this reading a real >0 percentage once
    rounded" the actual question asked, which is what the suppression was
    always meant to answer.

    Clamped to 100 at the top, and now warned about once per device: a
    ``batteryLevel`` above 100 is a device/driver bug, not a value a working
    battery can report, so letting it through silently would just move the
    failure to the node's own ``percentToBatteryRemaining`` clamp (a debug
    line there, naming no device) for no better an answer. This is the one
    line in the module that logs above debug — see :data:`_overrange_warned`.
    """
    value = getattr(dev, "batteryLevel", None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value > 100 and dev.id not in _overrange_warned:
        _overrange_warned.add(dev.id)
        _LOG.warning(
            "Matter bridge: device %s (id %s) reported batteryLevel %r, above the 0-100 domain — "
            "clamped to 100.", getattr(dev, "name", ""), dev.id, value)
    percent = min(100, int(round(value)))
    if percent <= 0:
        return None
    return percent


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
class ExportHandler:
    """One §4.2 role's outbound vocabulary."""

    #: The §4.2 role this handler serves.
    role: str = ""
    #: Per-key diff tolerances; anything absent compares exactly.
    tolerances: dict[str, float] = {}
    #: §4.2 keys this role never publishes together — one replaces another in
    #: the same snapshot rather than the device losing the ability to report
    #: it (issue #282). Empty for every role except ``extendedColorLight``;
    #: see :data:`ExtendedColorLightExport.mode_alternating_keys` for why it
    #: exists and how ``ExportBridge`` uses it.
    mode_alternating_keys: frozenset = frozenset()

    # -- state (plugin → node) ------------------------------------------
    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        """The full §4.2 state snapshot for ``dev``.

        Keys whose value the device cannot supply are **omitted**, never
        defaulted: ``set_state`` args are a partial map by design (§3.4), and a
        fabricated 0 would be pushed to every ecosystem as fact.

        ``options`` is the export entry's §4.1 ``options`` map. Most roles have
        none and ignore it; ``windowCovering`` reads its polarity from it.
        """
        raise NotImplementedError

    def published_states(self, dev: Any, options: Optional[dict] = None) -> dict:
        """:meth:`states_for`, plus ``batteryLevel`` when the device has one.

        The ONE place battery is added, deliberately outside every role's own
        override chain (issue #220). Six of the fifteen handlers below skip
        ``super().states_for()`` entirely (``OnOffExport``,
        ``WindowCoveringExport``, ``DoorLockExport``, ``ThermostatExport``,
        ``BinarySensorExport``, ``NumericSensorExport``), so a battery key
        added inside ``states_for`` itself would silently vanish for exactly
        those roles — the Wrong Abstraction fifteen near-identical additions
        would be, and the reason this is a wrapper rather than a mixin method
        any subclass could accidentally shadow.

        :meth:`diff_with_gaps` and :meth:`diff_from` both read through THIS,
        never through ``states_for`` directly — otherwise ``batteryLevel``
        would show as permanently "stopped" on every single diff, since
        ``states_for`` never had it to begin with.
        """
        states = self.states_for(dev, options)
        percent = battery_percent(dev)
        if percent is not None:
            states = {**states, STATE_BATTERY_LEVEL: percent}
        return states

    def diff(self, orig_dev: Any, new_dev: Any, options: Optional[dict] = None) -> dict:
        """The changed §4.2 keys only. Empty dict = nothing to push.

        Both snapshots are taken with the **same** ``options``: polarity is a
        property of the export, not of a moment, so comparing an inverted
        reading against a non-inverted one would report a change on every
        config edit and, worse, push the wrong one.

        A key the device has **stopped** answering produces nothing here; see
        :meth:`diff_with_gaps`, which is the seam callers that need to notice
        should use.
        """
        return self.diff_with_gaps(orig_dev, new_dev, options).changed

    def diff_with_gaps(self, orig_dev: Any, new_dev: Any,
                       options: Optional[dict] = None) -> StateDiff:
        """:meth:`diff`, plus the keys that stopped being reported at all.

        **The decision, because it is not obvious and it is not free.** A
        published key can *vanish*: a battery sensor whose ``sensorValue`` goes
        ``None`` on a dead cell, a lock whose ``onState`` goes ``None`` when its
        driver loses the device. ``states_for`` correctly omits it — §3.4 state
        maps are partial by design and a fabricated 0/False would be pushed to
        every ecosystem as fact — so ``after`` simply has one fewer key, and
        iterating ``after`` finds nothing to say.

        §4.2 has **no spelling for "I no longer know"**: every state key is a
        value, and ``set_state`` cannot carry an absence. Nor does a reconnect
        heal it — ``attach`` sends the same partial snapshot and §3.4 leaves
        keys it was not given untouched. So the ecosystem keeps the last value
        it was told, forever, and a lock that stopped reporting shows "Locked"
        long after nobody knows whether it is.

        Keeping the last known good value is still the *right* answer — the
        alternatives are inventing a state or flapping the accessory — but it
        must not be a **silent** one. The gap is returned rather than logged
        here (handlers are stateless singletons with no device context and no
        latch), so ``export_bridge`` can say it once per device per streak.
        Spelling an explicit "unknown" on the wire would be a protocol
        addition, deliberately not made here.
        """
        return self.diff_from(self.published_states(orig_dev, options), new_dev, options)

    def diff_from(self, pushed: Optional[dict], new_dev: Any,
                  options: Optional[dict] = None) -> StateDiff:
        """:meth:`diff_with_gaps` against a snapshot instead of a device (E5).

        The snapshot callers pass is the one they last actually **pushed**, and
        the difference from ``diff_with_gaps`` is the whole reason this exists:
        a tolerance compares against whatever ``before`` is, so comparing
        against the *previous Indigo reading* lets a change smaller than the
        tolerance happen over and over and never be sent. A hue ramping one
        degree per step against ``HUE_TOLERANCE_DEGREES`` of 1 pushes nothing,
        ever, while the accessory drifts arbitrarily far from the lamp. Diffing
        against the last pushed value bounds that error at exactly the
        tolerance, which is what a tolerance is supposed to mean.

        ``pushed`` of ``None`` means "nothing has been pushed for this device"
        and every readable key is treated as changed — the safe direction: an
        unnecessary ``set_state`` is idempotent, a missed one is stale state in
        somebody's Home app.

        ``stopped`` excludes :data:`mode_alternating_keys` **only when the new
        snapshot still carries at least one of them** (issue #282 trap (a)). A
        genuine mode flip always publishes the replacement channel — that is
        what a mode flip *is* — so "some mode-alternating key is in ``after``"
        is the actual signal that this is the mechanism working, not a device
        that stopped answering colour: ``ExportBridge._report_stopped_keys``
        would otherwise warn "stopped reporting hue, saturation" on every
        single mode flip. A device that stops answering colour ENTIRELY —
        driver loses it, no RGB, no white temperature, no colour mode — has
        *no* mode-alternating key in ``after`` either, so the exemption does
        not apply and the loss is reported like any other. A genuine stoppage
        of any other key is unaffected either way.
        """
        before = dict(pushed or {})
        after = self.published_states(new_dev, options)
        changed = {
            key: value for key, value in after.items()
            if self._changed(key, before.get(key, _MISSING), value)
        }
        stopped = frozenset(before) - frozenset(after)
        if frozenset(after) & self.mode_alternating_keys:
            stopped -= self.mode_alternating_keys
        return StateDiff(changed, stopped)

    def _changed(self, key: str, before: Any, after: Any) -> bool:
        if before is _MISSING:
            return True
        tolerance = self.tolerances.get(key)
        if tolerance is not None and isinstance(before, (int, float)) \
                and isinstance(after, (int, float)):
            return abs(after - before) > tolerance
        return before != after

    # -- commands (node → plugin) ---------------------------------------
    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        """``command name → handler``. Subclasses extend, never replace.

        Each handler takes ``(args, dev, options)``. Most ignore the third —
        naming it ``_options`` there — but the signature is uniform so a role
        that needs the export's configuration does not have to be a special
        case in :meth:`dispatch`.

        A handler returns ``None`` when it did something, or a short reason
        string when it lawfully did **nothing** (see
        :meth:`WindowCoveringExport._stop_motion`, the only one today).
        """
        return {}

    def dispatch(self, command: str, args: dict, dev: Any,
                 options: Optional[dict] = None) -> Union[bool, str]:
        """Run one §4.2 command against ``dev``. Tri-state.

        * ``False`` — not this role's command. The caller logs it: only it
          knows which device and which role were involved, which is the version
          of that line worth having;
        * ``True`` — dispatched to Indigo;
        * a **reason string** — the command is this role's, was accepted, and
          changed nothing. That is a third outcome and it used to be squashed
          into ``True``: ``stopMotion`` on a dimmer-modelled blind returned
          "handled" and vanished, so a user pressing stop in the Home app saw
          the blind carry on with nothing anywhere to explain it. A truthy
          string keeps every existing ``if not dispatch(...)`` call site correct
          while giving the caller — which owns the device, the log and the
          once-per-streak latch — something to say.
        """
        handler = self.commands().get(command)
        if handler is None:
            return False
        return handler(dict(args or {}), dev, dict(options or {})) or True


class OnOffExport(ExportHandler):
    """``onOffPlugInUnit`` / ``onOffLight`` — the whole of the relay export."""

    role = export_catalog.ROLE_ON_OFF_PLUG

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        return {STATE_ON_OFF: bool(getattr(dev, "onState", False))}

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {**super().commands(), COMMAND_ON_OFF: self._on_off}

    @staticmethod
    def _on_off(args: dict, dev: Any, _options: dict) -> None:
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

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        states = super().states_for(dev, options)
        brightness = _number(dev, "brightness")
        if brightness is not None:
            states[STATE_LEVEL] = int(_clamp(round(brightness), 0, 100))
        return states

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {**super().commands(), COMMAND_SET_LEVEL: self._set_level}

    @staticmethod
    def _set_level(args: dict, dev: Any, _options: dict) -> None:
        level = args.get(STATE_LEVEL)
        if not isinstance(level, (int, float)) or isinstance(level, bool):
            raise ValueError(f"setLevel without a numeric level: {args!r}")
        indigo.dimmer.setBrightness(dev, value=int(_clamp(round(level), 0, 100)))


class ColorTemperatureLightExport(DimmableLightExport):
    """``colorTemperatureLight`` — adds ``colorTempMireds`` over Indigo's Kelvin."""

    role = export_catalog.ROLE_COLOR_TEMPERATURE_LIGHT

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        states = super().states_for(dev, options)
        kelvin = _number(dev, "whiteTemperature")
        if kelvin:  # 0 and None both mean "this device is not telling us"
            states[STATE_COLOR_TEMP_MIREDS] = int(
                _clamp(kelvin_to_mireds(kelvin), MIREDS_MIN, MIREDS_MAX))
        return states

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {**super().commands(), COMMAND_SET_COLOR_TEMP: self._set_color_temp}

    @staticmethod
    def _set_color_temp(args: dict, dev: Any, _options: dict) -> Optional[str]:
        """Set white temperature, preserving the white channel's own level.

        ``setColorLevels`` documents whiteTemperature as used *in combination
        with* whiteLevel, so sending the temperature alone risks a driver
        reading whiteLevel as 0 and turning the lamp off — a colour tweak that
        blacks out the room is a worse bug than a colour tweak that misses.
        "Preserve the white channel's own level" still stands as the goal, but
        for a LIT lamp the source of truth for that level is ``brightness``,
        not the stored ``whiteLevel`` — issue #281 proved live that
        ``whiteLevel`` does not track brightness on channel-publishing drivers
        (z2m: ``whiteLevel`` sat at 0.0, then stale at 20, while ``brightness``
        was actually 29, then 49, then 69). Sending the stored ``whiteLevel``
        with every colour-temperature write published a literal
        ``{"brightness": 0}`` on the wire and switched an ON lamp OFF on every
        single Apple adaptive-lighting tick (~3s). For an OFF lamp
        (``brightness`` 0 or ``None``) the stored ``whiteLevel`` is still what
        is sent — the two existing off-lamp pins below (a lamp reporting
        ``whiteLevel`` 0, and one reporting some other stored level) remain
        the contract for that case; only the LIT branch changed. Gate and
        source both read ``brightness`` so they cannot disagree with each
        other: :class:`DimmableLightExport`'s ``states_for`` already treats
        ``brightness`` as the export-side level, and :func:`_number` keeps the
        read ``None``-safe, falling back to today's off-lamp behaviour when it
        is absent. (A future shape banked but out of scope here: matter.js
        ``executeIfOff`` colour staging, referenced on issue #281, would let a
        colour-temperature write land on an OFF lamp without touching its
        on/off state at all — not needed while the write already preserves an
        off lamp's stored level.)

        Two guards, all about not depending on someone else to be careful:

        * **the mireds are clamped here.** The node clamps too, but that is the
          *other process*: an unclamped 1 mired is a 1,000,000 K write and 10000
          mireds is 100 K, both outside Indigo's own 1200–15000 domain, and both
          would reach the driver if the node ever stopped clamping or a command
          arrived from anywhere else. ``round`` rather than ``int`` because
          truncating 369.9 to 369 is a different colour, not a rounding detail.
        * **no white channel means no command.** ``whiteLevel is None`` is real
          Indigo for "this device has no white channel at all" — inventing 100
          for it asks its driver to drive something it does not have, so the
          command is skipped entirely. A whiteLevel of **0** is a different
          answer to a different question: the channel exists and is currently
          off, and 0 is the *real* value, so it is sent as 0. The `or 100`
          default that used to sit here caught it — `0.0` is falsy — and turned
          "set the colour temperature of a lamp that is off" into "set the
          colour temperature AND switch the white channel to full", which is a
          bridge turning a light on that nobody asked it to. Matter keeps
          colour and on/off orthogonal for exactly this reason: a
          `MoveToColorTemperature` on an off lamp changes its colour, not its
          state.

        A third guard used to sit here too: **RGB was zeroed alongside.**
        This zeroed ``redLevel``/``greenLevel``/``blueLevel`` alongside the
        temperature write, because Matter's ``colorMode`` is one-of and the
        node's push side picks hue/saturation over colour temperature when a
        device reports both (``bridge-node/src/endpoints.ts`` ``colorPatch``)
        — an RGBW lamp left holding stale RGB levels would report a colour we
        did not set, and the node would believe it over the temperature we
        just wrote. That reasoning was correct but the fix was in the wrong
        process: zeroing here is a **hardware** ``setColorLevels`` call, so a
        channel-publishing driver (z2m) puts ``{"color":{"r":0,"g":0,"b":0}}``
        on the wire and the bulb actually goes to deep blue before the
        temperature write lands (issue #282). It has been **removed**;
        coherence for the *node's* colour-mode belief now lives on the
        reporting side instead — :meth:`ExtendedColorLightExport.states_for`
        publishes exactly one colour channel per snapshot, chosen by the
        device's own colour mode — so ``colorPatch`` derives the right mode
        from which keys arrive, without this handler ever having to touch a
        channel nobody asked it to change. Switching the lamp's own hardware
        mode per write is the bulb/driver's job, and z2m already does it.
        """
        mireds = args.get(STATE_COLOR_TEMP_MIREDS)
        if not isinstance(mireds, (int, float)) or isinstance(mireds, bool) or not mireds:
            raise ValueError(f"setColorTemp without usable mireds: {args!r}")
        white_level = _number(dev, "whiteLevel")
        if white_level is None:
            # The tri-state reason, not a bare `return`. `dispatch` ends in
            # `handler(...) or True`, so returning None reported SUCCESS for a
            # command that did nothing at all — the ecosystem had already
            # flipped its tile to the new colour temperature, the lamp never
            # moved, and the only trace was a debug line from a static method
            # that cannot name the device. The caller latches this per device.
            return ("the Indigo device has no white channel, so there is no colour "
                    "temperature to set")
        brightness = _number(dev, "brightness")
        level = brightness if brightness is not None and brightness > 0 else white_level
        levels: dict[str, int] = {
            "whiteLevel": int(_clamp(round(level), 0, 100)),
            "whiteTemperature": mireds_to_kelvin(
                int(_clamp(round(mireds), MIREDS_MIN, MIREDS_MAX))),
        }
        indigo.dimmer.setColorLevels(dev, **levels)
        return None


class ExtendedColorLightExport(ColorTemperatureLightExport):
    """``extendedColorLight`` — adds ``hue``/``saturation`` over Indigo's RGB."""

    role = export_catalog.ROLE_EXTENDED_COLOR_LIGHT
    tolerances = {STATE_HUE: HUE_TOLERANCE_DEGREES}

    #: §4.2 keys this role alternates between depending on the device's colour
    #: mode — never published together (see :meth:`states_for`), so a switch
    #: from one to the other is not a device that "stopped reporting" the keys
    #: it dropped. ``ExportBridge._report_stopped_keys`` and ``_note_pushed``
    #: both consult this set: the former to not warn on a mode flip, the
    #: latter to evict the suppressed channel's stale value so it is resent in
    #: full the next time the device returns to it (issue #282 trap (b)).
    mode_alternating_keys = frozenset({STATE_COLOR_TEMP_MIREDS, STATE_HUE, STATE_SATURATION})

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        """Exactly one colour channel per snapshot — never CT *and* hue/sat.

        Matter's ``colorMode`` is one-of, and the fix for issue #282 moved
        keeping the node's belief coherent from the *write* side (zeroing the
        channel not in use, which was a real hardware command) to here: what
        the node believes is built from what we tell it, so telling it about
        only the active channel is sufficient and it never has to guess.
        ``bridge-node``'s ``colorPatch`` derives ``colorMode`` per-push from
        key presence — hue/sat wins only if sent — so sending one channel here
        **is** the mode signal; no bridge change needed.

        Mode source, in order:

        1. ``dev.states.get("colorMode")`` — z2m vocabulary: ``"color_temp"``
           means the CT channel is live; ``"xy"``/``"hs"`` mean hue/sat is.
           Read as ``dict(getattr(dev, "states", None) or {})``, the same
           guarded-and-caught shape :func:`export_catalog.custom_boolean_state_keys`
           (~line 587) and :func:`export_catalog._has_numeric_state` (~line 609)
           already use for exactly this reason — ``.states`` is a live proxy
           like every other device attribute, so an exception here degrades to
           "no ``colorMode``" the same way this file already chose elsewhere,
           rather than taking the whole snapshot down with it.
           :meth:`BinarySensorExport._mapped_reading` (line ~1069) reads it the
           same way for its own state-key lookup, so this is not the export
           subsystem's first ``.states`` read — just the first one keyed on a
           *fixed* name (``colorMode``) rather than a user-chosen one.
        2. A bogus value (not one of the three above — a non-z2m driver using
           different vocabulary, or garbage) falls through to the heuristic
           below rather than raising: an unrecognised mode is still a mode
           that has to resolve to *something* single-channel.
        3. ``colorMode`` absent entirely (non-z2m plugins) — heuristic: RGB
           all zero/absent **and** a usable ``whiteTemperature`` means CT is
           the channel actually driving the lamp right now; anything else
           (RGB non-zero, or no CT to fall back on) means hue/sat.
        """
        states = DimmableLightExport.states_for(self, dev, options)
        red = _number(dev, "redLevel")
        green = _number(dev, "greenLevel")
        blue = _number(dev, "blueLevel")
        kelvin = _number(dev, "whiteTemperature")
        try:
            color_mode = dict(getattr(dev, "states", None) or {}).get("colorMode")
        except Exception:  # pylint: disable=broad-except
            color_mode = None
        if color_mode == "color_temp":
            use_color_temp = True
        elif color_mode in ("xy", "hs"):
            use_color_temp = False
        else:
            # Known limitation, accepted rather than engineered around: a
            # non-z2m RGBW driver that never publishes `colorMode` AND leaves
            # RGB levels populated after a CT write will keep reading as
            # hue/sat here — the pre-#282 zeroing used to make that case look
            # CT-mode by clearing RGB as a side effect, and this heuristic has
            # no other signal to replace it with. The wire command is still
            # correct (CT-only, per `_set_color_temp`); only the REPORTED tile
            # can go stale for that one driver family. Revisit if a real
            # report surfaces from a non-colorMode RGBW plugin — not before,
            # because inventing a heuristic for a driver nobody has seen risks
            # trading a rare stale tile for a common wrong one.
            rgb_is_zero = all(value in (None, 0) for value in (red, green, blue))
            use_color_temp = bool(rgb_is_zero and kelvin)
        if use_color_temp:
            if kelvin:  # 0 and None both mean "this device is not telling us"
                states[STATE_COLOR_TEMP_MIREDS] = int(
                    _clamp(kelvin_to_mireds(kelvin), MIREDS_MIN, MIREDS_MAX))
        elif None not in (red, green, blue):
            hue, saturation = rgb_to_hue_saturation(red, green, blue)
            states[STATE_SATURATION] = saturation
            # Below the floor the recovered hue is noise, not colour — see
            # SATURATION_HUE_FLOOR for the measured error table.
            if saturation >= SATURATION_HUE_FLOOR:
                states[STATE_HUE] = hue
        return states

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {**super().commands(), COMMAND_SET_COLOR: self._set_color}

    @staticmethod
    def _set_color(args: dict, dev: Any, _options: dict) -> None:
        """Set hue/saturation as Indigo RGB levels.

        Ecosystems send ``setColor`` **twice with identical values** (E3a, seen
        live) because Matter carries hue and saturation as separate attribute
        writes. No de-duplication is attempted: ``setColorLevels`` takes
        absolute values, so the second call is a no-op at the lamp, and
        suppressing it on "we already believe that" would silently skip the
        first call too whenever Indigo's belief has drifted from the hardware.
        Idempotent beats clever.

        **The white channel is left alone.** This used to zero ``whiteLevel``
        alongside, with the same colour-mode reasoning as
        :meth:`ColorTemperatureLightExport._set_color_temp` — but that zero is
        a hardware ``setColorLevels`` call too, and a channel-publishing
        driver (z2m) puts ``{"brightness":0}`` on the wire, switching the lamp
        off the instant a user picks a colour (issue #282). Coherence for the
        node's colour-mode belief now lives on the reporting side
        (:meth:`ExtendedColorLightExport.states_for`), so this handler no
        longer has to touch a channel nobody asked it to change; the bulb's
        own driver switches its hardware mode per write, exactly as it does
        for :meth:`ColorTemperatureLightExport._set_color_temp`.
        """
        hue = args.get(STATE_HUE)
        saturation = args.get(STATE_SATURATION)
        for name, value in ((STATE_HUE, hue), (STATE_SATURATION, saturation)):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"setColor without a numeric {name}: {args!r}")
        red, green, blue = hue_saturation_to_rgb(hue, saturation)
        levels = {"redLevel": red, "greenLevel": green, "blueLevel": blue}
        indigo.dimmer.setColorLevels(dev, **levels)


class WindowCoveringExport(ExportHandler):
    """``windowCovering`` — an Indigo dimmer's brightness read as open-ness.

    §4.2's ``position`` is 0–100 with **100 = open**, which is the inbound
    convention (``matter_handlers/window_covering.py`` maps Matter's
    percent-closed onto an Indigo brightness the same way round). So the default
    mapping is the identity, and the polarity that *does* vary — a motor wired so
    that Indigo brightness 100 drives the blind shut — is the per-export
    ``invert`` option (§4.1, ``export_store.OPTION_INVERT``).

    **The inversion is applied here, plugin-side, and only here.** By the time a
    ``position`` reaches the wire it always means "100 = open", whatever the
    hardware does; the node then owns the single further inversion into Matter's
    percent-closed. Two inversions in two processes, each documented at its own
    boundary, and neither one guessing what the other did.
    """

    role = export_catalog.ROLE_WINDOW_COVERING

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        brightness = _number(dev, "brightness")
        if brightness is None:
            return {}
        return {STATE_POSITION: _position_from_brightness(brightness, options)}

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {
            **super().commands(),
            COMMAND_GO_TO_POSITION: self._go_to_position,
            COMMAND_STOP_MOTION: self._stop_motion,
        }

    @staticmethod
    def _go_to_position(args: dict, dev: Any, options: dict) -> None:
        position = args.get(STATE_POSITION)
        if not isinstance(position, (int, float)) or isinstance(position, bool):
            raise ValueError(f"goToPosition without a numeric position: {args!r}")
        indigo.dimmer.setBrightness(
            dev, value=_position_from_brightness(_clamp(round(position), 0, 100), options))

    @staticmethod
    def _stop_motion(_args: dict, _dev: Any, _options: dict) -> Optional[str]:
        """Declared, honoured by doing nothing, and that is the correct answer.

        A covering exported through a *dimmer* has no stop: Indigo's dimmer
        device class offers ``SetBrightness``/``BrightenBy``/``DimBy`` and no
        halt-where-you-are action, which is the same gap the inbound handler
        records ("StopMotion has no Indigo dimmer action equivalent — omitted").
        Nothing can be synthesised either — stopping at the current position
        would need the position *while moving*, and Indigo only learns the
        brightness once the driver reports the move finished.

        It stays in :meth:`commands` rather than being left out because the
        alternative is worse in two ways: §4.2 lists ``stopMotion`` in this
        role's vocabulary (the zoo test enforces that both sides agree), and an
        undeclared command makes ``export_bridge`` log "a command that role does
        not define" — which reads like a protocol bug rather than a known
        limitation of modelling a blind as a dimmer.

        It returns the reason rather than writing a debug line of its own. A
        module-level debug from a stateless singleton could not name the export
        or throttle itself, so the one person who needs it — a user who pressed
        stop in the Home app and watched the blind keep going — would never see
        it. ``export_bridge`` says it once per device per streak instead.
        """
        return ("Indigo offers no stop action for a covering modelled as a dimmer, "
                "so the blind will finish its travel")


class DoorLockExport(ExportHandler):
    """``doorLock`` — an Indigo relay where **on = locked**.

    The polarity mirrors the inbound handler exactly
    (``matter_handlers/door_lock.py`` maps Matter ``LockState.Locked`` onto
    ``onOffState`` ``True``), so a Matter lock commissioned *into* Indigo and an
    Indigo lock exported *out* of it agree on what "on" means.

    PRD §7 in one sentence: **dispatch and stop.** No optimistic state write, no
    synthesised confirmation, no read-back-and-hope. ``locked`` follows Indigo's
    ``onState``, which follows the actual bolt.
    """

    role = export_catalog.ROLE_DOOR_LOCK

    def __init__(self) -> None:
        #: Device ids the relay-fallback notice (issue #289) has already been
        #: said for. A WORKING fallback is not a fault, so it earns one INFO
        #: line per device — not a warning repeated on every lock/unlock.
        self._relay_fallback_logged: set[int] = set()

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        on_state = getattr(dev, "onState", None)
        # `None` is real Indigo for "this device does not report on/off", and a
        # lock whose state is unknown must not be reported as unlocked.
        return {} if on_state is None else {STATE_LOCKED: bool(on_state)}

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {
            **super().commands(),
            COMMAND_LOCK: self._lock,
            COMMAND_UNLOCK: self._unlock,
        }

    def _lock(self, _args: dict, dev: Any, _options: dict) -> None:
        self._drive(dev, locked=True)

    def _unlock(self, _args: dict, dev: Any, _options: dict) -> None:
        self._drive(dev, locked=False)

    def _drive(self, dev: Any, *, locked: bool) -> None:
        """Issue #289: a relay a user declared ``doorLock`` must be as
        DRIVABLE as it is READABLE.

        ``indigo.device.lock``/``unlock`` only work for a device whose
        ``pluginProps["IsLockSubType"]`` is set (Indigo SDK, `Lock`/`Unlock`)
        — the OWNING plugin's declaration, checked statically here rather
        than by catching IndigoServer's ``TypeError``, so a real lock's
        command NEVER silently becomes the relay path even if ``lock``/
        ``unlock`` itself were to fail for some other reason.

        **The namespace trap (issue #289 review finding 1 — this exact
        mistake shipped once).** ``dev.pluginProps`` is the CALLING script's
        own slot in the device's per-plugin props map, not the owning
        plugin's — live-probed on jarvis: a native Z-Wave relay showed
        ``pluginProps={}`` from here while ``ownerProps`` carried its real
        61 props (Indigo SDK, "Generating a Dictionary for a Device":
        ``globalProps`` is keyed by owning plugin id, ``pluginProps`` is
        this script's own entry in it, ``ownerProps`` is the flat
        convenience view of the OWNER's entry regardless of who is asking).
        Since :func:`export_catalog.classify` structurally excludes
        self-owned devices (the loop guard), every device this handler ever
        sees is owned by someone else — so reading ``pluginProps`` here
        would read ``IsLockSubType`` as unset for literally every exportable
        device, including real Z-Wave/Yale locks, misrouting them all to the
        relay fallback. ``ownerProps`` (falling back to
        ``globalProps[dev.pluginId]`` for anything that predates it) is the
        only read that can ever come back populated.

        For anything that is not a lock sub-type — a plain relay, exactly
        the shape :meth:`states_for` already reads ``onState`` from and
        reports as the bolt — this used to just refuse forever: the refusal
        comment here argued a ``turnOn`` fallback would drive "a path with
        no lock semantics", but ``states_for`` was already treating that
        relay's ``onState`` AS the lock semantics. Trusting the state
        direction while refusing the command direction was the actual
        inconsistency (issue #289); the node side already relies on this
        exact polarity (``matter_handlers/door_lock.py``, ``bridge-node``'s
        ``IndigoDoorLockServer``), so driving it here is not a new
        assumption, only the missing half of one already made. A device
        that genuinely cannot be driven (``turnOn``/``turnOff`` itself
        raises) still fails loudly through :meth:`ExportBridge._apply_command`'s
        existing error path — nothing here swallows that.

        **Residual risk, unverified**: no lock was paired on jarvis to
        confirm a real Z-Wave/Yale lock's ``ownerProps`` actually carries
        ``IsLockSubType`` for its OWNING plugin the way the dictionary
        reference shows for a plain Z-Wave relay. If some plugin's lock
        device does not set it there, this degrades to the relay fallback —
        which historically is exactly how such locks were driven before
        ``IsLockSubType`` existed — and the notice below is what makes that
        visible instead of silent.
        """
        props = (getattr(dev, "ownerProps", None)
                or (getattr(dev, "globalProps", None) or {}).get(getattr(dev, "pluginId", ""), {})
                or {})
        is_lock = bool(props.get("IsLockSubType"))
        if is_lock:
            (indigo.device.lock if locked else indigo.device.unlock)(dev)
            return
        if dev.id not in self._relay_fallback_logged:
            self._relay_fallback_logged.add(dev.id)
            # `_PLUGIN_LOG`, not `_LOG` — see the module header. This line is
            # exactly the kind `_LOG` was built for (debug-only, no user
            # needs it) EXCEPT that a user does need it: it names a device
            # whose export cannot work the way its role implies.
            _PLUGIN_LOG.info(
                "Matter bridge: device %s (%s) is exported as doorLock but is not a lock "
                "sub-type — driving its lock/unlock commands as the relay it actually is "
                "(turnOn=locked, turnOff=unlocked, matching states_for's own polarity).",
                dev.id, getattr(dev, "name", "?"))
        (indigo.device.turnOn if locked else indigo.device.turnOff)(dev)


class ThermostatExport(ExportHandler):
    """``thermostat`` — setpoints, the current temperature, and the mode.

    Reads the documented ``ThermostatDevice`` attributes: ``temperatures``
    (a list, one per sensor), ``heatSetpoint``, ``coolSetpoint`` and
    ``hvacMode``. No fan in v1 — the FanControl descope (PRD §5.2) applies here
    too, so there is no fan key and no fan command.

    Temperatures are passed through unconverted; see the module docstring on why
    "°C" is an assumption Indigo cannot confirm.
    """

    role = export_catalog.ROLE_THERMOSTAT

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        states: dict = {}
        # Matter's Thermostat has exactly ONE `localTemperature`, while Indigo
        # allows up to three sensors. The first is the one Indigo itself treats
        # as primary (`temperatureInput1`), so it is what the ecosystem sees;
        # the others have no Matter home on this device type.
        temperature = _first_number(getattr(dev, "temperatures", None))
        if temperature is not None:
            states[STATE_LOCAL_TEMPERATURE_C] = temperature
        heat = _number(dev, "heatSetpoint")
        if heat is not None:
            states[STATE_HEATING_SETPOINT_C] = heat
        cool = _number(dev, "coolSetpoint")
        if cool is not None:
            states[STATE_COOLING_SETPOINT_C] = cool
        mode = system_mode_of(dev)
        if mode is not None:
            states[STATE_SYSTEM_MODE] = mode
        return states

    def commands(self) -> dict[str, Callable[[dict, Any, dict], Optional[str]]]:
        return {
            **super().commands(),
            COMMAND_SET_HEATING_SETPOINT: self._set_heating_setpoint,
            COMMAND_SET_COOLING_SETPOINT: self._set_cooling_setpoint,
            COMMAND_SET_SYSTEM_MODE: self._set_system_mode,
        }

    @staticmethod
    def _set_heating_setpoint(args: dict, dev: Any, _options: dict) -> None:
        indigo.thermostat.setHeatSetpoint(dev, value=_setpoint_of(args))

    @staticmethod
    def _set_cooling_setpoint(args: dict, dev: Any, _options: dict) -> None:
        indigo.thermostat.setCoolSetpoint(dev, value=_setpoint_of(args))

    @staticmethod
    def _set_system_mode(args: dict, dev: Any, _options: dict) -> None:
        mode = args.get(STATE_SYSTEM_MODE)
        hvac_mode = hvac_mode_for(mode)
        if hvac_mode is None:
            raise ValueError(f"setSystemMode with a mode outside the §4.2 domain: {args!r}")
        indigo.thermostat.setHvacMode(dev, value=hvac_mode)


#: Sentinel for "this export declares no mapping" — distinct from a mapping
#: whose state has gone, which is `None` and must publish nothing.
_UNMAPPED = object()


class BinarySensorExport(ExportHandler):
    """The binary sensor family — Indigo's ``onState``, or a mapped state.

    The standard reading mirrors the inbound handlers without inversion:
    ``sensors.py``'s ``OccupancyHandler`` writes ``onOffState`` from the
    occupancy bit and its ``ContactHandler`` writes it straight from
    BooleanState's ``stateValue``, which Matter documents as "TRUE = closed or
    contact".

    When ``options`` carries a mapping (ADR-0012, issue #252) the reading comes
    from the named device state instead. That is the whole of the mapping's
    runtime cost: a device typed ``<Device type="custom">`` has no ``onState``
    to read, but it has states, and the user has said which one is the sensor.

    No :meth:`commands` override — Matter sensors are read-only, and §4.2 gives
    these roles an empty command tuple by design rather than by omission.
    """

    #: The single §4.2 key this sensor publishes.
    state_key: str = ""

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        mapped = self._mapped_reading(dev, options)
        if mapped is not _UNMAPPED:
            # `None` here means the mapped state has gone (a plugin renamed or
            # dropped it). Publishing nothing is the same answer this handler
            # already gives an `onState` of None, and for the same reason: a
            # fabricated False would tell every ecosystem the leak sensor is
            # dry.
            return {} if mapped is None else {self.state_key: mapped}
        on_state = getattr(dev, "onState", None)
        # Documented as `None` when the sensor has no on/off concept; a
        # fabricated False would tell every ecosystem the door is shut.
        if on_state is None:
            return {}
        # Polarity applies here too, not only on the mapped path (issue #252,
        # reported live). A device with a perfectly ordinary `onState` can
        # still report it the other way round from the reading Matter wants —
        # a Z-Wave contact sensor is `onState` true when the door is OPEN,
        # while Matter's `contact` is true when it is SHUT.
        return {self.state_key: self._apply_invert(bool(on_state), options)}

    @staticmethod
    def _apply_invert(value: bool, options: Optional[dict]) -> bool:
        """``value``, flipped if this export declares inverted polarity."""
        if isinstance(options, dict) and options.get(export_catalog.OPTION_STATE_INVERT) is True:
            return not value
        return value

    @staticmethod
    def _mapped_reading(dev: Any, options: Optional[dict]) -> Any:
        """The mapped state as a bool, ``None`` if it has gone, or
        :data:`_UNMAPPED` if this export declares no mapping at all.

        Three outcomes rather than two because "no mapping" and "mapping whose
        state vanished" must not collapse: the first falls back to ``onState``,
        the second must publish nothing. Only a real ``bool`` counts — the
        mapping was chosen from `export_catalog.binary_state_keys`, which
        offers booleans only, so a state that has since changed type is a state
        that has effectively gone.
        """
        if not isinstance(options, dict):
            return _UNMAPPED
        key = options.get(export_catalog.OPTION_STATE_KEY)
        if not isinstance(key, str) or not key:
            return _UNMAPPED
        try:
            value = dict(getattr(dev, "states", None) or {}).get(key)
        except Exception:  # pylint: disable=broad-except
            return None
        if not isinstance(value, bool):
            return None
        return BinarySensorExport._apply_invert(value, options)


class OccupancySensorExport(BinarySensorExport):
    """``occupancySensor`` — an Indigo motion/presence sensor."""

    role = export_catalog.ROLE_OCCUPANCY_SENSOR
    state_key = STATE_OCCUPIED


class ContactSensorExport(BinarySensorExport):
    """``contactSensor`` — an Indigo door/window sensor. True = closed."""

    role = export_catalog.ROLE_CONTACT_SENSOR
    state_key = STATE_CONTACT


class WaterLeakDetectorExport(BinarySensorExport):
    """``waterLeakDetector`` — an Indigo leak sensor. True = leak detected.

    No inversion, and the polarity is worth stating because it is the OPPOSITE
    reading of the same cluster attribute :class:`ContactSensorExport` uses:
    BooleanState's `stateValue` is "true = closed" in a Contact Sensor and
    "true = detected" in a Water Leak Detector. Indigo's `onState` for a leak
    sensor is true when wet, so both sides already agree and the handler is a
    pass-through — the divergence lives in the device type, not in a convert.
    """

    role = export_catalog.ROLE_WATER_LEAK_DETECTOR
    state_key = STATE_LEAK


class WaterFreezeDetectorExport(BinarySensorExport):
    """``waterFreezeDetector`` — an Indigo freeze/frost sensor. True = detected."""

    role = export_catalog.ROLE_WATER_FREEZE_DETECTOR
    state_key = STATE_FREEZE


class RainSensorExport(BinarySensorExport):
    """``rainSensor`` — an Indigo rain sensor. True = rain detected."""

    role = export_catalog.ROLE_RAIN_SENSOR
    state_key = STATE_RAIN


class SmokeAlarmExport(BinarySensorExport):
    """``smokeAlarm`` — an Indigo smoke sensor. True = alarming.

    Still a plain boolean on the wire, even though the Matter cluster models
    alarms as a three-value enum (Normal/Warning/Critical). Indigo has no
    severity: a smoke sensor's `onState` is alarming or it is not, and the node
    is where the boolean becomes `Critical` and drives the cluster's mandatory
    `expressedState`. Inventing a Warning tier here would mean choosing a
    severity no Indigo device ever reported.
    """

    role = export_catalog.ROLE_SMOKE_ALARM
    state_key = STATE_SMOKE


class CoAlarmExport(BinarySensorExport):
    """``coAlarm`` — an Indigo carbon-monoxide sensor. True = alarming.

    A separate role from :class:`SmokeAlarmExport` rather than a second key on
    one: they share Matter device type 0x0076 but select their sensing half by
    cluster feature, and one Indigo boolean means one thing. Publishing both
    halves from it would tell every ecosystem that a smoke-only sensor is also
    watching for CO.
    """

    role = export_catalog.ROLE_CO_ALARM
    state_key = STATE_CO


class NumericSensorExport(ExportHandler):
    """The five measured-value sensors — Indigo's ``sensorValue``.

    No tolerance on the diff, unlike the colour roles: nothing round-trips here
    (Matter sensors are read-only), so there is no echo to damp and every change
    Indigo reports is a change the sensor reported. Also no commands, for the
    same reason as :class:`BinarySensorExport`.
    """

    #: The single §4.2 key this sensor publishes.
    state_key: str = ""

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        value = _number(dev, "sensorValue")
        # Documented as `None` when the sensor carries no numeric reading.
        return {} if value is None else {self.state_key: value}


class TemperatureSensorExport(NumericSensorExport):
    """``temperatureSensor`` — ``sensorValue`` read as °C (see the module doc)."""

    role = export_catalog.ROLE_TEMPERATURE_SENSOR
    state_key = STATE_TEMPERATURE_C


class HumiditySensorExport(NumericSensorExport):
    """``humiditySensor`` — ``sensorValue`` read as %RH."""

    role = export_catalog.ROLE_HUMIDITY_SENSOR
    state_key = STATE_HUMIDITY_PCT


class LightSensorExport(NumericSensorExport):
    """``lightSensor`` — ``sensorValue`` read as lux; the node applies the log scale."""

    role = export_catalog.ROLE_LIGHT_SENSOR
    state_key = STATE_LUX


class PressureSensorExport(NumericSensorExport):
    """``pressureSensor`` — ``sensorValue`` read as **hPa**, sent as §4.2 kPa.

    The only sensor role that converts, and the module docstring says why at
    length: Indigo's barometer unit is hPa — that is what this plugin's own
    *inbound* handler writes (``matter_handlers/sensors.py``, unit label
    ``" hPa"``) and what ``export_catalog``'s role heuristic matches on — while
    §4.2 names the wire key ``pressureKPa``. 1 hPa is 0.1 kPa.

    Skipping the divide sent sea-level 1013 hPa out as 1013 kPa, which the node
    faithfully scaled to 10130 (0.1 kPa) and every ecosystem rendered as a
    perfectly believable pressure ten times too high. Rounded to 0.1 kPa
    because that is the resolution the wire actually has (``kilopascalsToMatter``
    multiplies by 10 into an int16), so keeping more would be inventing digits
    the ecosystem cannot show.
    """

    role = export_catalog.ROLE_PRESSURE_SENSOR
    state_key = STATE_PRESSURE_KPA

    def states_for(self, dev: Any, options: Optional[dict] = None) -> dict:
        states = super().states_for(dev, options)
        if self.state_key in states:
            states[self.state_key] = round(states[self.state_key] / HPA_PER_KPA, 1)
        return states


class FlowSensorExport(NumericSensorExport):
    """``flowSensor`` — ``sensorValue`` read as m³/h (the node scales to 0.1 m³/h)."""

    role = export_catalog.ROLE_FLOW_SENSOR
    state_key = STATE_FLOW_M3H


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
#: role → handler. **The whole §4.2 v1 enum, as of E4.** The zoo test pins that
#: against ``bridge_protocol.ROLES``, so a role added to the protocol without a
#: handler here fails before it can reach a user.
HANDLERS: dict[str, ExportHandler] = {
    handler.role: handler for handler in (
        OnOffExport(),
        OnOffLightExport(),
        DimmableLightExport(),
        ColorTemperatureLightExport(),
        ExtendedColorLightExport(),
        WindowCoveringExport(),
        DoorLockExport(),
        OccupancySensorExport(),
        ContactSensorExport(),
        WaterLeakDetectorExport(),
        WaterFreezeDetectorExport(),
        RainSensorExport(),
        SmokeAlarmExport(),
        CoAlarmExport(),
        TemperatureSensorExport(),
        HumiditySensorExport(),
        LightSensorExport(),
        PressureSensorExport(),
        FlowSensorExport(),
        ThermostatExport(),
    )
}

#: The roles this plugin can actually bridge today.
BRIDGEABLE_ROLES = frozenset(HANDLERS)


def handler_for(role: str) -> Optional[ExportHandler]:
    """The handler for ``role``, or ``None`` if this version cannot bridge it.

    Total over v1 since E4. Kept because the *caller's* contract depends on it:
    a role it cannot bridge must be skipped rather than sent, or one unknown
    role fails the whole ``attach``.
    """
    return HANDLERS.get(role)


def is_bridgeable(role: str) -> bool:
    """True if an allow-list entry with ``role`` can be exported today."""
    return role in HANDLERS
