"""Reconcile matter-server nodes ↔ Indigo devices, and apply state updates.

The authoritative ``(nodeId, endpointId) → {device_type_id → indigoDeviceId}``
map is derived from each Indigo device's ``pluginProps`` (the single source of
truth that survives plugin reloads); an in-memory index caches it.  The index
is read/written from both the asyncio loop (reconcile, attribute events) and
Indigo threads (``deviceStartComm`` → ``note_device``), so every access is
guarded by ``self._lock`` (a re-entrant lock, since the index methods call one
another).

The nested-map structure (added in fix/#44) lets multiple additive devices live
on the same (node, endpoint) — e.g. an Air Quality node exposing AirQuality +
CO2 + PM2.5 + TVOC on endpoint 1 produces four separate Indigo devices, each
keyed by its ``deviceTypeId``.

This module is also the asyncio→Indigo write seam: ``apply_states`` is the one
place ``updateStatesOnServer`` is called *from the loop thread*, so it can be
swapped to ``run_in_executor`` later if the loop stalls. (There is a second
``updateStatesOnServer`` call in ``_create_one`` for initial states, but that
runs on the commissioning/Indigo thread, not the loop.)
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, Optional

import indigo

import protocol
from matter_model import (
    BBRIDGE_ATTR_REACHABLE,
    CLUSTER_BRIDGED_BASIC,
    NodeInfo,
    node_id_to_str,
    parse_node,
)
from matter_handlers.base import IndigoDeviceSpec, base_props
from matter_handlers.basic_information import (
    ATTR_NODE_LABEL,
    ATTR_SW_VERSION_STRING,
    CLUSTER_BASIC_INFORMATION,
)
from matter_handlers.boolean_state_config import (
    ATTR_SUPPORTED_SENSITIVITY_LEVELS,
    CLUSTER_BOOLEAN_STATE_CONFIG,
)
from matter_handlers.settings import ATTR_ATTRIBUTE_LIST, SETTINGS, implements, settings_for_type
from matter_handlers.electrical import (
    ATTR_ACTIVE_ENDPOINTS,
    ATTR_AVAILABLE_ENDPOINTS,
    ATTR_POWER_TOPOLOGY_FEATURE_MAP,
    CLUSTER_ELECTRICAL_ENERGY,
    CLUSTER_ELECTRICAL_POWER,
    CLUSTER_POWER_TOPOLOGY,
    FEATURE_DYNAMIC_POWER_FLOW,
    FEATURE_SET_TOPOLOGY,
    meter_props,
)
from matter_handlers.generic_switch import (
    CLUSTER_SWITCH,
    DEVICE_TYPE_BUTTON,
    PROP_SWITCH_FEATURES,
    switch_features,
)
from matter_handlers.power_source import (
    ATTR_ENDPOINT_LIST,
    CLUSTER_POWER_SOURCE,
    resolve_power_coverage,
)
import settings_report
from protocol import MatterCommand

# Device type ids for which SupportsPowerMeter/SupportsEnergyMeter are meaningful.
# This mirrors exactly the handler types that inject these props at creation
# (on_off → matterRelay, level_control → matterDimmer, color_control → matterColorDimmer).
_METER_CAPABLE_TYPES = frozenset({"matterRelay", "matterDimmer", "matterColorDimmer"})

#: Clusters that don't, by themselves, make an endpoint a *device*: utility /
#: metadata clusters any endpoint may carry, plus the merge-only measurement
#: clusters (their handlers augment a sibling device, never create one). An
#: endpoint ≥1 whose unhandled clusters are all in this set gets no
#: matterUnknown placeholder (issue #58).
_NON_DEVICE_CLUSTERS = frozenset({
    0x0003,  # Identify
    0x0004,  # Groups
    0x0005,  # Scenes (pre-Matter-1.3 id)
    0x001D,  # Descriptor
    0x001E,  # Binding
    0x0028,  # BasicInformation
    0x002F,  # PowerSource (node-scoped battery, merged into siblings)
    0x0039,  # BridgedDeviceBasicInformation
    0x0040,  # FixedLabel
    0x0041,  # UserLabel
    0x0062,  # ScenesManagement (Matter 1.3 id)
    0x0090,  # ElectricalPowerMeasurement (merge-only)
    0x0091,  # ElectricalEnergyMeasurement (merge-only)
    0x009C,  # PowerTopology (merge-only; drives meter-link resolution, issue #79)
})

#: Electrical clusters annotated in the _unknown_spec log/props when they
#: co-occur with a genuinely-unsupported cluster on the same endpoint (issue
#: #79 point 3) — distinct from _NON_DEVICE_CLUSTERS, which also includes
#: plain utility clusters (Identify, Groups, …) that aren't worth calling out.
_ELECTRICAL_MERGE_CLUSTERS = frozenset({
    CLUSTER_ELECTRICAL_POWER, CLUSTER_ELECTRICAL_ENERGY, CLUSTER_POWER_TOPOLOGY,
})

#: Matter device type 0x000E (Aggregator) — the endpoint a bridge publishes to
#: parent its bridged children. It carries no device clusters of its own (only
#: Descriptor), so it correctly produces no Indigo device; the children hang off
#: it as separate endpoints. A bridge that publishes an Aggregator with NO
#: children therefore commissions successfully and creates nothing at all, with
#: no explanation — the silent dead end reported in issue #105.
DEVICE_TYPE_AGGREGATOR = 0x000E


#: Friendly role suffix per Indigo device type, used to name a node's endpoint
#: devices.  A HomePod exposing temperature + humidity becomes
#: "<name> - Temperature" / "<name> - Humidity" rather than the opaque
#: "<name> (endpoint 1)".  Endpoint-number naming is kept only as the fallback
#: for a type absent from this map, and to disambiguate genuinely identical
#: siblings (e.g. four outlets on one strip — see create_devices).
#:
#: Since issue #204 stage 2 (ADR-0008 option B) EVERY endpoint device takes its
#: role suffix, including the sole device of a single-endpoint node: the node
#: device is what now carries the bare base name, so "Office Plug" (the node)
#: and "Office Plug - Switch" (the relay) read as a family instead of
#: colliding over one name and being told apart by a numeric suffix.
_ROLE_LABELS = {
    "matterRelay": "Switch",
    "matterDimmer": "Dimmer",
    "matterColorDimmer": "Light",
    "matterTemperatureSensor": "Temperature",
    "matterHumiditySensor": "Humidity",
    "matterMotionSensor": "Motion",
    "matterContactSensor": "Contact",
    "matterIlluminanceSensor": "Illuminance",
    "matterPressureSensor": "Pressure",
    "matterFlowSensor": "Flow",
    "matterThermostat": "Thermostat",
    "matterFan": "Fan",
    "matterWindowCovering": "Window Covering",
    "matterLock": "Lock",
    "matterValve": "Valve",
    "matterButton": "Button",
    "matterSmokeCOAlarm": "Smoke/CO",
    "matterAirQualitySensor": "Air Quality",
    "matterCO2Sensor": "CO₂",
    "matterPM25Sensor": "PM2.5",
    "matterTVOCSensor": "TVOC",
    "matterEnergyMeter": "Energy",
    "matterUnknown": "Unsupported",
}


#: A trailing "(endpoint N)" — the pre-role naming fallback, still generated for
#: a type absent from _ROLE_LABELS on a multi-endpoint node.
_ENDPOINT_SUFFIX_RE = re.compile(r" \(endpoint \d+\)$")


def _generated_base(name: Any, role: str) -> Optional[str]:
    """The base a plugin-generated endpoint-device name was built FROM, or None.

    The inverse of ``create_devices``' naming rules, and deliberately only of
    the *suffixed* ones — a bare name carries no evidence of who wrote it, so
    it is not parsed here (see ``_derive_family_base``, which admits a bare
    name only when it matches a base it already knows).

    Recognised: ``"<base> - <Role>"``, ``"<base> - <Role> <n>"`` (the identical-
    siblings form, and equally the numeric suffix ``_unique_name`` adds on a
    collision — indistinguishable, and both mean the same thing here) and
    ``"<base> (endpoint <n>)"``.

    ``role`` is the label of the device's OWN Indigo type, not any role: a
    matterRelay named "Lamp - Motion" is a hand-rename that happens to look
    generated, and admitting it would let one such name steer the whole
    family's true-up (issue #204 stage 2).
    """
    if not isinstance(name, str) or not name:
        return None
    stripped = _ENDPOINT_SUFFIX_RE.sub("", name)
    if stripped != name:
        return stripped or None
    if role:
        match = re.fullmatch(rf"(?P<base>.+) - {re.escape(role)}(?: \d+)?", name)
        if match:
            return match.group("base")
    return None


def _is_generated_name(name: Any, base: str, role: str) -> bool:
    """Whether ``name`` is a name THIS plugin would have generated from ``base``.

    The gate on every rename in the stage-2 true-up: a device whose name is not
    one of these forms was named by a human and is never touched (issue #204 —
    "NEVER rename a hand-renamed device"). Covers the bare base (the pre-stage-2
    single-endpoint form, and the node device's own form), any of the suffixed
    forms ``_generated_base`` parses, and ``_unique_name``'s numeric suffix on
    top of either.
    """
    if not isinstance(name, str) or not base:
        return False
    if name == base or re.fullmatch(rf"{re.escape(base)} \d+", name):
        return True
    if role and (name == f"{base} - {role}"
                 or re.fullmatch(rf"{re.escape(base)} - {re.escape(role)} \d+", name)):
        return True
    return bool(re.fullmatch(rf"{re.escape(base)} \(endpoint \d+\)", name))


def _kvlist(states: dict) -> list:
    return [{"key": key, "value": value} for key, value in states.items()]


def _reachable_kv(value: bool) -> dict:
    """kv-dict for the ``reachable`` boolean WITH its display ``uiValue``.

    ``reachable`` is a bare Boolean YesNo on several device types — several of
    them (``matterNode`` in particular) use it as their ``UiDisplayStateId``,
    so the device list's own status column shows it. Without a uiValue that
    renders as a bare "yes"/"no", which next to an on/off actuator reads like
    another on/off answer rather than a connectivity flag. The STATE stays a
    plain boolean either way (ADR-0007 — triggers/conditionals may bind it);
    this only changes what a human reads in the column.
    """
    return {"key": "reachable", "value": bool(value),
            "uiValue": "reachable" if value else "unreachable"}


def _capability_fingerprint(lists: dict) -> dict:
    """An AttributeList map reduced to what a capability answer actually depends on.

    An AttributeList is a SET of attribute ids; the wire carries it as a sequence
    and nothing promises a stable order. ``settings.implements`` reads it as a
    set, so two orderings of the same ids are the same answer — comparing the raw
    values would report a capability change where none happened (issue #190: a
    needless state-list rebuild plus a repeat of the removal log).

    A value that is not a sequence, or one carrying something unhashable, is
    passed through untouched: it is unusable either way, and ``implements``
    already answers *unknown* for it.
    """
    def fingerprint(value):
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value
        try:
            return frozenset(value)
        except TypeError:
            return value        # a list of dicts, say — order-sensitive, but never a capability

    return {key: fingerprint(value) for key, value in lists.items()}


def _salvage_node_id(raw: Any) -> Optional[int]:
    """Best-effort node id from a raw node dict that ``parse_node`` rejected.

    Accepts the plain ints matter-server sends and the ``"0x…"`` strings this
    codebase elsewhere treats as a legitimate node-id form (see
    ``node_id_to_str``). Returns None when nothing usable is present.
    """
    if not isinstance(raw, dict):
        return None
    value = raw.get("node_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


class NodeDeviceTombstones:
    """Which nodes' synthetic ``matterNode`` device the user has deliberately
    deleted (issue #204, ADR-0008), so a later reconcile does not silently
    bring it back.

    Every OTHER device this plugin creates is cluster-derived: delete one and
    the next reconcile recreates it, because the Matter node still reports the
    cluster that justified it — that is the correct, expected self-heal
    (issue #45). The node device is different. It exists because
    ``device_sync`` decided to represent the node (ADR-0008), not because a
    cluster demanded it, so deleting it is a real, standing choice that a
    reconcile must not quietly overturn. Without this, every WS reconnect
    (``reconcile_all`` → ``create_devices``) would recreate a device the user
    just removed.

    Same shape as :class:`settings_report.SurveyLog` — one JSON string in
    ``pluginPrefs`` — but a plain set of node ids rather than a fingerprint
    map, because there is nothing to compare against, only membership: a node
    is either tombstoned or it isn't. A blob that will not parse starts empty
    rather than raising, the same discipline as ``SurveyLog.load``.

    Cleared by ``_forget_node_capabilities`` on decommission, for the same
    reason ``SurveyLog.forget`` is: node ids are reused across a
    decommission/recommission cycle, and a genuinely different device must
    not inherit an old one's tombstone. The asymmetry with everything else
    that method drops is deliberate — every other cache it clears is
    cluster-derived and self-heals on the next informative pass; a tombstoned
    node device does not exist to self-heal, because deleting it WAS the
    point, so it is cleared only when the node itself is gone, never merely
    re-reported.
    """

    def __init__(self, save: Optional[Callable[[str], None]] = None, logger: Optional[Any] = None) -> None:
        self._tombstoned: set[str] = set()
        self._save = save
        self._logger = logger
        self._lock = threading.RLock()

    def load(self, blob: Any) -> None:
        """Replace the tombstone set from a stored JSON string (or anything unusable)."""
        parsed: set[str] = set()
        if isinstance(blob, str) and blob.strip():
            try:
                raw = json.loads(blob)
                if isinstance(raw, list):
                    parsed = {str(v) for v in raw}
            except (TypeError, ValueError):
                parsed = set()
        with self._lock:
            self._tombstoned = parsed

    def to_json(self) -> str:
        with self._lock:
            return json.dumps(sorted(self._tombstoned), separators=(",", ":"))

    def is_tombstoned(self, node_id: Any) -> bool:
        with self._lock:
            return str(int(node_id)) in self._tombstoned

    def add(self, node_id: Any) -> None:
        """Record a deliberate deletion (plugin.deviceDeleted → note_node_device_deleted)."""
        with self._lock:
            self._tombstoned.add(str(int(node_id)))
        self._persist()

    def forget(self, node_id: Any) -> None:
        """Drop a decommissioned node's tombstone, so a re-commission is not born deleted."""
        with self._lock:
            existed = str(int(node_id)) in self._tombstoned
            self._tombstoned.discard(str(int(node_id)))
        if existed:
            self._persist()

    def clear_all(self) -> int:
        """The menu route back (issue #204's "Recreate Matter node devices…"):
        every tombstoned node is forgotten in one go, so the next reconcile
        recreates every node device the user has deleted.

        Returns the count of ids cleared (issue #204 review, fix B) — the
        menu handler uses it to skip a pointless 30s reconcile when there was
        nothing to clear, and to make its success log honest about how many
        nodes are actually in play."""
        with self._lock:
            count = len(self._tombstoned)
            self._tombstoned = set()
        if count:
            self._persist()
        return count

    def _persist(self) -> None:
        if self._save is None:
            return
        try:
            self._save(self.to_json())
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never sink a reconcile
            # Unlike SurveyLog's failed save (cosmetic — the digest is just
            # stale until the next successful write), a failed tombstone save
            # is not cosmetic (issue #204 review, fix C): if the plugin
            # restarts before a LATER successful save, this deliberately-
            # deleted node device is resurrected on the next reconcile,
            # because load() at startup reads whatever pluginPrefs still has.
            # Loud, not silent — but still swallowed here, same reasoning as
            # SurveyLog._persist: an exception escaping into
            # create_devices/deviceDeleted would be worse than one repeated
            # write attempt later.
            if self._logger is not None:
                self._logger.warning(
                    "Matter: could not save node-device tombstones (%s) — if the plugin "
                    "restarts before a later successful save, a node device you deleted "
                    "may be recreated.", exc)


class DeviceSync:
    def __init__(self, registry: Any, logger: Any) -> None:
        self.registry = registry
        self.logger = logger
        # Nested index: (node_id, endpoint_id) → {device_type_id: dev_id}
        # device_type_id="" is the sentinel for handlers that have no deviceTypeId
        # (e.g. ElectricalPower/Energy) — these are stored under "" and the
        # fallback in lookup() handles them correctly (single-device endpoint).
        self._index: dict[tuple[int, int], dict[str, int]] = {}
        self._active: set[int] = set()
        self._lock = threading.RLock()
        # Meter-link maps for split-endpoint energy measurement (issue #79 —
        # e.g. IKEA GRILLPLATS: relay on ep1, ElectricalPower/Energy on ep2).
        # Deterministic from the node model, rebuilt by _resolve_meter_links on
        # every create_devices/reconcile pass — never persisted.
        # Forward: (node_id, source_ep) -> target_ep, for live attribute routing.
        self._forward_links: dict[tuple[int, int], int] = {}
        # Reverse: (node_id, target_ep) -> {source_eps}, for state priming and
        # capability-prop self-heal (both start from the target device).
        self._reverse_links: dict[tuple[int, int], set[int]] = {}
        # Every cluster this plugin recognizes at all — either a registered
        # ClusterHandler (whether or not it's primary for a given endpoint;
        # e.g. PowerSource/Electrical never produce a spec of their own but
        # ARE recognized) or a plain utility cluster. Used by the issue #81
        # "leftover cluster" diagnostic to distinguish a genuinely-unsupported
        # cluster from one this plugin already accounts for some other way.
        self._recognized_clusters = frozenset(
            h.cluster_id for h in registry.handlers
        ) | _NON_DEVICE_CLUSTERS
        # What each of a node's power sources POWERS, per node:
        # node_id -> {source endpoint -> endpoints it powers} (issue #205).
        # Decided by matter_handlers.power_source.resolve_power_coverage from
        # the device's own EndpointList (0x001F, core §11.7.7.32), falling back
        # per source to the pre-#205 heuristic for rev-1 firmware that predates
        # the attribute. Rebuilt from the node snapshot on every informative
        # create pass (never merged, never persisted), exactly like the meter
        # links above; read only through _battery_endpoints/_battery_targets,
        # which are the single decision seam the four old open-coded copies of
        # the heuristic — creation, priming, capability self-heal and live
        # fan-out — collapsed into. Issue #82's cross-contamination bug was a
        # bug in one of those copies.
        self._power_coverage: dict[int, dict[int, frozenset[int]]] = {}
        # The raw LIMITS attribute behind every setting that declares
        # FromAttribute bounds (most take theirs from the spec and cache nothing
        # here — matter_handlers.settings.SETTINGS), keyed
        # (node_id, endpoint_id, cluster, attribute) — issues #85 and #186.
        # NodeInfo snapshots are transient (this class holds no other
        # node-attribute cache), so the ConfigUI layer in plugin.py needs these
        # captured somewhere durable; REBUILT per node on every create/reconcile
        # pass THAT CARRIES ATTRIBUTES (an empty snapshot is no information, not
        # a retraction — see create_devices) and dropped on decommission (#192).
        #
        # Cached rather than read live ON PURPOSE, and the distinction matters:
        # get_node is a CACHE and is the wrong source for a reading that drifts,
        # but a limits attribute is firmware-fixed structure (HoldTimeLimits,
        # SupportedSensitivityLevels), which is exactly what a snapshot is good
        # for. It also keeps the Edit Device dialog from blocking the Indigo UI
        # thread on a multi-second read to a sleepy device just to draw itself.
        self._setting_limits: dict[tuple[int, int, int, int], Any] = {}
        # Each settings-bearing cluster's AttributeList (0xFFFB) per
        # (node_id, endpoint_id, cluster) — the device's own statement of which
        # attributes it implements, and THE capability check for whether a
        # setting may be offered (issue #186) — and, since #190, for which of the
        # type's declared STATES this unit is given (plugin.getDeviceStateList).
        # Needed because most settings take
        # their bounds from the spec rather than from a device attribute, so
        # "are the limits readable?" cannot stand in for "does it have this?".
        self._attribute_lists: dict[tuple[int, int, int], Any] = {}
        # Every node id matter-server has told us about this session, whether or
        # not it produced any Indigo device. `_index` cannot serve this purpose:
        # it is keyed by endpoints that HAVE an Indigo device (however the entry
        # got there — creation, note_device, or rebuild_index), so a node that
        # maps to nothing never appears in it, and the decommission picker (which
        # reads list_nodes()) could never offer the one kind of node most in need
        # of decommissioning — an empty bridge (issue #111; #105 explains why such
        # a node exists at all).
        #
        # Unlike `_index` this is NOT rebuilt from Indigo at startup — nothing in
        # Indigo records a node that produced no devices — so an empty bridge is
        # unlistable until the first successful reconcile. Refreshed on every
        # reconcile pass (WS connect/reconnect only, see plugin._resync), so a
        # node removed out of band stops being offered.
        self._known_nodes: set[int] = set()
        # Which nodes have already had their settable-attribute report logged
        # (issue #191). Injected by plugin.startup because the log is persisted
        # in pluginPrefs and this class owns no prefs; left None in tests and in
        # any path that has not wired it, which disables the automatic report
        # rather than making it fire on every reconcile pass — an unbounded
        # repeat is the one outcome that would make the INFO wallpaper.
        self.survey_log: Optional[Any] = None
        # Deliberate matterNode deletions (issue #204, ADR-0008). Injected by
        # plugin.startup, same discipline as survey_log: None in tests and any
        # path that hasn't wired it, which means "nothing is tombstoned" —
        # NOT "recreate nothing" — see _ensure_node_device.
        self.node_tombstones: Optional[Any] = None
        # Nodes whose AttributeLists changed but whose Indigo devices have not
        # yet had their state lists rebuilt (issue #190). Needed because
        # _cache_setting_limits both records the new answer and reports the
        # change: once it has run, no later pass can rediscover that something
        # moved, so a refresh that is skipped (an exception later in the create
        # pass) or that fails (Indigo rejecting the rebuild) would latch the
        # stale state list until a plugin restart. Cleared only by a refresh that
        # actually completed.
        self._pending_state_refresh: set[int] = set()
        # (node_id, dev_id) pairs already told their battery reading is
        # frozen because new EndpointList evidence excluded their endpoint
        # (issue #205 upgrade gap — see _warn_of_newly_excluded_battery_devices).
        # A plain per-run set, not persisted: the point is one INFO line per
        # device per plugin run, not a permanent suppression — a restart (or a
        # later coverage change that excludes a DIFFERENT device) should be
        # free to log again.
        self._battery_exclusion_warned: set[tuple[int, int]] = set()
        # Nodes already told their AttributeList stably fails ADR-0003's
        # node-device gate (issue #204 review, fix D) — both NodeLabel and
        # SoftwareVersionString read back False, not None. Same per-run-only
        # discipline as _battery_exclusion_warned: a restart should be free
        # to log again, and this is not meant to be a permanent suppression.
        self._non_conformant_node_warned: set[int] = set()
        # dev_ids delete_node is about to delete itself (issue #204 review,
        # fix A). Indigo's deviceDeleted callback "gets called for every kind
        # of device deletion — the user deleting it, a plugin deleting it,
        # Indigo deleting it as part of a group" (Plugin Guide, deviceDeleted),
        # so delete_node's own indigo.device.delete calls fire the SAME
        # callback a user's manual delete does. Without this set,
        # note_node_device_deleted cannot tell "user deleted the node device"
        # from "delete_node deleted it as part of a decommission" and
        # tombstones both — but the decommission already forgets the node
        # (or the id gets reused on recommission), so the tombstone then
        # blocks the node device from ever being recreated. Only the node
        # device's id is ever marked — note_node_device_deleted is the sole
        # reader and only matterNode deletions reach it — and it is unmarked
        # again either on the deviceDeleted callback (discard on hit) or
        # immediately when its own delete raises, so a marked-but-never-
        # deleted id can never swallow a LATER genuine user deletion.
        self._self_deleted_ids: set[int] = set()
        # (dev_id, kind) pairs already warned about by _ensure_grouped (issue
        # #204 stage 2). Grouping is attempted on EVERY create/reconcile pass —
        # that repetition IS the migration, and it is what makes the operation
        # idempotent without a version stamp — so an unguarded warning would
        # nag once per WS reconnect for the lifetime of the plugin run. Same
        # per-run-only discipline as _battery_exclusion_warned: a restart is
        # free to say it again, because a restart is when a user is looking.
        self._group_warned: set[tuple[int, str]] = set()
        # (node_id, endpoint_id) pairs already told their standalone issue-#79
        # matterEnergyMeter fallback is now redundant (issue #215): a link
        # that resolves AFTER the fallback was created leaves both devices
        # showing the same reading (PR #213 kept the standalone one updating
        # rather than orphaning it), so the user is told once — unlike the
        # matterUnknown obsolescence log above, which is content to repeat
        # every pass, this one is a standing fact for the endpoint's whole
        # life once linked, so repeating it would just be noise on every
        # reconnect. Evicted in _forget_node_capabilities alongside
        # _forward_links, so a decommissioned-then-recommissioned node can
        # earn the log again rather than being silenced by a stale entry.
        self._redundant_meter_logged: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------
    # Active-device tracking (deviceStartComm/deviceStopComm)
    # ------------------------------------------------------------------
    def set_active(self, dev_id: int, active: bool) -> None:
        if active:
            self._active.add(dev_id)
        else:
            self._active.discard(dev_id)

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------
    @staticmethod
    def _prop_present(value: Any) -> bool:
        # ids are stored as strings ("0" is valid); only None / "" mean absent.
        return value is not None and value != ""

    def rebuild_index(self) -> None:
        with self._lock:
            self._index.clear()
            for dev in indigo.devices.iter("self"):
                props = dev.pluginProps
                node_id = props.get("nodeId")
                endpoint_id = props.get("endpointId")
                if self._prop_present(node_id) and self._prop_present(endpoint_id):
                    key = (int(node_id), int(endpoint_id))
                    type_id = getattr(dev, "deviceTypeId", "") or ""
                    self._index.setdefault(key, {})[type_id] = dev.id
        # Outside the lock: it iterates Indigo and logs, and nothing under the
        # lock depends on it — same discipline as the capability refresh.
        self._warn_unconfigured_strays()

    def _warn_unconfigured_strays(self) -> None:
        """Name any device of ours that Indigo has made INVISIBLE to us (#62).

        Indigo mutates ``deviceTypeId`` the moment a type is picked from the
        Edit Device Type menu — before, and regardless of, any save — and a
        device left mid-edit that way is ``configured=False``. Unconfigured
        devices are excluded from ``indigo.devices.iter("self")`` and never get
        ``deviceStartComm``, so BOTH of issue #58's type-change guards are
        structurally blind to this: ``validateDeviceConfigUi`` only fires on a
        completed save, and the ``deviceStartComm`` backstop never runs at all.

        The device is then missing from the index and from ``_unique_name``'s
        view, so the next reconcile tries to RE-CREATE it and Indigo refuses
        server-side with ``NameNotUniqueError`` — because the invisible device
        still holds the name. PR #61 turned that collision into an actionable
        warning, but only for endpoints reconcile actually tries to recreate: a
        stray whose node is offline, decommissioned, or simply not in this
        pass produces no collision and so says nothing at all, while the device
        sits there dead.

        This scans the UNFILTERED collection — the only place such a device is
        still visible — and reports it directly, at reconcile, without waiting
        for a collision that may never come.

        Identified by ``createdTypeId``, not by plugin id: that prop is stamped
        by this plugin at creation (and healed onto older devices), so its
        presence means we made the device and it finished its first save. That
        matters because a device the user is part-way through adding by hand is
        ALSO ``configured=False``, and warning about a dialog that is merely
        still open would be noise on every reconcile it stays open for.
        """
        try:
            strays = [
                dev for dev in indigo.devices
                if not getattr(dev, "configured", True)
                and (dev.pluginProps or {}).get("createdTypeId")
            ]
        except Exception as exc:  # noqa: BLE001 - a diagnostic must never sink reconcile
            self.logger.debug("could not scan for unconfigured devices: %s", exc)
            return
        for dev in strays:
            created = (dev.pluginProps or {}).get("createdTypeId")
            current = getattr(dev, "deviceTypeId", "") or ""
            changed = (f" Its type was changed from {created} to {current}."
                       if current and current != created else "")
            self._group_warn(
                dev.id, "unconfigured-stray",
                'Matter device "%s" (id %s) is in a half-edited state and this plugin '
                'can no longer see it: Indigo reports it as not configured, which '
                'excludes it from the plugin\'s device list, so it receives no updates '
                'and no commands.%s This is what using Indigo\'s "Edit Device Type" '
                'menu on a Matter device does — the type changes the moment it is '
                'picked, before any save. To fix it: DELETE the device in Indigo and '
                'reload the Matter plugin; the next reconcile recreates it correctly '
                'with the right type.',
                dev.name, dev.id, changed,
            )

    def lookup(self, node_id: Any, endpoint_id: Any,
               device_type_id: Optional[str] = None) -> Optional[int]:
        """Look up an Indigo device id for a (node, endpoint[, type]) triple.

        - With ``device_type_id``: returns the device of exactly that type on
          the endpoint, or None if it hasn't been created yet.
        - Without ``device_type_id``: if the endpoint has exactly one device,
          returns it; if several, returns the first inserted (deterministic) —
          preserves pre-#44 semantics for callers that mean "the endpoint's
          device" (e.g. Reachable handler, reachability marking, reconcile
          refresh, node_event dispatch for handlers without a device_type_id).
        """
        with self._lock:
            type_map = self._index.get((int(node_id), int(endpoint_id)))
            if type_map is None:
                return None
            if device_type_id is not None:
                return type_map.get(device_type_id)
            # no type requested — return the sole device or the first inserted
            values = list(type_map.values())
            return values[0] if values else None

    def _all_dev_ids_for_endpoint(self, node_id: Any, endpoint_id: Any) -> list[int]:
        """Return all dev_ids registered under (node, endpoint), preserving insertion order."""
        with self._lock:
            type_map = self._index.get((int(node_id), int(endpoint_id)))
            if type_map is None:
                return []
            return list(type_map.values())

    def _lookup_for_cluster(self, node_id: Any, endpoint_id: Any, cluster: int) -> Optional[int]:
        """Resolve the correct Indigo device for a non-node-scoped cluster update.

        0. Electrical measurement clusters (0x0090/0x0091) are rewritten to
           their linked target endpoint FIRST (issue #79 — split-endpoint
           energy, e.g. IKEA GRILLPLATS): the electrical handlers have no
           ``device_type_id``, so without this rewrite step 3's bare
           ``lookup()`` would resolve (or fail to resolve) against the source
           endpoint itself, which has no device when the link succeeded.
        1. Resolve the handler's ``device_type_id`` (if any).
        2. If that type is present in the endpoint's type-map → return it.
        3. Else fall back to ``lookup(node, ep)`` (single/first device) —
           covers merge-into cases: FanControl→thermostat, Electrical→relay.

        Step 0 is numbered to match execution order in the code below, not
        just to list a concern — it must run before the bare ``lookup()`` in
        step 3 has a chance to miss the (now-empty) source endpoint.
        """
        nid, eid = int(node_id), int(endpoint_id)
        if cluster in (CLUSTER_ELECTRICAL_POWER, CLUSTER_ELECTRICAL_ENERGY):
            with self._lock:
                target = self._forward_links.get((nid, eid))
            if target is not None:
                eid = target
                # The linked target endpoint may host more than one device
                # (e.g. a colour light); prefer the meter-capable one over the
                # sole/first-device fallback in step 3 so a multi-device
                # endpoint can never mis-route an energy reading.
                with self._lock:
                    type_map = self._index.get((nid, eid))
                if type_map:
                    for cap_type in _METER_CAPABLE_TYPES:
                        if cap_type in type_map:
                            return type_map[cap_type]
        handler = self.registry.handler_for_cluster(cluster)
        if handler is None:
            return None
        type_id = getattr(handler, "device_type_id", "") or ""
        if type_id:
            with self._lock:
                type_map = self._index.get((nid, eid))
                if type_map and type_id in type_map:
                    return type_map[type_id]
        # Fallback: use the single/first device on the endpoint
        return self.lookup(nid, eid)

    def note_device(self, dev: Any) -> None:
        """Index a single device from its pluginProps (deviceStartComm)."""
        props = dev.pluginProps
        node_id = props.get("nodeId")
        endpoint_id = props.get("endpointId")
        if self._prop_present(node_id) and self._prop_present(endpoint_id):
            key = (int(node_id), int(endpoint_id))
            type_id = getattr(dev, "deviceTypeId", "") or ""
            with self._lock:
                self._index.setdefault(key, {})[type_id] = dev.id

    def delete_node(self, node_id: Any, forget: bool = True) -> list:
        """Delete all Indigo devices for a node; return the ids actually deleted.

        Ids whose Indigo delete fails are NOT included in the returned list, so
        the decommission response never claims a device was removed when it
        wasn't.

        ``forget`` controls whether the node also stops being offered by
        :meth:`list_nodes`. Pass the *fabric removal's* outcome: only this
        method's caller knows whether the node actually left the fabric, and
        forgetting one that didn't is unrecoverable for a node with no Indigo
        devices — ``_index`` cannot restore it, so it would vanish from the
        picker while still commissioned, with no way to retry until the next
        reconcile (issue #111 review).

        Deletion DISSOLVES THE GROUP FIRST: every candidate is ungrouped, and
        only then is anything deleted. ``indigo.device.delete`` refuses to
        delete the root of a non-empty Indigo device group, and per ADR-0009
        that root is whichever member is OLDEST — the node device only on a
        node commissioned since the node device existed, and an ENDPOINT
        device on every fielded install. Ordering alone therefore cannot
        avoid the refusal (children-before-node-device, the stage-1 rule,
        throws on the very first child of an age-rooted group); emptying the
        group does, for every shape, with no need to know which end Indigo
        picked. Each ungroup is guarded on its own: a device that cannot be
        ungrouped still gets its delete attempted, and a delete that Indigo
        refuses is reported and excluded from the returned list exactly as
        before.
        """
        target = int(node_id)
        with self._lock:
            # Stop offering a node that has genuinely left the fabric. Pruning
            # _index alone used to suffice — a node with no index entries simply
            # vanished from list_nodes() — but now that list_nodes() also draws
            # on _known_nodes, a decommissioned node would linger there until the
            # next reconcile_all and could be picked a second time.
            if forget:
                self._known_nodes.discard(target)
            # Collect all dev_ids across all endpoints for this node. The
            # matterNode device (if present) is still held back to the end —
            # not because deletion order matters any more (the group is
            # dissolved below), but because the decommission response reads
            # better with the node device last and `_self_deleted_ids` is
            # easiest to reason about when it is.
            node_dev_id: Optional[int] = None
            candidates: list[int] = []
            for (nid, eid), type_map in self._index.items():
                if nid != target:
                    continue
                for type_id, dev_id in type_map.items():
                    if eid == 0 and type_id == "matterNode":
                        node_dev_id = dev_id
                    else:
                        candidates.append(dev_id)
            if node_dev_id is not None:
                candidates.append(node_dev_id)
            # Mark the node device (and only it) as self-deleted BEFORE
            # calling indigo.device.delete (issue #204 review, fix A — see
            # _self_deleted_ids above). Whether Indigo's deviceDeleted
            # callback for this delete lands synchronously inside the call
            # below or arrives afterwards, note_node_device_deleted must find
            # the id already marked. Membership is cleared there (discard on
            # hit) — or right here if the delete itself raises, because a
            # marked id whose device SURVIVED (e.g. the non-empty-group-root
            # refusal once grouping lands) would otherwise swallow a later
            # genuine user deletion of that same device: no tombstone, no
            # INFO, and the next reconcile resurrects it.
            if node_dev_id is not None:
                self._self_deleted_ids.add(node_dev_id)
            # Dissolve the node's device group before deleting any of it
            # (ADR-0009). Per-device and guarded: an ungroup Indigo refuses —
            # the docs' open-dialog hazard, or a device that vanished between
            # the index read and here — must not stop the other members being
            # ungrouped, and the delete below is still attempted for it (a
            # non-root member deletes perfectly well from inside a group).
            for dev_id in candidates:
                try:
                    indigo.device.ungroupDevice(indigo.devices[dev_id])
                except Exception as exc:  # noqa: BLE001
                    self.logger.debug("could not ungroup Indigo device %s: %s", dev_id, exc)
            deleted = []
            for dev_id in candidates:
                try:
                    indigo.device.delete(indigo.devices[dev_id])
                    deleted.append(dev_id)
                except Exception as exc:  # noqa: BLE001
                    if dev_id == node_dev_id:
                        self._self_deleted_ids.discard(dev_id)
                    self.logger.warning("could not delete Indigo device %s: %s", dev_id, exc)
            deleted_set = set(deleted)
            # Drop successfully-deleted devices from the nested index
            new_index: dict[tuple[int, int], dict[str, int]] = {}
            for key, type_map in self._index.items():
                if key[0] == target:
                    remaining = {tid: did for tid, did in type_map.items() if did not in deleted_set}
                    if remaining:
                        new_index[key] = remaining
                else:
                    new_index[key] = type_map
            self._index = new_index
            return deleted

    def note_node_device_deleted(self, node_id: Any, dev_id: Any) -> None:
        """A user deleted a node's ``matterNode`` device by hand (issue #204,
        ADR-0008) — called from ``plugin.deviceDeleted``.

        Drops the index entry immediately, so a stale dev_id can never be
        handed back by :meth:`lookup`/:meth:`_ensure_node_device` before the
        next reconcile runs, and tombstones the node so ``create_devices``
        does not recreate it out from under the user on the next reconnect.
        The "Recreate Matter node devices…" menu item
        (``ServerMenuMixin.menuRecreateNodeDevices``) is the only way back.

        Endpoint devices deliberately do NOT get this treatment: they are
        cluster-derived, so deleting one is corrected by the next reconcile
        (issue #45's self-heal) — that is existing, expected behaviour this
        method does not change (and since stage 2 the recreated device rejoins
        the node device's group, and is named from the family base rather than
        the product name). Only the node device is synthetic enough that its
        deletion has to be honoured rather than healed.

        Since stage 2 the node device is in a group with its siblings, and the
        two deletion routes behave DIFFERENTLY — verified live 2026-08-12,
        because assuming otherwise would have put a false claim in the user
        docs. ``indigo.device.delete`` (the scripting API, what ``delete_node``
        calls) refuses the ROOT of a non-empty group, which is why that method
        dissolves the group first. **Indigo's UI does not refuse**: deleting a
        grouped device flags the dependent devices and offers to delete the
        whole family, so a user who confirms takes every device on the node
        with it — node device included, whichever one leads the group
        (ADR-0009: that is the oldest member, so the node device on a node
        commissioned since node devices existed, an older endpoint device on a
        fielded install). Either way the node device's own deletion lands in
        ``deviceDeleted`` and tombstones, which is right: the user confirmed a
        dialog naming it. The endpoint devices heal on the next reconcile with
        NEW ids, so their bindings do not — ``docs/MATTER.md`` warns about that
        and points at decommissioning as the operation that actually means
        "remove this hardware", and at a quiet folder for "just hide it".

        ``dev_id`` (issue #204 review, fix A) is what tells a genuine user
        deletion apart from ``delete_node`` deleting its own node device as
        part of a decommission: Indigo's ``deviceDeleted`` fires for BOTH —
        "it gets called for every kind of device deletion, whether the user
        deletes it manually, a script or action group deletes it, or a
        plugin deletes it" (Plugin Guide, deviceDeleted) — so without this
        check every decommission would tombstone the node it just forgot,
        and a later recommission onto the reused node id would get no node
        device and a misleading "will not be recreated" INFO forever.
        """
        nid = int(node_id)
        did = int(dev_id)
        with self._lock:
            if did in self._self_deleted_ids:
                self._self_deleted_ids.discard(did)
                return
            type_map = self._index.get((nid, 0))
            if type_map is not None:
                type_map.pop("matterNode", None)
                if not type_map:
                    self._index.pop((nid, 0), None)
        if self.node_tombstones is not None:
            self.node_tombstones.add(nid)
        self.logger.info(
            "node %s: its Matter node device was deleted and will not be recreated "
            "automatically. Use the 'Recreate Matter node devices…' menu item if you "
            "want it back.",
            node_id_to_str(nid),
        )

    def knows_node(self, node_id: Any) -> bool:
        """Whether this node is one we currently track — same set list_nodes offers.

        Lets a caller tell "node we have never heard of" from "node we know, whose
        removal failed", which the decommission path must not conflate: the latter
        deserves the retry guidance, not a 404 (issue #111 review).
        """
        target = int(node_id)
        with self._lock:
            return target in self._known_nodes or any(nid == target for (nid, _eid) in self._index)

    def node_count(self) -> int:
        # Counts the same nodes list_nodes() offers — including one that produced
        # no Indigo devices. Deriving this from _index alone would under-report a
        # commissioned empty bridge in the /status payload's nodeCount.
        with self._lock:
            return len(self._known_nodes | {nid for (nid, _eid) in self._index})

    def list_nodes(self) -> list:
        """Per-node summary for UI pickers: ``[(node_id, [device names])]``.

        Every node matter-server has reported is listed, INCLUDING one that
        produced no Indigo devices — its entry carries an empty name list, which
        ``ServerMenuMixin.getMatterNodes`` renders as "(no Indigo devices)".
        See ``_known_nodes`` for why ``_index`` alone could not do this.

        Sorted by node id; device names resolved outside the lock so a slow
        Indigo lookup can't stall state/command dispatch.
        """
        with self._lock:
            by_node: dict[int, set] = {nid: set() for nid in self._known_nodes}
            for (nid, _eid), type_map in self._index.items():
                by_node.setdefault(nid, set()).update(type_map.values())
        out = []
        for nid in sorted(by_node):
            names = []
            for dev_id in sorted(by_node[nid]):
                try:
                    names.append(indigo.devices[dev_id].name)
                except KeyError:
                    # deleted out-of-band mid-iteration; reconcile will heal the index
                    names.append(f"device {dev_id}")
                except Exception as exc:  # noqa: BLE001 - UI label only; never break the picker
                    self.logger.debug("list_nodes: name lookup for device %s failed: %s", dev_id, exc)
                    names.append(f"device {dev_id}")
            out.append((nid, names))
        return out

    # ------------------------------------------------------------------
    # Creation (called from the commissioning worker / reconcile)
    # ------------------------------------------------------------------
    def create_from_raw(self, raw_node: dict, suggested_name: str,
                        suggested_room: Optional[str] = None) -> dict:
        node = parse_node(raw_node, suggested_name or "")
        return self.create_devices(node, suggested_room)

    def create_devices(self, node: NodeInfo, suggested_room: Optional[str] = None) -> dict:
        created: list[int] = []
        new_ids: list[int] = []
        primary: Optional[int] = None
        failed = 0
        # The commission path threads a user-chosen name (via node.suggested_name)
        # and/or room; reconcile and out-of-band node_added do not. When either is
        # present this create is *authoritative* and may rename/re-folder a device
        # that node_added raced ahead and created with the bare product name.
        authoritative = bool((node.suggested_name or "").strip()) or bool((suggested_room or "").strip())
        with self._lock:
            # Covers the commission and node_added paths; reconcile_all maintains
            # _known_nodes independently (see list_nodes).
            self._known_nodes.add(int(node.node_id))
            # Plan over every mappable endpoint (existing or not) so the
            # "(endpoint N)" suffix is decided by the node's true device count,
            # not by how many happen to be missing on this pass. A plug's root
            # endpoint 0 produces no handler, so this is not len(node.endpoints).
            plan: list[tuple] = []  # (endpoint, spec)
            # Ask each PowerSource endpoint what it powers (issue #205). Indigo
            # applies Supports* via device props at creation, not Devices.xml
            # statics (the colour-support lesson; issue #56), so this has to be
            # settled before the plan loop stamps them.
            power_source_eps = {
                int(endpoint.endpoint_id) for endpoint in node.endpoints
                if endpoint.has(CLUSTER_POWER_SOURCE)
            }
            coverage = resolve_power_coverage(
                power_source_eps,
                {ep: node.attributes.get((ep, CLUSTER_POWER_SOURCE, ATTR_ENDPOINT_LIST))
                 for ep in power_source_eps},
                (int(endpoint.endpoint_id) for endpoint in node.endpoints),
                note=self.logger.debug,
            )
            # An EMPTY snapshot is "no information", never "implements nothing"
            # (issue #192). matter-server populates its attribute cache lazily:
            # getNodeDetails returns `attributeCache.get(nodeId) ?? {}` and kicks
            # off the fill in the background, so the first node_updated after a
            # server restart legitimately carries `attributes: {}` and a second
            # one follows once the cache is warm. Every capability answer below
            # is derived from those attributes, so believing an empty one would
            # retract the node's battery endpoints and AttributeLists on a
            # routine restart — and, since #190, RESURRECT exactly the spurious-0
            # states #190 removes (unknown keeps a state), for the next warm
            # snapshot to withdraw them again. Verified in matter-server 1.2.2:
            # @matter-server/ws-controller ControllerCommandHandler.js
            # getNodeDetails + AttributeDataCache.js #runPopulate, which collects
            # into a local object and does ONE #cache.set once complete — so the
            # snapshot is all-or-nothing, never partially filled.
            informative = bool(node.attributes)
            if informative:
                with self._lock:
                    if coverage.by_source:
                        self._power_coverage[int(node.node_id)] = dict(coverage.by_source)
                    else:
                        self._power_coverage.pop(int(node.node_id), None)
            # Deferred deliberately: the refresh calls into Indigo per device and
            # must not run under _lock, and it has to see the devices this pass
            # is about to create.
            if informative and self._cache_setting_limits(node):
                # Recorded rather than acted on, because the cache is now the NEW
                # value: if the refresh below is skipped or throws, no later pass
                # can ever detect the change again and the stale state list is
                # latched until a plugin restart. The node stays pending until a
                # refresh actually completes.
                self._pending_state_refresh.add(int(node.node_id))
            # Pass 1: cache every endpoint's handler-produced specs BEFORE any
            # fallback/placeholder decision. Meter-link resolution (issue #79)
            # needs to know which endpoints will host a _METER_CAPABLE_TYPES
            # device regardless of endpoint iteration order — precedent for
            # pre-loop node-wide computation is the SupportsBatteryLevel scan
            # just above.
            specs_by_ep: dict[int, list] = {
                int(endpoint.endpoint_id): self.registry.handlers_for_endpoint(node, endpoint)
                for endpoint in node.endpoints
            }
            # Same `informative` gate as _power_coverage above, for the same
            # reason (ADR-0003: an empty snapshot is no information, never a
            # retraction) — _resolve_meter_links CLEARS-then-rebuilds this
            # node's links from node.endpoints, so a non-informative pass (zero
            # endpoints) would wipe every link a prior pass established. A live
            # ep-0 reading arriving after that wipe would resolve to the node
            # device instead of the actuator it belongs to, writing a duplicate
            # reading the priming guard below then never corrects (issue #204
            # review, fix A).
            if informative:
                self._resolve_meter_links(node, specs_by_ep)

            for endpoint in node.endpoints:
                eid = int(endpoint.endpoint_id)
                ep_key = (int(node.node_id), eid)
                raw_specs = specs_by_ep[eid]
                specs = list(raw_specs)
                # Captured up front (issue #80 review point B) so the
                # obsolescence log below can fire for EVERY way an endpoint
                # stops needing its matterUnknown placeholder this pass — not
                # just the "already had real specs" case the old `elif` only
                # covered. Without this, an endpoint reclassified as a
                # meter-linked source (specs stay empty) or as an ambiguous
                # matterEnergyMeter fallback never got the "can be deleted"
                # log, orphaning the placeholder (or leaving a duplicate
                # device with no log connecting them).
                had_placeholder = bool(self._index.get(ep_key, {}).get("matterUnknown"))
                if not specs:
                    if self._is_meter_source_candidate(endpoint, specs):
                        # Standalone energy-measurement endpoint (e.g. IKEA
                        # GRILLPLATS ep2): if link resolution found the
                        # endpoint it measures, no device is created here at
                        # all — readings route to the linked target via
                        # _lookup_for_cluster. Otherwise fall back to a
                        # standalone matterEnergyMeter device rather than the
                        # matterUnknown placeholder (issue #79) — EXCEPT for
                        # endpoint 0 (issue #204/ADR-0008), which never gets a
                        # standalone matterEnergyMeter: it already has a home,
                        # the matterNode device's own curEnergyLevel/
                        # accumEnergyTotal states, and _lookup_for_cluster's
                        # bare lookup(node, 0) fallback resolves an unlinked
                        # (node, 0, 0x0090/0x0091) reading straight to it.
                        if eid != 0 and ep_key not in self._forward_links:
                            specs = [self._energy_meter_spec(node, endpoint)]
                        elif eid != 0 and ep_key not in self._redundant_meter_logged:
                            # The link resolved THIS pass (or a prior one), but
                            # a standalone matterEnergyMeter from before it
                            # resolved may still be sitting there (PR #213 —
                            # it keeps updating rather than going stale). Same
                            # voice as the matterUnknown obsolescence log
                            # below; fired once, not every pass (issue #215).
                            meter_dev_id = self._index.get(ep_key, {}).get("matterEnergyMeter")
                            if meter_dev_id is not None:
                                self._redundant_meter_logged.add(ep_key)
                                try:
                                    meter_name = indigo.devices[meter_dev_id].name
                                except Exception as exc:  # noqa: BLE001 - name lookup only; log fires either way
                                    self.logger.debug(
                                        "redundant meter: device %s name lookup failed: %s",
                                        meter_dev_id, exc,
                                    )
                                    meter_name = f"device {meter_dev_id}"
                                self.logger.info(
                                    "node %s endpoint %s energy readings now route to the "
                                    "linked actuator device — the standalone '%s' energy-meter "
                                    "device is redundant and can be deleted",
                                    node_id_to_str(node.node_id), eid, meter_name,
                                )
                    else:
                        # No handler claimed this endpoint. If it carries clusters
                        # that DO make it a device (just ones we don't support yet),
                        # surface it as a matterUnknown placeholder instead of
                        # silently creating nothing — a commission that "succeeds"
                        # with no visible device is a half-happened op (issue #58).
                        # Endpoint 0 is the Matter root (utility clusters only) and
                        # never gets a placeholder.
                        fallback = self._unknown_spec(node, endpoint)
                        if fallback is not None:
                            specs = [fallback]
                elif raw_specs:
                    # Endpoint produced real handler spec(s): surface any
                    # cluster this plugin recognizes nothing about at all, so
                    # it isn't silently dropped once a device already exists
                    # for the endpoint (issue #81 — e.g. a pump exposing
                    # OnOff + an unhandled cluster: the relay is created but
                    # the extra cluster vanishes with no diagnostic). No
                    # second device — an INFO log only, same voice as the
                    # matterUnknown placeholder log; may repeat every
                    # reconcile pass (same precedent as the obsolescence log
                    # below).
                    leftover = sorted(set(endpoint.cluster_ids) - self._recognized_clusters)
                    if leftover:
                        self.logger.info(
                            "node %s endpoint %s also exposes clusters this plugin "
                            "does not support yet (%s) — please report the device "
                            "at github.com/simons-plugins/indigo-matter/issues",
                            node_id_to_str(node.node_id), endpoint.endpoint_id,
                            ", ".join(f"0x{c:04X}" for c in leftover),
                        )
                if had_placeholder and not any(s.device_type_id == "matterUnknown" for s in specs):
                    # The endpoint no longer needs (or has already replaced)
                    # its matterUnknown placeholder this pass. Never
                    # auto-delete a user's device; tell them it's obsolete.
                    self.logger.info(
                        "node %s endpoint %s is now supported — the 'Matter Device "
                        "(unsupported clusters)' placeholder device can be deleted",
                        node_id_to_str(node.node_id), endpoint.endpoint_id,
                    )
                for spec in specs:
                    # A device gets the battery prop when some power source on
                    # this node says it powers this endpoint (issue #205). Read
                    # from the pass-local coverage, not the cache: this is the
                    # pass that WRITES the cache, and a non-informative one
                    # deliberately writes nothing.
                    if eid in coverage.covered:
                        spec.props.setdefault("SupportsBatteryLevel", True)
                    if spec.device_type_id in _METER_CAPABLE_TYPES:
                        # Central injection (issue #79): a linked source
                        # endpoint's electrical clusters unlock the meter
                        # states on the TARGET device. The existing
                        # same-endpoint checks in on_off/level_control/
                        # color_control stay untouched — they cover the
                        # Tapo-style co-located case.
                        for src_eid in self._reverse_links.get(ep_key, ()):
                            src_endpoint = self._endpoint_by_id(node, src_eid)
                            if src_endpoint is None:
                                continue
                            for key, value in meter_props(src_endpoint).items():
                                spec.props.setdefault(key, value)
                    plan.append((endpoint, spec))

            if not plan:
                self._warn_if_empty_bridge(node)

            multi = len(plan) > 1
            # Count role occurrences across the plan so genuinely identical
            # siblings (e.g. four outlets on one strip, all "Switch") fall back
            # to an endpoint-numbered suffix, while a node's distinct functions
            # (temperature vs humidity) read as "<name> - <role>".
            role_counts: dict[str, int] = {}
            for _ep, _spec in plan:
                role = _ROLE_LABELS.get(_spec.device_type_id, "")
                if role:
                    role_counts[role] = role_counts.get(role, 0) + 1
            # Stamp the hardware product as each device's Model so a node's
            # sibling devices share one value in the Indigo device list's Model
            # column (the grouping Indigo offers short of a Device Factory).
            model = node.product_name or node.vendor_name or ""
            folder_id = self._resolve_folder_id(suggested_room) if authoritative else 0
            # The base name this node's EXISTING Indigo devices already agree
            # on (issue #204 stage 2) — what the user called this thing, which
            # a NON-authoritative pass has no other way of knowing: reconcile
            # parses the node with an empty suggested_name, so every spec below
            # falls back to the product name. Only consulted on a
            # non-authoritative pass; a commission/room pass carries the user's
            # choice in spec.name already and _apply_identity stamps it.
            derived_base = None if authoritative else self._derive_family_base(node)
            # Pre-compute the FULL set of planned device_type_ids per endpoint so
            # _prime_states can identify merge-into handlers correctly regardless
            # of creation order.  (The index is built incrementally, so checking
            # it inside the loop would miss not-yet-created siblings.)
            ep_planned_types: dict[tuple, set] = {}
            for ep_, spec_ in plan:
                key_ = (int(node.node_id), int(ep_.endpoint_id))
                ep_planned_types.setdefault(key_, set()).add(spec_.device_type_id)
            # Resolve-or-create the node's own synthetic device (issue #204,
            # ADR-0008) BEFORE any endpoint device — deliberately, and
            # deliberately NEVER appended to `plan` itself (`multi = len(plan)
            # > 1` and `role_counts` above must see only endpoint specs, or
            # every single-endpoint node would flip to "multi" and rename its
            # one real device with a role suffix it doesn't need — the most
            # dangerous regression in this change; that guard is unchanged).
            #
            # FIRST is the whole point (ADR-0009): Indigo orders a device
            # group's members by device AGE and roots the group at the OLDEST
            # member — the argument order of `groupWithDevice` decides nothing
            # (controlled experiment, jarvis 2026-08-12). Creation order is
            # therefore the ONLY lever on which device roots the group, and it
            # only exists on a FRESH commission, where the node device can be
            # made the oldest by making it first. A fielded install's node
            # device is younger than every endpoint device it will ever be
            # grouped with and can never root them; that is accepted, and
            # membership — not the root — is what this plugin delivers.
            node_dev_id = self._ensure_node_device(
                node, coverage, folder_id, model, plan, derived_base=derived_base)
            for endpoint, spec in plan:
                # Bridged endpoints carry their own identity in cluster 0x0039.
                # When a bridge label (node_label or product_name) is present:
                #   - Non-authoritative pass (node_added / reconcile): use the
                #     bridge label as the device name; skip "(endpoint N)" suffix.
                #   - Authoritative pass (user-chosen name in suggested_name):
                #     use spec.name (already encodes suggested_name); still skip
                #     the suffix — bridged children have unique identities.
                # Every OTHER endpoint device is named "<base> - <Role>" since
                # issue #204 stage 2, single-endpoint nodes included.
                bridge_label = endpoint.node_label or endpoint.product_name
                # A bridged child names itself; everything else is named from
                # the family base — the derived one when this pass found one
                # (issue #204 stage 2: it is what recreating a deleted device
                # under the family's own name depends on), the spec's own
                # otherwise.
                base = spec.name if bridge_label or not derived_base else derived_base
                role = _ROLE_LABELS.get(spec.device_type_id, "")
                if bridge_label:
                    # TODO(#43): on an authoritative pass, spec.name (the user's
                    # chosen bridge name) overwrites each child's own NodeLabel,
                    # producing "My Hue Bridge", "My Hue Bridge 2", … and wiping
                    # per-bulb names the user set in the bridge app.  Issue #43
                    # tracks the fix (prefix or bridge-only authoritative stamp).
                    name = spec.name if authoritative else bridge_label
                elif role and role_counts.get(role, 0) > 1:
                    # Genuinely identical siblings (four outlets on one strip):
                    # the role alone cannot tell them apart, so the endpoint
                    # number does.
                    name = f"{base} - {role} {endpoint.endpoint_id}"
                elif role:
                    # Name by the device's function ("- Temperature"), not its
                    # Matter endpoint number, which is meaningless to the user.
                    # Since #204 stage 2 this is NOT conditional on `multi`: a
                    # single-endpoint node's one device is suffixed too, because
                    # the node device is what holds the bare base now.
                    name = f"{base} - {role}"
                elif multi:
                    # Unmapped type on a multi-device node — nothing to name it
                    # by but its endpoint.
                    name = f"{base} (endpoint {endpoint.endpoint_id})"
                else:
                    name = base
                ep_key = (int(node.node_id), int(endpoint.endpoint_id))
                type_id = spec.device_type_id
                # Existing-device check is now per (node, ep, device_type_id)
                # so additive specs on the same endpoint each get their own device.
                existing_type_map = self._index.get(ep_key, {})
                existing = existing_type_map.get(type_id)
                if existing is not None:
                    # Already created (e.g. node_added won the race). On the
                    # authoritative commission pass, stamp the chosen name/room.
                    if authoritative:
                        self._apply_identity(existing, name, folder_id)
                    elif derived_base and not bridge_label:
                        # Non-authoritative pass on a node whose family base is
                        # known: true the name up to the canonical form, but
                        # ONLY if the current one is a name this plugin wrote
                        # (issue #204 stage 2). A BRIDGED child is excluded
                        # outright: its name comes from its own NodeLabel, set
                        # in the bridge's app, and this pass has no business
                        # deciding that a family base beats it (issue #43 is
                        # the open question about bridge naming; stage 2
                        # deliberately does not touch it).
                        self._true_up_endpoint_name(
                            existing, name, derived_base, self._fallback_base(node), role)
                    created.append(existing)
                    primary = primary if primary is not None else existing
                    continue
                # Stamp the node id into the device's address (the Indigo UI's
                # protocol-identifier column) so the nodeId is recoverable for
                # decommission without spelunking pluginProps (issue #18).
                spec.props.setdefault("address", node_id_to_str(node.node_id))
                dev_id = self._create_one(spec, name, folder_id, model)
                if dev_id is None:
                    failed += 1
                    continue
                if type_id == "matterUnknown":
                    self.logger.info(
                        "node %s endpoint %s exposes only clusters this plugin does not "
                        "support yet (%s) — created placeholder device %s; please report "
                        "the device at github.com/simons-plugins/indigo-matter/issues so "
                        "support can be added",
                        node_id_to_str(node.node_id), endpoint.endpoint_id,
                        spec.props.get("supportedClusters", "?"), dev_id,
                    )
                self._index.setdefault(ep_key, {})[type_id] = dev_id
                created.append(dev_id)
                new_ids.append(dev_id)
                primary = primary if primary is not None else dev_id
                self._prime_states(
                    node, dev_id, endpoint.endpoint_id, type_id,
                    ep_sibling_types=ep_planned_types.get(ep_key, set()),
                )
            # The name true-up, RE-RUN now that the endpoint loop above has
            # renamed everything it is going to. Going first costs the node
            # device the one thing going last used to buy it: on a fielded
            # install mid-migration an endpoint device may still have been
            # holding the bare base when `_create_one`'s `_unique_name`
            # looked, so a node device created THIS pass can be born as
            # "<base> 2". By here the sibling has taken its role suffix and
            # the bare base is free, so the same idempotent true-up that heals
            # a 2026.13.0 install heals that too — in ONE pass, as before.
            # A settled node (name already canonical, folder set, prop
            # stamped) reads the device and writes nothing.
            if node_dev_id is not None:
                self._true_up_node_device(node_dev_id, node, derived_base, folder_id)
            # Every one of this node's OTHER devices belongs in ONE Indigo
            # device group with the node device (ADR-0008 option B, as
            # narrowed by ADR-0009: membership is the deliverable, the root is
            # Indigo's own age-ordered choice). Collected here, under the lock
            # that owns `_index`, and PERFORMED BELOW, outside it. Read from
            # the index rather than from `plan`/`new_ids` so the sweep also
            # reaches devices this pass did not touch — an already-created
            # sibling, a matterUnknown placeholder, a matterEnergyMeter
            # fallback — which is what makes this the migration for a fielded
            # install as well as the wiring for a fresh commission. Empty when
            # the node has no node device (the ADR-0003 gate not passed, or
            # tombstoned): there is nothing for the family to be a group WITH.
            to_group: list[tuple[int, int]] = []
            # This node's own device ids, node device included — passed to
            # _ensure_grouped so it can tell "every id in this fielded group is
            # ours" (heal it) from "this group has a device that isn't" (leave
            # it, ADR-0008/#204 review). Built alongside to_group from the same
            # _index sweep, so both see the identical membership.
            family: frozenset[int] = frozenset()
            if node_dev_id is not None:
                family = frozenset(
                    {int(node_dev_id)} | {
                        dev_id
                        for (nid, _eid), type_map in self._index.items()
                        if nid == int(node.node_id)
                        for dev_id in type_map.values()
                    }
                )
                to_group = [(dev_id, node_dev_id) for dev_id in family if dev_id != node_dev_id]
        # Outside the lock, deliberately, and for the same reason
        # report_settable_attributes below is: every call reaches into Indigo
        # (getGroupList/groupWithDevice), and grouping is Indigo-side metadata
        # that no reader of `_index` is waiting on. Idempotent by construction
        # — a device already in a group with its node device is a no-op — so
        # this IS the migration; there is no version stamp to keep in step.
        # Order-independent too (ADR-0009): a partially-grouped node heals by
        # ACCUMULATION, each remaining member joining the group the others are
        # already in, whatever order the sweep happens to reach them in.
        for dev_id, group_with in to_group:
            try:
                # Existence check first (issue #204 review, fix D): the index
                # can still carry an endpoint device the user deleted by hand
                # (plugin.deviceDeleted only prunes matterNode ids), and
                # grouping a dead id is a guaranteed failure that is not the
                # user's problem. int() lives in here too, so a non-coercible
                # id costs one device rather than the rest of the node's sweep
                # (fix F).
                indigo.devices[int(dev_id)]
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("not grouping device %s: %s", dev_id, exc)
                continue
            try:
                self._ensure_grouped(dev_id, group_with, family)
            except Exception as exc:  # noqa: BLE001 - per-device independence (fix F)
                self.logger.debug(
                    "grouping device %s with %s failed: %s", dev_id, group_with, exc)
        if new_ids:
            # The only event-log evidence of an out-of-band join (node_added)
            # is this line — keep it INFO, not debug (issue #19). Idempotent
            # re-passes (reconcile) create nothing and stay quiet.
            self.logger.info(
                "Matter node %s (%s %s): created Indigo device(s) %s",
                node_id_to_str(node.node_id),
                node.vendor_name or "unknown vendor",
                node.product_name or "unknown product",
                ", ".join(str(dev_id) for dev_id in new_ids),
            )
        if failed:
            # partial creation must be visible to the commission result, not hidden
            self.logger.warning(
                "node %s: %d of %d expected device(s) failed to create",
                node.node_id, failed, len(plan),
            )
        # `new_ids` too, not just a changed cache: a device created on a pass
        # where the AttributeLists happened to match (a node_updated that
        # recreates a device the user deleted, say) would otherwise be born with
        # the unfiltered type-level state list and never be revisited, because
        # the cache already agrees with itself.
        if int(node.node_id) in self._pending_state_refresh or new_ids:
            self._refresh_state_lists(int(node.node_id))
        # Outside the lock, and after the devices exist: the survey names them,
        # and reporting is a log-and-persist that has no business holding a lock
        # every state update also needs. `informative` for the same reason the
        # capability caches need it — an empty snapshot would survey a device as
        # implementing nothing, record that as the answer, and then never report
        # the real one because the fingerprint had already been banked.
        if informative:
            # A device whose endpoint WAS covered under the old heuristic (or an
            # earlier, less specific EndpointList) but is now EXCLUDED by this
            # pass's evidence keeps its last batteryLevel forever with no
            # further diagnostic (props/states are add-only by policy, and
            # reconcile only runs on a WS reconnect) — name it once so that
            # staleness is discoverable. Same reasoning as report_settable_
            # attributes just below: a diagnostic must not run under the lock
            # every state update needs, and it has nothing to do with the
            # endpoint loop above that the lock actually protects (issue #205
            # review — the old in-lock, pre-loop placement contradicted this).
            self._warn_of_newly_excluded_battery_devices(node, coverage)
            self.report_settable_attributes(node)
        result = {
            "indigoDeviceIds": created,
            # Deliberately separate from indigoDeviceIds/primaryDeviceId
            # (issue #204): the node device is not one of the node's endpoint
            # devices and must never become — or displace — the primary a
            # user who commissioned, say, a plug expects to get back (the
            # relay), so it is surfaced as its own key instead of folded in.
            "nodeDeviceId": node_dev_id,
            "primaryDeviceId": primary,
            "endpointCount": len(node.endpoints),
            "vendorId": node.vendor_id,
            "productId": node.product_id,
            "vendorName": node.vendor_name,
            "productName": node.product_name,
        }
        if failed:
            result["partial"] = True
            result["failedEndpoints"] = failed
        return result

    def _warn_if_empty_bridge(self, node: NodeInfo) -> None:
        """Explain a bridge that commissioned fine but exposes nothing (issue #105).

        An Aggregator endpoint carries no device clusters of its own, so it correctly
        yields no Indigo device — the bridged children are meant to hang off it as
        separate endpoints. When a bridge publishes the aggregator and NO children,
        commissioning reports success and creates nothing, with no hint that the fault
        lies on the bridge's side. A device-bearing endpoint this plugin doesn't support
        at least gets a matterUnknown placeholder; this case got silence.

        Only called when the node produced no devices at all, so a bridge whose children
        are present — even if they only rate placeholders — never triggers it. Like the
        other diagnostics here it may repeat on a reconcile pass; that is the same
        precedent as the placeholder-obsolescence log, and the condition is one the user
        has to fix on the bridge anyway.
        """
        aggregator_eps = [
            endpoint.endpoint_id for endpoint in node.endpoints
            if DEVICE_TYPE_AGGREGATOR in (endpoint.device_types or ())
        ]
        if not aggregator_eps:
            return
        self.logger.warning(
            "Matter node %s (%s %s) is a bridge that is not currently exposing any "
            "devices over Matter: endpoint %s is an Aggregator with no bridged "
            "children, so there is nothing for this plugin to create. Check the "
            "bridge's own configuration — on Homebridge/Matterbridge this usually means "
            "no accessory has been mapped to a Matter device type yet.",
            node_id_to_str(node.node_id),
            node.vendor_name or "unknown vendor",
            node.product_name or "unknown product",
            ", ".join(str(eid) for eid in aggregator_eps),
        )

    @staticmethod
    def _unknown_spec(node: NodeInfo, endpoint: Any) -> Optional[IndigoDeviceSpec]:
        """Placeholder spec for a device-bearing endpoint no handler claims.

        Returns None for endpoint 0 (the Matter root — utility clusters only)
        and for endpoints whose clusters are all in _NON_DEVICE_CLUSTERS
        (nothing device-like to surface).
        """
        if int(endpoint.endpoint_id) == 0:
            return None
        unmapped = sorted(set(endpoint.cluster_ids) - _NON_DEVICE_CLUSTERS)
        if not unmapped:
            return None
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        supported = ", ".join(f"0x{c:04X}" for c in unmapped)
        # List merge-only electrical clusters too when they co-occur with a
        # genuinely-unsupported one, so the log/UI doesn't silently drop them
        # (issue #79 point 3) — they're real clusters on this endpoint, just
        # not the reason a placeholder was needed.
        merge_only = sorted(set(endpoint.cluster_ids) & _ELECTRICAL_MERGE_CLUSTERS)
        if merge_only:
            supported += " (endpoint also exposes merge-only {})".format(
                ", ".join(f"0x{c:04X}" for c in merge_only)
            )
        return IndigoDeviceSpec(
            device_type_id="matterUnknown",
            name=name,
            props={**base_props(node, endpoint), "supportedClusters": supported},
            initial_states={"reachable": True},
        )

    @staticmethod
    def _energy_meter_spec(node: NodeInfo, endpoint: Any) -> IndigoDeviceSpec:
        """Fallback spec for a standalone energy-measurement endpoint whose
        reading can't be attributed to any actuator endpoint (issue #79) —
        e.g. a bridge, or a power strip with more than one relay and no
        SetTopology endpoint list. Sibling of ``_unknown_spec`` — built
        directly by device_sync, not via a registered ``ClusterHandler``
        (``is_primary_for`` has no access to cross-endpoint link state;
        ``_unknown_spec`` is the established device_sync-owned-spec
        precedent). ElectricalPowerHandler/ElectricalEnergyHandler then write
        to this device via the normal per-endpoint route — no link needed,
        since the device lives on the source endpoint itself.
        """
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        return IndigoDeviceSpec(
            device_type_id="matterEnergyMeter",
            name=name,
            props=base_props(node, endpoint),
            initial_states={"curEnergyLevel": 0.0, "accumEnergyTotal": 0.0, "reachable": True},
        )

    @staticmethod
    def _node_spec(node: NodeInfo, coverage: Any,
                   base_name: Optional[str] = None) -> IndigoDeviceSpec:
        """Spec for the synthetic per-node device anchored at endpoint 0
        (issue #204, ADR-0008).

        Third device_sync-owned spec builder, beside ``_unknown_spec``/
        ``_energy_meter_spec`` — no ``ClusterHandler`` owns this device either:
        ``BasicInformationHandler.is_primary_for`` is False precisely so
        device_sync stays the one place that decides WHETHER a node device
        exists (the plan/ADR-0003 evidence a per-cluster handler cannot see).

        ``name`` is the BARE base — ``base_name`` when the caller derived one
        from the node's existing devices, else ``node.suggested_name``/
        ``product_name``/fallback — with NO role suffix and NO "(endpoint N)"
        disambiguation. Since stage 2 (ADR-0008 option B) that no longer
        collides with an application endpoint's name: every endpoint device
        now carries a role suffix, so the bare base is the node device's
        alone. ``_create_one``'s ``_unique_name`` still stands behind it for
        the fielded install mid-migration, where an endpoint device may still
        be holding the bare name until this same pass renames it.
        """
        name = base_name or node.suggested_name or node.product_name or f"Matter {node.node_id}"
        props = {
            "nodeId": str(node.node_id),
            "endpointId": "0",
            # One node, one address — same convention every sibling endpoint
            # device already uses (node_id_to_str stamped at creation).
            "address": node_id_to_str(node.node_id),
            "vendorName": node.vendor_name,
            "productName": node.product_name,
            # First use of the parsed sw_version field anywhere in this
            # plugin — matter_model.parse_node has captured it since the
            # first version, but nothing has ever surfaced it until now.
            "softwareVersion": node.sw_version,
            # Stamped (not just implied by `name`) so the follow-up naming PR
            # can true up the node's device group deterministically without
            # re-deriving the bare base from scratch.
            "nodeBaseName": name,
        }
        if 0 in coverage.covered:
            props["SupportsBatteryLevel"] = True
        return IndigoDeviceSpec(
            device_type_id="matterNode",
            name=name,
            props=props,
            # nodeLabel/batteryLevel are NOT seeded here — they arrive
            # through the normal _prime_states pass immediately after
            # creation, exactly like every other device's first-value fill.
            initial_states={"reachable": True, "softwareVersion": node.sw_version},
        )

    def _ensure_node_device(self, node: NodeInfo, coverage: Any, folder_id: int,
                            model: str, plan: list, *,
                            derived_base: Optional[str] = None) -> Optional[int]:
        """Resolve or create this node's synthetic ``matterNode`` device
        (issue #204, ADR-0008).

        Order of checks, each a distinct reason to do nothing:

        1. Already indexed — idempotent, same precedent as every endpoint
           device's existing-device check. Since stage 2 this branch is not
           quite "do nothing": it is where an already-created node device has
           its name, its ``nodeBaseName`` prop and its folder trued up
           (``_true_up_node_device``), which is what the fielded installs
           created by 2026.13.0 need.
        2. Tombstoned — the user deliberately deleted this node's device
           (``note_node_device_deleted``); recreating it out from under them
           on the next reconnect would be exactly the silent override the
           tombstone exists to prevent.
        3. Empty plan — the issue #105 empty-bridge case. A node with no
           endpoint devices at all has nothing for a node device to be the
           root OF; creating one here would just be a device with nothing
           behind it. (A node that is merely mid-interview and hasn't
           produced any specs YET is covered by check 4 anyway: it has no
           AttributeList yet either.)
        4. ADR-0003 gate — ep 0's own BasicInformation AttributeList (0xFFFB)
           must positively evidence NodeLabel (0x0005) OR
           SoftwareVersionString (0x000A). Both are spec-mandatory, so a
           healthy, fully-interviewed node always passes; a node still
           mid-interview correctly gets nothing THIS pass — unknown is not
           yes (ADR-0003's asymmetric policy), and the next informative
           reconcile tries again.

           ``implements()`` is trinary (True/False/None) and ``None`` must
           stay silent — a mid-interview node is not a verdict. But False on
           BOTH attributes (not None on either) IS a stable verdict: the
           device positively read back an AttributeList lacking two
           spec-MANDATORY attributes, i.e. a non-conformant node that will
           never earn a node device. That is worth one INFO per node per
           plugin run, not silence identical to "ask again later" (issue
           #204 review, fix D).
        """
        nid = int(node.node_id)
        with self._lock:
            existing = self._index.get((nid, 0), {}).get("matterNode")
        if existing is not None:
            self._true_up_node_device(existing, node, derived_base, folder_id)
            return existing
        if self.node_tombstones is not None and self.node_tombstones.is_tombstoned(nid):
            return None
        if not plan:
            return None
        attribute_list = node.attributes.get((0, CLUSTER_BASIC_INFORMATION, ATTR_ATTRIBUTE_LIST))
        has_node_label = implements(attribute_list, ATTR_NODE_LABEL)
        has_sw_version = implements(attribute_list, ATTR_SW_VERSION_STRING)
        if has_node_label is not True and has_sw_version is not True:
            if has_node_label is False and has_sw_version is False \
                    and nid not in self._non_conformant_node_warned:
                self._non_conformant_node_warned.add(nid)
                self.logger.info(
                    "Matter node %s: its BasicInformation AttributeList reports neither "
                    "NodeLabel nor SoftwareVersionString, so no node device will be created "
                    "(non-conformant).",
                    node_id_to_str(nid),
                )
            return None
        spec = self._node_spec(node, coverage, derived_base)
        if not folder_id:
            # Reconcile has no commission-time folder (it passes 0), so a node
            # device created for an EXISTING install would land in the root
            # folder while its endpoint siblings sit in a real one — observed
            # on the live rig during #204 validation. A node device belongs
            # beside its siblings: adopt the first indexed endpoint device's
            # folder. Fresh commissions pass a real folder_id and skip this.
            folder_id = self._sibling_folder_id(nid) or folder_id
        dev_id = self._create_one(spec, spec.name, folder_id, model)
        if dev_id is None:
            return None
        with self._lock:
            self._index.setdefault((nid, 0), {})["matterNode"] = dev_id
        self._prime_states(node, dev_id, 0, "matterNode", ep_sibling_types={"matterNode"})
        self.logger.info(
            "Matter node %s (%s %s): created node device %s",
            node_id_to_str(nid), node.vendor_name or "unknown vendor",
            node.product_name or "unknown product", dev_id,
        )
        return dev_id

    def _sibling_folder_id(self, node_id: int) -> int:
        """The folder of one of this node's existing endpoint devices, or 0.

        Used only when the caller has no commission-time folder (reconcile);
        which sibling wins is arbitrary and fine — a node's devices are
        created into one folder together, and a user who has since spread
        them across folders gets one of their own choices, not ours.
        """
        with self._lock:
            dev_ids = [
                dev_id
                for (nid, eid), type_map in self._index.items()
                if nid == node_id and eid != 0
                for dev_id in type_map.values()
            ]
        for dev_id in dev_ids:
            try:
                folder = int(indigo.devices[dev_id].folderId)
            except (KeyError, AttributeError, TypeError, ValueError):
                continue  # deleted out-of-band or a fake without folders
            if folder:
                return folder  # first sibling with a real folder wins
        return 0

    # ------------------------------------------------------------------
    # Naming/folder true-up + grouping (issue #204 stage 2, ADR-0008 option B)
    #
    # Lock discipline, stated out loud because this module's own prose insists
    # on it: every true-up below is called from ``create_devices`` WHILE it
    # holds ``_lock``, and each one reads a device (and may rename it) once per
    # pass. That is an increment on an existing pattern, and deliberate —
    # ``_create_one``, ``_apply_identity`` and ``_prime_states`` already run
    # in-lock from the same loop, and the true-ups decide from, and act on,
    # the very ``_index`` entries the lock protects. The GROUPING calls are the
    # ones that stay outside it (see ``create_devices``): they are Indigo-side
    # metadata no reader of ``_index`` is waiting on. A slow or blocking call
    # added here is what would have to move out, not this comment.
    # ------------------------------------------------------------------

    @staticmethod
    def _fallback_base(node: NodeInfo) -> str:
        """The base name a NON-authoritative pass builds every spec from.

        Exactly what each handler's ``create_indigo_devices`` computes
        (``suggested_name or product_name or "Matter <id>"``) with the empty
        ``suggested_name`` reconcile always parses with — i.e. the name a
        device this plugin created OUTSIDE a commission is wearing. Knowing it
        is what lets the true-up tell a plugin-written bare name from a
        hand-written one.
        """
        return node.product_name or f"Matter {node.node_id}"

    def _derive_family_base(self, node: NodeInfo) -> Optional[str]:
        """What this node's EXISTING Indigo devices already agree they are
        called, or None (issue #204 stage 2).

        A reconcile pass parses the node with no ``suggested_name``, so every
        spec falls back to the product name — which is how a device recreated
        after a hand-delete came back as "ALPSTUGA air quality monitor -
        Switch" beside three siblings called "Bedroom air quality monitor - …"
        (observed on the live rig). The user's own name is not in the snapshot;
        the only place it survives is the devices themselves.

        Each existing endpoint device casts at most one vote:

        * a name that PARSES as one of this plugin's suffixed forms for that
          device's own role (``_generated_base``) votes for its prefix;
        * a bare name equal to the product-name fallback votes for that;
        * anything else does not vote at all.

        The first rule is not limited to names this plugin actually wrote, and
        saying otherwise would be a lie (issue #204 review, fix G): a name the
        USER typed that happens to be shaped "<X> - <that device's own role>"
        is indistinguishable from a generated one and casts a full STRONG vote
        for "<X>". Two honest consequences. Beside siblings that say something
        else it produces a disagreement, so the node gets no family base and
        NOTHING is renamed this pass — the safe outcome, and the common one. As
        a node's SOLE endpoint device its vote IS the family base, so "<X>"
        propagates to the node device's name. What is guaranteed is narrower
        than "a hand-rename is ignored": a hand-renamed device is never itself
        renamed, because every rename is gated on the name already being a
        generated form of the base it is being trued up to.

        Votes equal to the product-name fallback are WEAK: they are what a
        device that never learned the user's name looks like, so they lose to
        any other agreed answer rather than turning that answer into a
        disagreement (this is exactly the ALPSTUGA case — three strong votes
        and one weak one). With no strong votes, a single weak one still wins:
        a fielded install commissioned without a name genuinely is called the
        product name, and its devices still need their role suffixes.

        Disagreement among strong votes returns None, and NOTHING is renamed
        for this node on this pass. That is the conservative outcome by
        design: with no single answer, any rename would be a guess.
        """
        nid = int(node.node_id)
        fallback = self._fallback_base(node)
        with self._lock:
            # Endpoint devices only: the node device (endpoint 0) is named FROM
            # this answer, so letting it vote would make the derivation
            # self-confirming and freeze a wrong nodeBaseName forever.
            entries = [
                (dev_id, type_id)
                for (n_id, eid), type_map in self._index.items()
                if n_id == nid and eid != 0
                for type_id, dev_id in type_map.items()
            ]
        strong: set[str] = set()
        weak: set[str] = set()
        for dev_id, type_id in entries:
            try:
                name = indigo.devices[dev_id].name
            except Exception as exc:  # noqa: BLE001 - deleted out of band; it just doesn't vote
                self.logger.debug("family base: device %s unreadable: %s", dev_id, exc)
                continue
            base = _generated_base(name, _ROLE_LABELS.get(type_id, ""))
            if base is None:
                if fallback and name == fallback:
                    weak.add(fallback)
                continue
            (weak if base == fallback else strong).add(base)
        if len(strong) == 1:
            return next(iter(strong))
        if not strong and len(weak) == 1:
            return next(iter(weak))
        if entries:
            self.logger.debug(
                "node %s: no single family base derives from its existing devices "
                "(candidates %s) — leaving every name alone this pass",
                node_id_to_str(nid), sorted(strong | weak),
            )
        return None

    def _true_up_endpoint_name(self, dev_id: int, target: str, derived_base: str,
                               fallback_base: str, role: str) -> None:
        """Rename an existing endpoint device to its canonical name — but only
        if this plugin is the one that named it (issue #204 stage 2).

        The whole migration turns on the second half. A device whose name is
        one this plugin would have generated, from either the derived family
        base or the product-name fallback, is ours to true up; anything else
        is the user's and is left exactly as it is, however tempting the
        resemblance. Renames are id-bound-safe for triggers and schedules
        (Indigo binds by id), which is why this is allowed to happen at all.

        A canonical name that is already TAKEN aborts the rename outright
        rather than accepting ``_apply_identity``'s deduplicated substitute —
        see the guard below; that substitute is what makes this pass
        non-idempotent (issue #204 review, fix A).
        """
        try:
            dev = indigo.devices[dev_id]
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("name true-up: device %s unavailable: %s", dev_id, exc)
            return
        current = getattr(dev, "name", "")
        if current == target:
            return
        if not (_is_generated_name(current, derived_base, role)
                or _is_generated_name(current, fallback_base, role)):
            self.logger.debug(
                "leaving device %s (%r) alone — not a name this plugin generated", dev_id, current)
            return
        if self._unique_name(target) != target:
            # The canonical name belongs to something else — a foreign device,
            # a second node of the same product, a sibling mid-migration.
            # _apply_identity would silently rename to "<target> 2" instead,
            # and _is_generated_name's "{base} \d+" arm re-admits THAT next
            # pass, so the device oscillates "… 2" ↔ "… 3" forever, one real
            # rename per reconcile (issue #204 review, fix A). Leaving it alone
            # is the same posture the hand-rename gate above already takes.
            self.logger.debug(
                "target name %r is taken — leaving device %s alone", target, dev_id)
            return
        # folder 0 = leave the folder alone; dedup because a true-up repeats
        # every pass and its failures must not nag (fix E).
        self._apply_identity(dev_id, target, 0, dedup=True)

    def _true_up_node_device(self, dev_id: int, node: NodeInfo,
                             derived_base: Optional[str], folder_id: int) -> None:
        """Heal an already-created ``matterNode`` device's name, its
        ``nodeBaseName`` prop and its folder (issue #204 stage 2).

        Both defects are real and fielded, not hypothetical:

        * **Name/prop.** A node device created by a RECONCILE pass took the
          product-name fallback, because ``suggested_name`` is not in the
          snapshot — so a live FP300 ended up called "Presence Multi-Sensor
          FP300" beside children called "Kitchen matter motion 2 - …", with
          ``nodeBaseName`` stamped to match. That prop is therefore not
          trustworthy on a fielded install and must be RECOMPUTED from the
          family, never merely read.
        * **Folder.** Folder adoption at creation landed with the node device
          itself; the seven devices created before it (and any install
          upgrading through 2026.13.0) are still sitting in the root folder
          while their siblings are in a real one.

        The name is only changed when the current one is a name this plugin
        generated — ``nodeBaseName`` (whatever it says) or the product-name
        fallback, plus ``_unique_name``'s numeric suffix on either. A node
        device the user has renamed is left alone like any other device, and
        so is its ``nodeBaseName``: restamping the prop under a name the user
        owns costs the device a comm restart and buys nothing, because the
        rename it would unlock is the very thing being declined (issue #204
        review, fix H2).

        The prop is stamped only once the rename has actually LANDED (issue
        #204 review, fix B). Stamping it unconditionally is worse than doing
        nothing: ``_apply_identity`` catches and warns its own failures, so a
        refused ``replaceOnServer`` used to leave the device wearing the old
        name with ``nodeBaseName`` already advanced to the new base — after
        which ``_is_generated_name`` never matches again and the device is
        stuck under the wrong name permanently, silently, with no later pass
        able to retry.
        """
        try:
            dev = indigo.devices[dev_id]
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("node-device true-up: device %s unavailable: %s", dev_id, exc)
            return
        # Folder first, and independently of any name evidence: an orphaned
        # node device in the root folder is worth healing even for a node whose
        # base cannot be derived.
        try:
            if not getattr(dev, "folderId", 0):
                target_folder = folder_id or self._sibling_folder_id(int(node.node_id))
                if target_folder:
                    # dedup: this runs every pass until it succeeds (fix E).
                    self._move_to_folder(dev_id, target_folder, dedup=True)
        except Exception as exc:  # noqa: BLE001 - a folder must never sink a pass
            self.logger.debug("node-device folder adoption for %s failed: %s", dev_id, exc)
        if not derived_base:
            return
        props = dict(getattr(dev, "pluginProps", {}) or {})
        stamped = props.get("nodeBaseName") or self._fallback_base(node)
        current = getattr(dev, "name", "")
        if current != derived_base:
            if not _is_generated_name(current, stamped, ""):
                self.logger.debug(
                    "leaving node device %s (%r) alone — not a name this plugin generated",
                    dev_id, current)
                return
            if self._unique_name(derived_base) != derived_base:
                # Taken: same ping-pong as the endpoint side (fix A).
                self.logger.debug(
                    "target name %r is taken — leaving device %s alone", derived_base, dev_id)
                return
            if not self._apply_identity(dev_id, derived_base, 0, dedup=True):
                # The rename was refused; the prop must NOT run ahead of it
                # (fix B). A later pass, once Indigo accepts writes again,
                # finds the same evidence and retries.
                return
        if props.get("nodeBaseName") != derived_base:
            # Only when it actually moved: stamping props costs the device a
            # comm restart (the same reason SurveyLog lives in pluginPrefs
            # rather than in props), so this must be a once-per-install write,
            # not a once-per-reconcile one.
            props["nodeBaseName"] = derived_base
            try:
                dev.replacePluginPropsOnServer(props)
            except Exception as exc:  # noqa: BLE001 - deduped for the same reason
                # the rename and folder failures above are (fix E): a props
                # write Indigo keeps refusing is retried on every pass.
                self._group_warn(
                    dev_id, "restamp",
                    "could not restamp nodeBaseName on node device %s: %s", dev_id, exc)

    def _group_warn(self, dev_id: int, kind: str, fmt: str, *args: Any) -> None:
        """One warning per device per KIND of failure, per plugin run.

        Named for its first caller (grouping) but the dedupe is general — the
        button-FeatureMap warning in ``_reassert_capability_props`` uses it too,
        for the same reason: a house full of affected devices must not write one
        line per device per reconcile.
        """
        key = (int(dev_id), kind)
        if key in self._group_warned:
            return
        self._group_warned.add(key)
        self.logger.warning(fmt, *args)

    def _group_and_verify(self, dev_id: int, node_dev_id: int, first: int, second: int) -> None:
        """Call ``groupWithDevice(first, second)`` and confirm ``dev_id`` and
        ``node_dev_id`` land in one group afterwards. Shared by both callers
        in ``_ensure_grouped`` below — the ordinary join and the family
        rejoin differ only in which id leads the call; the failure handling
        and the membership read-back are identical either way."""
        try:
            indigo.device.groupWithDevice(first, second)
        except Exception as exc:  # noqa: BLE001
            self._group_warn(
                dev_id, "group",
                "could not group device %s with Matter node device %s: %s — if a device "
                "or device-factory dialog was open, close it and reload the plugin",
                dev_id, node_dev_id, exc)
            return
        try:
            after = [int(member) for member in indigo.device.getGroupList(dev_id)]
        except Exception as exc:  # noqa: BLE001 - the group was made; only the check failed
            self.logger.debug("could not verify the group of device %s: %s", dev_id, exc)
            return
        if int(node_dev_id) not in after:
            # MEMBERSHIP is what is verified, not position: the call reported
            # success but the two devices are not in one group, which is the
            # only outcome left that the user can act on.
            self._group_warn(
                dev_id, "verify",
                "device %s was grouped with Matter node device %s but Indigo did not put "
                "them in one group — the node's devices will not show together in the "
                "device list", dev_id, node_dev_id)

    def _ensure_grouped(self, dev_id: int, node_dev_id: int,
                         family: "frozenset[int]" = frozenset()) -> None:
        """Put ``dev_id`` in one Indigo device group with ``node_dev_id``
        (ADR-0008 option B, as narrowed by ADR-0009).

        **Membership is the deliverable; the root is Indigo's to choose.** A
        controlled experiment on the live rig (jarvis, 2026-08-12) settled
        what the docs never say: Indigo orders a group's members by device
        AGE and the OLDEST member is the root — stable, and identical read
        from either end. ``groupWithDevice(a, b)`` and ``groupWithDevice(b,
        a)`` produce the same group; **the argument order does nothing**. So
        there is no arg order to get right, no root to steer, and nothing the
        plugin could do about a fielded install whose node device was created
        after every device it is joining. What this method guarantees is that
        the node's devices end up in ONE group together, which is the part
        that is actually visible and useful in the Indigo UI.

        Idempotent, which is what lets it double as the migration for every
        fielded install: a device already grouped with its node device costs
        one ``getGroupList`` read and returns — whatever roots that group.
        Order-independent for the same reason: a PARTIAL group (the node
        device plus some of its siblings) is completed by each remaining
        sibling simply joining it, the third line of the experiment.

        Never re-parents a group the USER built. A device already in a group
        of two or more that does NOT contain its node device is a deliberate
        arrangement of theirs — warn once and leave it, rather than quietly
        rearranging the device list of someone who grouped their Matter relay
        with a Z-Wave sensor. The one exception is ``family``: a group whose
        members are ENTIRELY this node's own device ids (incl. the node
        device) is not a foreign arrangement at all, just this node's own
        membership caught mid-repair — see the ``family`` branch below.

        Everything is guarded: the docs warn twice that this call should not
        be made while a device dialog or the device factory UI is open, and a
        plugin cannot detect either. A grouping failure must therefore be a
        log line, never something that sinks a reconcile pass that has real
        work in it.
        """
        if int(dev_id) == int(node_dev_id):
            return
        try:
            group = [int(member) for member in indigo.device.getGroupList(dev_id)]
        except Exception as exc:  # noqa: BLE001
            # Debug, not a warning (issue #204 review, fix D): by far the most
            # likely cause is an endpoint device the user deleted by hand —
            # plugin.deviceDeleted only prunes matterNode ids, so a dead
            # `_index` id can reach here — and that is not something to put a
            # WARNING in the user's event log about. _derive_family_base treats
            # the identical condition the same way.
            self.logger.debug("could not read the device group of %s: %s", dev_id, exc)
            return
        if int(node_dev_id) in group:
            # Already done — from ANY end. Whether the node device is the
            # group's root (a fresh commission, where it was created first and
            # is therefore the oldest) or merely a member (every fielded
            # install) is not this method's business: ADR-0009.
            return
        # An ungrouped device answers [dev_id] (or, on some versions, nothing
        # at all — undocumented either way); both fall through to the group
        # call below, and neither is mistaken for a user-built group.
        if len(group) > 1:
            if set(group) <= family:
                # Every id in this group is one of THIS NODE'S OWN devices
                # (incl. the node device itself, which is why it is not in
                # `group` already) — not a user's foreign arrangement, just
                # this node's own membership caught mid-repair. Two fielded
                # ways this happens: a node device deleted by hand and
                # recreated via the "Recreate Matter node devices…" menu
                # (the group of its own endpoint devices survives the
                # delete — nothing ever ungroups them), or two siblings
                # grouped together without ever picking up the node device
                # (a leftover from before this migration, or the user's own
                # tidying). Either way the fix is the same: join the node
                # device into the surviving group.
                #
                # This is SINGLETON-JOINS-GROUP — the live experiment's
                # third line (jarvis, 2026-08-12): a singleton (temp) joined
                # an existing pair ([motion, node]) via
                # ``groupWithDevice(temp, node)``, one of its members named
                # as the second argument. The reverse direction — pulling
                # ONE member OUT of an established group to join it to a
                # singleton elsewhere — is deliberately never used here: the
                # experiment never ran that case, so anyone relaxing this
                # guard to cover it must extend the live experiment first.
                try:
                    node_group = [
                        int(member) for member in indigo.device.getGroupList(node_dev_id)]
                except Exception as exc:  # noqa: BLE001
                    self.logger.debug(
                        "could not read the device group of node device %s: %s",
                        node_dev_id, exc)
                    return
                if len(node_group) > 1:
                    # Guard: the node device is unexpectedly already a member
                    # of some OTHER group of its own — should not happen (it
                    # was just created, or is a singleton). Merging two
                    # already-established groups is not something the third
                    # line of the experiment covers, so this leaves both
                    # alone rather than guess.
                    self.logger.debug(
                        "node device %s is already in its own device group — not joining "
                        "it into device %s's family group", node_dev_id, dev_id)
                    return
                self._group_and_verify(dev_id, node_dev_id, node_dev_id, dev_id)
                return
            # Deliberately says nothing about which member roots it: under
            # age ordering that is frequently `dev_id` itself, and "device
            # 5678 is in a group rooted at 5678" is not a sentence to put in
            # a user's event log.
            self._group_warn(
                dev_id, "foreign",
                "device %s is already in a device group that its Matter node device %s is "
                "not part of, so it has not been added to the node's group — ungroup it if "
                "you want it grouped with the rest of the node", dev_id, node_dev_id)
            return
        # Arg order is irrelevant (the experiment: the two orders returned
        # byte-identical member lists), so this reads the way the docs' own
        # example does and means nothing more than "these two belong in one
        # group".
        self._group_and_verify(dev_id, node_dev_id, dev_id, node_dev_id)

    @staticmethod
    def _endpoint_by_id(node: NodeInfo, endpoint_id: int) -> Optional[Any]:
        for endpoint in node.endpoints:
            if int(endpoint.endpoint_id) == endpoint_id:
                return endpoint
        return None

    @staticmethod
    def _is_meter_source_candidate(endpoint: Any, specs: list) -> bool:
        """A device-bearing endpoint that has 0x0090/0x0091 but produces no
        primary handler spec of its own — a standalone energy-measurement
        endpoint whose reading must be attributed to another endpoint, or
        fall back to a standalone matterEnergyMeter device (issue #79).

        Requires the endpoint's clusters to be otherwise entirely within
        _NON_DEVICE_CLUSTERS (the same bar _unknown_spec uses): an endpoint
        that ALSO carries a genuinely-unsupported cluster is not a "pure"
        energy-measurement endpoint, so it must fall through to the
        matterUnknown placeholder path instead — otherwise the unsupported
        cluster is silently dropped with no placeholder and no log line,
        reintroducing the exact "commission succeeds with nothing visible"
        failure mode issue #58 fixed.

        Endpoint 0 is exempted from the purity check (issue #204/ADR-0008):
        it always carries root utility clusters (General Commissioning,
        Network Commissioning, Operational Credentials, …) that are not in
        _NON_DEVICE_CLUSTERS, so a literal purity bar would keep excluding it
        even now that it may host 0x0090/0x0091. Nothing is lost by skipping
        the bar here: ``_unknown_spec`` already returns ``None`` for endpoint
        0 unconditionally (by design, since before this change existed), so
        endpoint 0 never got a matterUnknown placeholder either way — the
        purity bar's #58 concern was never covered BY a placeholder here, it
        was covered by other surfaces. The writable half of what a
        root-utility cluster exposes surfaces via settings_report's survey
        (WRITABLE attributes only); anything read-only is visible on demand
        via the explorer (settings_report.explore_lines), not in this report.
        This is preferred over maintaining a parallel root-utility-clusters
        frozenset that would rot as the model grows.
        """
        if specs:
            return False
        if not (endpoint.has(CLUSTER_ELECTRICAL_POWER) or endpoint.has(CLUSTER_ELECTRICAL_ENERGY)):
            return False
        if int(endpoint.endpoint_id) == 0:
            return True
        return not set(endpoint.cluster_ids) - _NON_DEVICE_CLUSTERS

    def _resolve_meter_target(self, node: NodeInfo, source_endpoint: Any,
                              meter_capable_eps: set) -> Optional[int]:
        """Resolve the single endpoint a candidate source endpoint's energy
        readings should be attributed to, or None if attribution is ambiguous
        (caller falls back to a standalone matterEnergyMeter device).

        1. SetTopology (0x009C FeatureMap bit 2): read AvailableEndpoints
           (preferring ActiveEndpoints when DynamicPowerFlow is also set) and
           link only when exactly one listed endpoint hosts a
           _METER_CAPABLE_TYPES device. A malformed endpoint-list element
           (None/str/dict — issue #80 review point C) degrades to the
           sole-actuator heuristic below rather than raising and aborting the
           whole node's device creation.
        2. NodeTopology, no 0x009C at all, or SetTopology attrs unreadable:
           the node's SINGLE endpoint hosting a _METER_CAPABLE_TYPES device
           (the "sole actuator" heuristic) — zero or more than one candidate
           means attribution is ambiguous, so no link is made. Never guess on
           a multi-actuator node (bridges, power strips without SetTopology).

        Every path that returns None is logged at debug (issue #80 review
        point D) so "why do I have two devices for my plug" is diagnosable
        from the event log without adding print-debugging.
        """
        eid = int(source_endpoint.endpoint_id)
        feature_map = node.attributes.get(
            (eid, CLUSTER_POWER_TOPOLOGY, ATTR_POWER_TOPOLOGY_FEATURE_MAP)
        )
        if source_endpoint.has(CLUSTER_POWER_TOPOLOGY) and isinstance(feature_map, int) \
                and feature_map & FEATURE_SET_TOPOLOGY:
            attr_id = ATTR_ACTIVE_ENDPOINTS if feature_map & FEATURE_DYNAMIC_POWER_FLOW \
                else ATTR_AVAILABLE_ENDPOINTS
            listed = node.attributes.get((eid, CLUSTER_POWER_TOPOLOGY, attr_id))
            if isinstance(listed, list):
                try:
                    listed_eps = {int(e) for e in listed}
                except (TypeError, ValueError):
                    listed_eps = None
                if listed_eps is not None:
                    candidates = sorted(listed_eps & meter_capable_eps)
                    if len(candidates) == 1:
                        return candidates[0]
                    self.logger.debug(
                        "meter-link: node %s source endpoint %s SetTopology "
                        "listed=%s meter-capable=%s — ambiguous/empty match, no link",
                        node_id_to_str(node.node_id), eid,
                        sorted(listed_eps), sorted(meter_capable_eps),
                    )
                    return None
                # malformed endpoint-list element — fall through to the
                # sole-actuator heuristic below rather than raise.
            # SetTopology bit set but the endpoint-list attribute is
            # unreadable — fall through to the sole-actuator heuristic below.
        candidates = sorted(meter_capable_eps)
        if len(candidates) == 1:
            return candidates[0]
        self.logger.debug(
            "meter-link: node %s source endpoint %s sole-actuator heuristic "
            "candidates=%s — expected exactly one meter-capable endpoint, no link",
            node_id_to_str(node.node_id), eid, candidates,
        )
        return None

    def _resolve_meter_links(self, node: NodeInfo, specs_by_ep: dict) -> None:
        """Rebuild this node's meter-link maps (issue #79 — split-endpoint
        energy measurement).

        No persistent storage: rebuilt from the node model on every
        create_devices call (commission, node_added, reconcile), so a stale
        link from a prior interview state self-heals automatically.
        """
        node_id = int(node.node_id)
        with self._lock:
            self._forward_links = {
                k: v for k, v in self._forward_links.items() if k[0] != node_id
            }
            self._reverse_links = {
                k: v for k, v in self._reverse_links.items() if k[0] != node_id
            }
        # A target endpoint that already has its OWN co-located 0x0090/0x0091
        # (Tapo-style) alongside its actuator clusters must never also become
        # a link TARGET (issue #80 review point A): otherwise an unrelated
        # orphaned electrical-only endpoint on the same node links onto it and
        # overwrites its own readings (both at priming and via live routing).
        # Such a node's orphan endpoint correctly falls back to a standalone
        # matterEnergyMeter device instead.
        endpoints_by_id = {int(ep.endpoint_id): ep for ep in node.endpoints}
        meter_capable_eps = set()
        for eid, specs in specs_by_ep.items():
            if not any(spec.device_type_id in _METER_CAPABLE_TYPES for spec in specs):
                continue
            ep = endpoints_by_id.get(eid)
            if ep is not None and (
                ep.has(CLUSTER_ELECTRICAL_POWER) or ep.has(CLUSTER_ELECTRICAL_ENERGY)
            ):
                continue
            meter_capable_eps.add(eid)
        forward: dict[tuple[int, int], int] = {}
        reverse: dict[tuple[int, int], set] = {}
        for endpoint in node.endpoints:
            eid = int(endpoint.endpoint_id)
            if not self._is_meter_source_candidate(endpoint, specs_by_ep.get(eid, [])):
                continue
            target_eid = self._resolve_meter_target(node, endpoint, meter_capable_eps)
            if target_eid is None:
                continue
            forward[(node_id, eid)] = target_eid
            reverse.setdefault((node_id, target_eid), set()).add(eid)
        with self._lock:
            self._forward_links.update(forward)
            self._reverse_links.update(reverse)

    # ------------------------------------------------------------------
    # Power coverage readers (issue #205)
    # ------------------------------------------------------------------

    def _battery_endpoints(self, node_id: Any) -> frozenset[int]:
        """Endpoints on this node that some power source says it powers.

        The devices on these endpoints are the ones that should carry a
        ``batteryLevel`` state, so this is what the creation prop and the
        reconcile self-heal both ask. An empty answer means "no power source
        told us about this node" — which is also what an unknown node returns,
        and correctly so: the prop is add-only, so nothing is withdrawn by it.

        add-only: exclusion never withdraws a state — see the one-time INFO
        in create_devices (``_warn_of_newly_excluded_battery_devices``) for
        the diagnostic that covers the resulting staleness instead.
        """
        with self._lock:
            by_source = self._power_coverage.get(int(node_id))
        if not by_source:
            return frozenset()
        return frozenset().union(*by_source.values())

    def _battery_targets(self, node_id: Any, source_ep: Any) -> Optional[frozenset[int]]:
        """Endpoints a reading from ``(node_id, source_ep)`` applies to.

        ``None`` is not "nothing" — it is **no authority cached**, and callers
        must read it as "keep the node-wide default this code had before issue
        #205". That is the pre-change behaviour for a node no create pass has
        described yet (a live attribute arriving before the first reconcile),
        and it deliberately errs towards delivering a reading to too many
        devices rather than dropping it: a battery level on a sibling is
        cosmetic and self-corrects on the next pass, a battery level that never
        arrives looks like broken hardware.

        A source that is UNKNOWN inside a node we do have a map for is a
        different case, and gets a different answer: the map is the whole node's
        answer, so an absent source is one the snapshot did not carry. With
        several sources on the node, confine it to its own endpoint (the #82
        posture — never fan an unattributed reading across a bridge's children);
        with one or none, there is nothing to cross-contaminate, so fall back to
        the node-wide default.
        """
        with self._lock:
            by_source = self._power_coverage.get(int(node_id))
        if not by_source:
            return None
        targets = by_source.get(int(source_ep))
        if targets is not None:
            return targets
        return frozenset({int(source_ep)}) if len(by_source) > 1 else None

    def _warn_of_newly_excluded_battery_devices(self, node: NodeInfo, coverage: Any) -> None:
        """One-time INFO: an existing device's endpoint just dropped OUT of
        this node's power coverage (issue #205 upgrade gap).

        ``SupportsBatteryLevel``/``batteryLevel`` are add-only (see
        ``_battery_endpoints``): exclusion never withdraws them, so a device
        fed a reading under looser (or absent) EndpointList evidence keeps
        showing its last-written value forever once fresher evidence excludes
        its endpoint — silently, because reconcile only runs on a WS
        reconnect. This cannot fix that (add-only is deliberate policy
        elsewhere), but it can name the device once so the staleness is
        discoverable rather than invisible.

        Only the ``SupportsBatteryLevel`` prop gates this — not whether
        ``batteryLevel`` looks like a real reading. There used to be a second
        gate here ("a reading actually arrived"), but issue #190 established
        that Indigo initialises a declared Integer state to 0 on device
        creation, so a bare ``0`` is indistinguishable from "no reading ever
        arrived"; treating it as a live value would silently exclude exactly
        the devices most likely to need the warning (freshly created, never
        updated). The prop alone still catches the upgrade case: a device
        created before #205 already carries it, and it is precisely THIS
        pass's fresh routing evidence that newly excludes the endpoint.

        Only endpoints still present on the node are considered — see the
        ``present`` filter below. ``eid not in coverage.covered`` also fires
        when a bridge child DEPARTS the node entirely (``coverage.covered`` is
        derived from ``node.endpoints`` under the rev-1 fallback), and that
        case already has its own, more accurate diagnosis: the orphan sweep in
        ``reconcile_all`` marks a departed endpoint's device unreachable. This
        warning is only about an endpoint that is still there, whose routing
        just narrowed.

        Guarded to once per (node, device) per plugin run via
        ``_battery_exclusion_warned`` — this runs on every informative
        create/reconcile pass, and a WS reconnect can recur many times in a
        session, so without the guard the same device would nag every time.

        Never raises. This is a diagnostic hanging off the creation path, and
        a diagnostic that can break reconciliation is worse than no
        diagnostic.
        """
        try:
            node_id = int(node.node_id)
            present = {int(ep.endpoint_id) for ep in node.endpoints}
            with self._lock:
                excluded = [
                    (eid, dev_id)
                    for (nid, eid), type_map in self._index.items()
                    if nid == node_id and eid in present and eid not in coverage.covered
                    for dev_id in type_map.values()
                ]
            for eid, dev_id in excluded:
                key = (node_id, dev_id)
                if key in self._battery_exclusion_warned:
                    continue
                try:
                    dev = indigo.devices[dev_id]
                except KeyError:
                    continue  # deleted out-of-band — nothing to warn about
                if not dev.pluginProps.get("SupportsBatteryLevel"):
                    continue
                battery = dev.states.get("batteryLevel")
                self._battery_exclusion_warned.add(key)
                self.logger.info(
                    "node %s endpoint %s (device %s \"%s\"): no power source on "
                    "this node covers this endpoint any more under the current "
                    "routing (device's own EndpointList evidence where reported, "
                    "the rev-1 fallback otherwise) — its batteryLevel state "
                    "(currently %s) will not update further.",
                    node_id_to_str(node.node_id), eid, dev_id, getattr(dev, "name", "?"), battery,
                )
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not sink reconcile
            self.logger.debug(
                "battery-exclusion warning for node %s failed: %s",
                node_id_to_str(getattr(node, "node_id", "?")), exc,
            )

    def _create_one(self, spec: Any, name: str, folder_id: int = 0,
                    model: str = "") -> Optional[int]:
        # Stamp the type the cluster pipeline chose, so the type-edit guard
        # (validateDeviceConfigUi + deviceStartComm) can detect a manual change
        # via Indigo's Edit Device Type menu (issue #58).
        spec.props.setdefault("createdTypeId", spec.device_type_id)
        try:
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId=spec.device_type_id,
                name=self._unique_name(name),
                props=spec.props,
                folder=folder_id,
            )
        except ValueError as exc:  # noqa: BLE001 - surface but don't abort the batch
            if "NameNotUnique" in str(exc):
                # A device with this name exists but was invisible to both the
                # index and _unique_name — live evidence (issue #62): a device
                # whose type was changed via Indigo's Type menu is left
                # configured=False, drops out of iter("self"), and never gets
                # deviceStartComm, so neither #58 guard can see it. Replace the
                # raw traceback with the remedy.
                self.logger.warning(
                    "a device named \"%s\" already exists but is not usable for this "
                    "node (typically: its type was changed via Indigo's Type menu, "
                    "leaving it unconfigured) — delete that device and reload this "
                    "plugin; it will be recreated correctly",
                    name,
                )
            else:
                self.logger.exception(exc)
            return None
        except Exception as exc:  # noqa: BLE001 - surface but don't abort the batch
            self.logger.exception(exc)
            return None
        if model:
            # Cosmetic only (the Model column); a failure here must never sink
            # an otherwise-created device.
            try:
                dev.model = model
                dev.replaceOnServer()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("could not set model on device %s: %s", dev.id, exc)
        if spec.initial_states:
            try:
                dev.updateStatesOnServer(_kvlist(spec.initial_states))
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("initial state set failed: %s", exc)
            if "reachable" in spec.initial_states:
                # Second write, uiValue-bearing (display column UX, see
                # _reachable_kv): initial_states is a plain {key: value} dict
                # (_kvlist above has no uiValue slot), so the write just above
                # left the column reading a bare "yes"/"no". apply_states
                # carries the kv-dict uiValue form; on a device this fresh
                # (just created, no errorState yet) its errorState-clearing
                # side effect is a no-op.
                self.apply_states(
                    dev.id, [_reachable_kv(spec.initial_states["reachable"])])
        return dev.id

    # ------------------------------------------------------------------
    # Folder ("room") + identity helpers
    # ------------------------------------------------------------------
    def _resolve_folder_id(self, room: Optional[str]) -> int:
        """Map a Domio 'room' to an Indigo device-folder id, creating it if absent.

        Indigo has no native room concept — devices are organised into folders —
        so a suggestedRoom becomes a device folder. Returns 0 (no folder) when no
        room is given, or when the folder can't be resolved/created (a folder
        problem must never sink device creation).
        """
        name = (room or "").strip()
        if not name:
            return 0
        try:
            for folder in indigo.devices.folders:
                if folder.name == name:
                    return folder.id
            return indigo.devices.folder.create(name).id
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("could not resolve/create device folder %r: %s", name, exc)
            return 0

    def _move_to_folder(self, dev_id: int, folder_id: int, *, dedup: bool = False) -> None:
        try:
            try:
                indigo.device.moveToFolder(dev_id, value=folder_id)
            except TypeError:  # older positional signature
                indigo.device.moveToFolder(dev_id, folder_id)
        except Exception as exc:  # noqa: BLE001
            # `dedup` is for the true-up callers only — see _apply_identity.
            self._warn_once_if(dedup, dev_id, "folder",
                               "could not move device %s to folder %s: %s",
                               dev_id, folder_id, exc)

    def _warn_once_if(self, dedup: bool, dev_id: int, kind: str, fmt: str, *args: Any) -> None:
        """Warn — once per device per kind when ``dedup``, every time otherwise.

        The true-up paths (issue #204 stage 2) run on EVERY reconcile pass, so a
        rename or folder move Indigo keeps refusing would nag once per pass
        forever; the grouping side already built ``_group_warned`` for exactly
        that shape and this shares it (issue #204 review, fix E). The
        authoritative commission caller keeps the undeduped warning: it is a
        one-shot, user-initiated action whose failure must be visible each time
        the user asks for it.
        """
        if dedup:
            self._group_warn(dev_id, kind, fmt, *args)
        else:
            self.logger.warning(fmt, *args)

    def _apply_identity(self, dev_id: int, name: str, folder_id: int,
                        *, dedup: bool = False) -> bool:
        """Stamp a commission-chosen name/folder onto an already-created device.

        Used when node_added raced ahead and created the device with the bare
        product name before the commission job (which carries the user's choices)
        ran. Idempotent: a device that already matches is left untouched.

        Returns whether the device is now called exactly ``name`` — False when
        the rename failed, when the device was unreadable, or when a collision
        forced ``_unique_name``'s numeric substitute. The true-up callers
        depend on the answer (issue #204 review, fix B: ``nodeBaseName`` must
        never advance past a rename that did not land); the authoritative
        commission caller ignores it, and its substitute-name behaviour is
        unchanged.
        """
        try:
            dev = indigo.devices[dev_id]
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("apply_identity: device %s unavailable: %s", dev_id, exc)
            return False
        renamed = False
        try:
            target = name if dev.name == name else self._unique_name(name)
            if dev.name != target:
                dev.name = target
                dev.replaceOnServer()
            # Only AFTER replaceOnServer returns: `dev.name` was already
            # mutated in memory above, so it is not evidence on its own.
            renamed = target == name
        except Exception as exc:  # noqa: BLE001
            self._warn_once_if(dedup, dev_id, "rename",
                               "could not rename device %s to %r: %s", dev_id, name, exc)
        if folder_id and getattr(dev, "folderId", 0) != folder_id:
            self._move_to_folder(dev_id, folder_id, dedup=dedup)
        return renamed

    def _prime_states(self, node: NodeInfo, dev_id: int, endpoint_id: int,
                      own_type_id: str,
                      ep_sibling_types: Optional[set] = None) -> None:
        """Apply the node's current attribute values to a freshly-created device.

        get_node carries a snapshot of every attribute; matter-server only emits
        attribute_updated on subsequent *changes*, so without this a device whose
        value is static at connect time would sit at its hardcoded initial state.

        Two passes: first the device's own endpoint (standard clusters); then any
        OTHER endpoints whose cluster handler is node-scoped (e.g. PowerSource on
        endpoint 0 primes battery level into a sensor on endpoint 1), OR that are
        a linked meter-source endpoint for this device (issue #79 — split-endpoint
        energy, e.g. IKEA GRILLPLATS ep2's ActivePower priming the ep1 relay).

        The node-scoped cross-endpoint fan-in is itself limited to the
        endpoints the source says it powers (issue #205 — ``_battery_targets``,
        from the device's own EndpointList): otherwise a bridge child's battery
        reading would prime its siblings too, which is issue #82. A source with
        no cached authority keeps the original any-endpoint behaviour.

        Within the own endpoint, skip attributes whose cluster's handler targets
        a *different* existing device on the same endpoint (fix/#44: prevents the
        Pressure device being primed with Flow values or an AQ device being primed
        with CO2 values it doesn't own) — and, since #204/ADR-0008, also skip
        0x0090/0x0091 on the own endpoint when that endpoint is itself a
        resolved meter-link SOURCE: its device (only ever the node device
        today, endpoint 0) must not mirror a reading _resolve_meter_links
        already sent to another endpoint's device.

        The sibling-type skip is narrowed to fire only when the handler's type
        actually exists as a SEPARATE device on the endpoint.  Merge-into handlers
        (FanControl co-located with a Thermostat) carry their standalone
        device_type_id ("matterFan") but share this device — they must still prime.

        ``ep_sibling_types`` is the *full* set of device_type_ids for this
        endpoint (all types that will ever exist, not just those created so far).
        Callers must pass the complete set — ``create_devices`` derives it from
        the plan list before creation starts; ``_refresh_live_node`` derives it
        from the live index (which is complete by the time refresh runs).
        When ``None`` (legacy / fallback), the current index snapshot is used.
        """
        if ep_sibling_types is None:
            with self._lock:
                ep_sibling_types = set(
                    self._index.get((int(node.node_id), int(endpoint_id)), {}).keys()
                )
        dev = indigo.devices[dev_id]
        with self._lock:
            linked_sources = self._reverse_links.get((int(node.node_id), int(endpoint_id)), set())
            # Endpoint 0 is the ONE link-source endpoint whose own device must
            # not ALSO be primed with the reading _resolve_meter_links just
            # attributed to another endpoint's device — that would show the
            # same wattage twice. It is special because its device is the
            # NODE device (issue #204/ADR-0008): a plug may carry 0x0090/0x0091
            # on ep0 while its relay sits on ep1, and the node device has its
            # own real identity independent of that relay, so it must not
            # mirror a reading that was just attributed elsewhere. This guard
            # is symmetric with _lookup_for_cluster's live-routing rewrite,
            # which already sends a live update to the linked TARGET only.
            #
            # Every OTHER link-source endpoint (>0) is different: its device,
            # when it has one, only exists BECAUSE no actuator claimed it
            # (_is_meter_source_candidate/_energy_meter_spec's standalone
            # matterEnergyMeter fallback) — there is no separate "real"
            # identity for a reading to duplicate, so it must keep receiving
            # its own readings normally, including on a LATER pass where its
            # link only now resolves (issue #204 review, fix B — narrowed from
            # "any link-source endpoint hosting a device", which froze such a
            # standalone meter at its pre-link reading with no diagnostic).
            own_forward_target = self._forward_links.get((int(node.node_id), int(endpoint_id)))
        kv: list = []
        for (ep, cluster, attribute), value in node.attributes.items():
            handler = self.registry.handler_for_cluster(cluster)
            if handler is None:
                continue
            if (ep == endpoint_id and int(ep) == 0 and own_forward_target is not None
                    and cluster in (CLUSTER_ELECTRICAL_POWER, CLUSTER_ELECTRICAL_ENERGY)):
                continue
            # Include attributes from: this device's own endpoint, any
            # node-scoped cluster living on a different endpoint, or a linked
            # meter-source endpoint (issue #79 — split-endpoint energy).
            if ep != endpoint_id and not handler.node_scoped and ep not in linked_sources:
                continue
            # Issue #205: a node-scoped cluster (PowerSource) on a DIFFERENT
            # endpoint only fans in when that source says it powers THIS
            # endpoint — a bridge with several battery-powered children must
            # not cross-contaminate (issue #82). No cached authority (None)
            # keeps the pre-#205 node-wide fan-in.
            if ep != endpoint_id and handler.node_scoped:
                targets = self._battery_targets(node.node_id, ep)
                if targets is not None and int(endpoint_id) not in targets:
                    continue
            # For this device's own endpoint, skip attributes that belong to a
            # sibling device (handler has a non-empty device_type_id that differs
            # from this device's type).  This prevents e.g. the Pressure device
            # being primed with Flow values, or an AQ device receiving CO2 values.
            #
            # However, *merge-into* handlers (e.g. FanControl co-located with a
            # Thermostat) carry their standalone device_type_id ("matterFan") but
            # share the thermostat device — they must still prime.  Only skip when
            # the handler's type actually exists as a SEPARATE device on this
            # endpoint (present in ep_sibling_types); if the type is absent the
            # handler is in merge-into mode and its attributes belong here.
            if ep == endpoint_id and not handler.node_scoped:
                handler_type_id = getattr(handler, "device_type_id", "") or ""
                if handler_type_id and handler_type_id != own_type_id:
                    # Only skip when this type exists as a SEPARATE device on the endpoint.
                    # Merge-into handlers (FanControl co-located with a Thermostat) carry their
                    # standalone device_type_id but share this device — they must still prime.
                    if handler_type_id in ep_sibling_types:
                        continue
            try:
                update = handler.on_attribute_update(dev, attribute, value)
                if update:
                    kv.extend(handler.format_kv(update))
            except Exception as exc:  # noqa: BLE001 - one bad attr must not abort priming
                self.logger.warning("prime %s attr %s/%s failed: %s", dev_id, cluster, attribute, exc)
        if kv:
            self.apply_states(dev_id, kv)

    # ------------------------------------------------------------------
    # Writable-setting limits cache (issues #85, #186)
    # ------------------------------------------------------------------

    def _cache_setting_limits(self, node: NodeInfo) -> bool:
        """Cache what the ConfigUI layer needs to offer settings on this node.

        Two things, both scanned straight off the node snapshot rather than
        through a handler, because neither is an Indigo state — a limit and a
        capability are not readings, so there is nothing to dispatch them to:

        * the **limits attribute** behind any setting that declares one, stored
          RAW because each setting declares its own parser (a struct for
          HoldTimeLimits, a count for SupportedSensitivityLevels) and parsing
          at read time keeps the code that knows a limit's SHAPE next to the
          code that declares it;
        * each settings-bearing cluster's **AttributeList**, which is the
          device's own statement of what it implements.

        This node's entries are REBUILT, not merged over (issue #192). The cache
        used to be append-only — and it is worth being precise about what that
        did and did not break, because the obvious reading is wrong. A key that
        was RE-reported simply overwrote, so a changed AttributeList or a widened
        limit was never stale. What survived forever was a key that stopped
        appearing at all: a cluster or endpoint the node no longer reports, a
        limits attribute that vanished while its cluster stayed, or the leftovers
        of a different device on a RE-USED node id whose topology does not
        happen to overwrite the same keys (ids are assigned at commissioning, so
        re-use across a decommission/recommission cycle is ordinary). Plus the
        unbounded growth. Rebuilding handles all of it with no separate eviction
        path to keep in step.

        The caller guarantees the snapshot is informative — see ``create_devices``
        for why an empty one must never reach here.

        Returns whether this node's **AttributeLists** changed, which is the half
        that decides what the user can see (settings offered, and since #190
        device states). Limits moving does not change any of that, so it is not
        worth rebuilding a state list over.
        """
        wanted = set()
        for setting in SETTINGS:
            attribute = getattr(setting.bounds, "attribute", None)
            if attribute is not None:
                wanted.add((setting.cluster, int(attribute)))
        clusters = {s.cluster for s in SETTINGS}
        node_id = int(node.node_id)
        limits: dict[tuple[int, int, int, int], Any] = {}
        attribute_lists: dict[tuple[int, int, int], Any] = {}
        for (ep, cluster, attribute), value in node.attributes.items():
            if (cluster, attribute) in wanted:
                limits[(node_id, int(ep), int(cluster), int(attribute))] = value
            elif attribute == ATTR_ATTRIBUTE_LIST and cluster in clusters:
                attribute_lists[(node_id, int(ep), int(cluster))] = value
        with self._lock:
            previous = {key: value for key, value in self._attribute_lists.items()
                        if key[0] == node_id}
            # Replaced wholesale rather than mutated in place, so a reader never
            # observes a half-rebuilt cache and never has to take the lock — see
            # setting_limits/attribute_list for why a lock-free read matters.
            self._setting_limits = {
                **{k: v for k, v in self._setting_limits.items() if k[0] != node_id},
                **limits}
            self._attribute_lists = {
                **{k: v for k, v in self._attribute_lists.items() if k[0] != node_id},
                **attribute_lists}
        # Compared by CONTENT, not by wire order: matter-server gives no ordering
        # guarantee for an AttributeList, and a device re-reporting the same ids
        # in a different order is not a capability change. Treating it as one
        # would rebuild every state list on the node and print the removal log
        # again — the exact flap the no-flap rule exists to prevent.
        return _capability_fingerprint(attribute_lists) != _capability_fingerprint(previous)

    # ------------------------------------------------------------------
    # Settable-attribute report (issue #191)
    # ------------------------------------------------------------------

    def offered_setting_pairs(self, node_id: Any, endpoint_id: Any) -> set:
        """``{(cluster, attribute)}`` the plugin already offers on an endpoint.

        Derived from the Indigo device TYPES that exist there, because that is
        what decides whether a setting has a ConfigUI field at all — a setting is
        declared against types and the XML cannot be generated, so a type with no
        field offers nothing however capable the hardware is.

        Empty for an endpoint with no Indigo device, which is correct rather than
        merely convenient: nothing is offered for a device that does not exist,
        so everything it implements is a genuine gap.
        """
        with self._lock:
            type_ids = set(self._index.get((int(node_id), int(endpoint_id)), {}))
        return {(s.cluster, s.attribute)
                for type_id in type_ids if type_id
                for s in settings_for_type(type_id)}

    def _device_names(self, node_id: Any, endpoint_id: Any) -> list:
        """Indigo device names on an endpoint, for the report's headings."""
        with self._lock:
            dev_ids = sorted(set(self._index.get((int(node_id), int(endpoint_id)), {}).values()))
        names = []
        for dev_id in dev_ids:
            try:
                names.append(indigo.devices[dev_id].name)
            except KeyError:
                continue  # deleted between the index read and here; nothing to name
            except Exception as exc:  # noqa: BLE001 - a label must never break the report
                self.logger.debug("survey: name lookup for device %s failed: %s", dev_id, exc)
        return names

    def survey_node(self, node: NodeInfo) -> Any:
        """The settable-attribute survey for one node, with device names attached.

        Public because the on-demand menu path wants exactly this against a node
        it fetched itself, and duplicating the two lookups it needs — what is
        already offered, and what the endpoints are called — is how the menu and
        the automatic report would drift into disagreeing about the same device.
        """
        survey = settings_report.survey_node(
            node, lambda ep: self.offered_setting_pairs(node.node_id, ep))
        return settings_report.name_endpoints(
            survey, lambda ep: self._device_names(node.node_id, ep))

    def report_settable_attributes(self, node: NodeInfo, force: bool = False) -> bool:
        """Log what this node exposes that the plugin does not offer. Once.

        Returns whether anything was logged. ``force`` skips the once-per-device
        check for the on-demand menu item, which exists precisely because a
        device commissioned before this feature shipped would otherwise never
        report — and because a user asking a question deserves an answer even if
        the plugin already answered it to an empty log months ago.

        A node with nothing to report is recorded as reported anyway, so a
        fully-supported device does not re-survey on every reconcile pass for
        the rest of time. Nothing is logged for it either way.

        Never raises. This is a diagnostic hanging off the creation path, and a
        diagnostic that can break reconciliation is worse than no diagnostic.
        """
        try:
            survey = self.survey_node(node)
            fingerprint = survey.fingerprint()
            log = self.survey_log
            if not force:
                if log is None or not log.should_report(node.node_id, fingerprint):
                    return False
            lines = settings_report.report_lines(survey)
            if log is not None:
                log.record(node.node_id, fingerprint)
            if not lines:
                if force:
                    # Silence would read as a failure to a user who just asked.
                    self.logger.info(
                        "%s implements no settable Matter attributes beyond the ones "
                        "this plugin already offers.", survey.identity())
                return False
            for line in lines:
                self.logger.info("%s", line)
            return True
        except Exception as exc:  # noqa: BLE001 - a diagnostic must not sink reconcile
            self.logger.warning("settable-attribute report for node %s failed: %s",
                                node_id_to_str(getattr(node, "node_id", "?")), exc)
            return False

    def _forget_node_capabilities(self, node_id: Any) -> None:
        """Drop every cached capability answer for a node that is gone.

        The rebuild in :meth:`_cache_setting_limits` covers a node that is still
        reporting; this covers one that has stopped — decommissioned, or absent
        from matter-server's authoritative node list.

        Be precise about what forgetting costs, because "unknown is harmless" is
        only half true and the wrong half is the intuitive one:

        * It can never **withdraw a state**. ``unimplemented_states`` needs a
          positive NO, so unknown keeps every state.
        * It absolutely can **hide a setting**. ``offered_settings`` withholds a
          spec-bounded setting for want of positive evidence, and a
          ``FromAttribute`` one for want of resolvable limits — so a forgotten
          node shows an EMPTY Device Settings section until the next informative
          reconcile refills the cache.

        That is the right trade for a node that is genuinely gone, which is why
        this is called from ``node_removed`` (an explicit decommission) and
        deliberately NOT from ``reconcile_all``'s ``dropped`` set, where a
        transient short ``get_nodes()`` would blank a device that is sitting
        there working.

        **`_power_coverage` is deliberately NOT evicted here** — the same
        conclusion the pre-#205 `_power_source_eps` reached, but no longer for
        the same reason, and the old reason was the dangerous one. That cache
        was read as ``len(...) > 1``, so an absent entry was a positive "one
        power source" and evicting it re-opened issue #82's cross-contamination
        on a bridge with several battery children. Coverage does not have that
        failure mode: absent means "no coverage", which is the SAFE answer
        everywhere it is read — ``_battery_endpoints`` returns nothing (and the
        prop is add-only, so nothing is withdrawn), ``_battery_targets`` returns
        None and the caller keeps the node-wide default it had before #205.

        It stays because eviction would buy nothing, not because it would cost
        something: every informative pass rebuilds this node's entry outright,
        so a re-used node id is corrected the first time the new device reports.
        Cache eviction is still not a policy to apply uniformly — it depends
        entirely on what the empty case is read to MEAN, which for the two
        dicts above is the opposite of harmless.

        **`_forward_links`/`_reverse_links` ARE evicted here** (verifier note,
        issue #204 review) — a THIRD answer, neither of the two above: unlike
        `_power_coverage`, a removed node's links have no devices behind them
        to protect (the node is gone, full stop), so there is nothing an empty
        entry could wrongly mean. And unlike `_setting_limits`/
        `_attribute_lists`, `_resolve_meter_links` does not do a bare rebuild
        on the next informative pass the way `_cache_setting_limits` does —
        it only runs from `create_devices`, so a stale link surviving a
        decommission would sit there until the node (or whatever reuses its
        id) is recommissioned and passes through `create_devices` again. This
        closes that gap rather than relying on the eventual rebuild to.
        """
        target = int(node_id)
        with self._lock:
            self._setting_limits = {k: v for k, v in self._setting_limits.items()
                                    if k[0] != target}
            self._attribute_lists = {k: v for k, v in self._attribute_lists.items()
                                     if k[0] != target}
            self._forward_links = {k: v for k, v in self._forward_links.items()
                                   if k[0] != target}
            self._reverse_links = {k: v for k, v in self._reverse_links.items()
                                   if k[0] != target}
            # `_redundant_meter_logged` (issue #215) rides along with the links
            # above for the same reason: it is a fact ABOUT a link ("this one
            # made a device redundant"), so once the link is gone there is
            # nothing left for a stale entry to correctly describe. A
            # recommissioned node starts the resolution over from scratch, and
            # should be free to earn the one-time log again if it lands in the
            # same shape.
            self._redundant_meter_logged = {
                k for k in self._redundant_meter_logged if k[0] != target
            }
        # And the "already reported" mark (issue #191). Node ids are assigned at
        # commissioning and re-used across a decommission/recommission cycle, so
        # keeping it would let a genuinely different device inherit the old
        # one's mark and never report. Outside the lock — SurveyLog has its own,
        # and persisting reaches pluginPrefs.
        if self.survey_log is not None:
            self.survey_log.forget(target)
        # And the node-device tombstone (issue #204). Same reused-node-id
        # reasoning as survey_log.forget above: a genuinely different device
        # commissioned onto a reused id must not be born deleted because a
        # PREVIOUS occupant's node device was removed by hand. Unlike every
        # other cache this method drops, a tombstone has no self-heal to fall
        # back to if this were skipped — it exists specifically to survive a
        # reconcile, so decommission (an explicit "this node is gone") is the
        # one event allowed to clear it, not a routine WS reconnect.
        if self.node_tombstones is not None:
            self.node_tombstones.forget(target)

    def _refresh_state_lists(self, node_id: Any) -> None:
        """Ask Indigo to rebuild the state list of every device on a node.

        Called when a node's AttributeLists changed, because ``getDeviceStateList``
        runs at device start — long before the first reconcile has said what the
        device implements (issue #190). Without this the answer could only ever
        settle on the NEXT plugin start, which is worse than the stale state it
        replaces: identical installations would disagree depending on how many
        times they had been restarted.

        Never fatal. A device that will not rebuild its state list keeps the one
        it has, which is exactly the pre-#190 behaviour — but the node stays on
        ``_pending_state_refresh`` so the next pass tries again, because the
        cache has already moved and nothing else would ever notice.
        """
        target = int(node_id)
        with self._lock:
            dev_ids = [
                dev_id
                for (nid, _ep), type_map in self._index.items() if nid == target
                for dev_id in type_map.values()
            ]
        complete = True
        for dev_id in dev_ids:
            try:
                indigo.devices[dev_id].stateListOrDisplayStateIdChanged()
            except KeyError:
                # Deleted between the index read and here — the same deletion
                # race the rest of this class tolerates. Nothing to retry for.
                self.logger.debug("device %s vanished before its state list could rebuild", dev_id)
            except Exception as exc:  # noqa: BLE001 - a device that will not rebuild keeps its states
                # NOT debug: this workspace's field notes record that this call
                # rejects a bad state id with an error naming no key, and it is
                # a plugin defect rather than anything the user did. Silence here
                # leaves the device on the state list #190 exists to correct.
                complete = False
                self.logger.warning(
                    "could not rebuild the state list for device %s: %s — its Matter settings "
                    "states may show values the device never reported", dev_id, exc)
        if complete:
            self._pending_state_refresh.discard(target)

    def setting_limits(self, node_id: Any, endpoint_id: Any, cluster: Any,
                       attribute: Any) -> Any:
        """Raw limits value for a setting on (node, endpoint), or None if
        unknown — not yet reconciled, or this node does not implement it.

        None is load-bearing for a setting with FromAttribute bounds: the
        ConfigUI layer treats "no limits" as "do not offer", which is what stops
        a pre-1.4 occupancy sensor with no HoldTime being shown a field it would
        fail to honour. A setting whose bounds come from the spec never consults
        this at all — its capability check is :meth:`attribute_list`.

        Reads WITHOUT the lock, deliberately — see :meth:`attribute_list`.
        """
        return self._setting_limits.get(
            (int(node_id), int(endpoint_id), int(cluster), int(attribute)))

    def attribute_list(self, node_id: Any, endpoint_id: Any, cluster: Any) -> Any:
        """The cluster's AttributeList on (node, endpoint), or None if unknown.

        None means "not captured", NOT "implements nothing" — callers must not
        read it as proof a device lacks an attribute (see settings.implements).

        **Lock-free, because the lock buys nothing here** — not because it would
        be unsafe to take. Writers REPLACE the dict rather than mutate it, so a
        reader sees either the whole old map or the whole new one, and the
        rebinding is atomic under the GIL.

        An earlier version of this comment claimed the lock-free read was
        REQUIRED, on the grounds that ``getDeviceStateList`` runs on Indigo's
        thread and could deadlock against ``create_devices`` holding ``_lock``
        inside ``indigo.device.create()``. That argument does not survive
        contact with the callback it names: ``deviceStartComm`` reaches this via
        ``stateListOrDisplayStateIdChanged`` and then calls ``note_device``
        thirteen lines later, which takes ``_lock`` anyway — so the hang would
        merely move. Indigo evidently does not invoke ``deviceStartComm``
        synchronously on a foreign thread from inside ``device.create()``, since
        the plugin creates devices in the field without hanging there.

        Do not read this docstring as "Indigo-thread paths must avoid ``_lock``".
        Several already take it (``note_device``, ``set_active``, ``lookup`` from
        the actionControl path).
        """
        return self._attribute_lists.get(
            (int(node_id), int(endpoint_id), int(cluster)))

    def sensitivity_levels_supported(self, node_id: Any, endpoint_id: Any) -> Optional[int]:
        """SupportedSensitivityLevels for (node, endpoint), or None if unknown.

        Kept as a named accessor because the Set Sensitivity Level action's
        picker (issue #85) reads a COUNT, not a range.
        """
        raw = self.setting_limits(node_id, endpoint_id, CLUSTER_BOOLEAN_STATE_CONFIG,
                                  ATTR_SUPPORTED_SENSITIVITY_LEVELS)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Capability-prop helpers (issue #45 — self-heal mid-interview creations)
    # ------------------------------------------------------------------

    def _capability_props(self, node: NodeInfo, endpoint: Any) -> dict:
        """Return capability props implied by the node's CURRENT cluster set.

        These props unlock Indigo states that handlers write into:
        - SupportsPowerMeter   → curEnergyLevel   (cluster 0x0090 on the endpoint,
                                                     or on a LINKED source endpoint)
        - SupportsEnergyMeter  → accumEnergyTotal  (cluster 0x0091 on the endpoint,
                                                     or on a LINKED source endpoint)
        - SupportsBatteryLevel → batteryLevel      (a cluster 0x002F instance on the
                                                     node that says it powers THIS
                                                     endpoint — its EndpointList)

        …plus one prop that unlocks no state but decides how a handler READS the
        device: ``switchFeatureMap`` (cluster 0x003B's FeatureMap), which tells
        ``GenericSwitchHandler`` whether InitialPress is this switch's only press
        event (issue #231). It belongs here for the same reason the rest do —
        it is derived from the node's own attributes, and a device created from
        an incomplete interview would otherwise carry the wrong answer forever,
        which for a button means every press is discarded and the device never
        changes state at all.

        The cluster constants are imported from their handler modules — no magic
        numbers here.  The battery check mirrors create_devices' central setdefault
        (issue #205): both ask the same coverage question, one from the pass-local
        answer and one from the cache that pass wrote.

        No longer a ``@staticmethod``: split-endpoint energy (issue #79 — e.g.
        IKEA GRILLPLATS) needs the instance's meter-link map to fold a linked
        source endpoint's electrical clusters into the TARGET endpoint's props,
        so the reconcile self-heal (below) can add meter props to a relay
        whose energy actually lives on a sibling endpoint.
        """
        props: dict = {}
        props.update(meter_props(endpoint))
        with self._lock:
            linked_sources = self._reverse_links.get(
                (int(node.node_id), int(endpoint.endpoint_id)), set()
            )
        for src_eid in linked_sources:
            src_endpoint = self._endpoint_by_id(node, src_eid)
            if src_endpoint is None:
                continue
            props.update(meter_props(src_endpoint))
        if int(endpoint.endpoint_id) in self._battery_endpoints(node.node_id):
            props["SupportsBatteryLevel"] = True
        if endpoint.has(CLUSTER_SWITCH):
            features = switch_features(node, endpoint)
            if features:
                # Stringified to match what the handler stamps at creation, so
                # the heal below compares like with like and does not rewrite
                # the prop on every pass — the handler applies the same truthy
                # test for exactly that reason. A FeatureMap of 0 is not
                # stamped: a Switch cluster always declares at least one
                # feature, so zero means "not reported", which under ADR-0003
                # must leave the device's existing answer alone rather than
                # overwrite it with a value asserting no features at all.
                props[PROP_SWITCH_FEATURES] = str(features)
        return props

    def _reassert_capability_props(self, node: NodeInfo) -> None:
        """Re-assert any capability props that are absent/falsy on existing devices.

        Called from _refresh_live_node (reconcile + node_updated) after the node's
        cluster snapshot is fully populated.  A device created via node_added during
        commissioning may have been created while the attribute map was incomplete,
        so some Supports* props were not set — Indigo never created the states, so
        handler updates are silently dropped forever.

        Strategy:
        - For each device on each endpoint, compute the capability props the node
          NOW implies (via _capability_props).
        - Filter to props that are absent or falsy in the device's current pluginProps.
        - If any are missing: call replacePluginPropsOnServer — Indigo rebuilds device
          states from the new props automatically — then read the props back and
          claim success ONLY for keys that verifiably stuck.
        - Guard per-device so a single failure never sinks the rest of reconcile.
        - Cluster-implied caps are add-only, never removed: a flaky interview must
          not strip capabilities that were legitimately set in a prior pass.
        - Meter props (SupportsPowerMeter/SupportsEnergyMeter) only apply to device
          types in _METER_CAPABLE_TYPES; SupportsBatteryLevel applies to any type
          (mirrors create_devices' node-level central setdefault behaviour exactly).

        Also re-asserts each handler's static ``display_props`` (issue #56 —
        value sensors created without SupportsSensorValue display "off" forever).
        Unlike the cluster-implied caps these are exact assertions, not add-only:
        they are fixed truths of the device type (SupportsOnState must go False
        on a value sensor), not interview-dependent capabilities.
        """
        for endpoint in node.endpoints:
            full_cap = self._capability_props(node, endpoint)
            with self._lock:
                ep_key = (int(node.node_id), int(endpoint.endpoint_id))
                type_map = dict(self._index.get(ep_key, {}))
            for dev_id in type_map.values():
                try:
                    dev = indigo.devices[dev_id]
                    type_id = getattr(dev, "deviceTypeId", "") or ""
                    current_props = dev.pluginProps
                    handler = self.registry.handler_for_device(dev)
                    # None for an unknown deviceTypeId (e.g. stale index entry
                    # from a renamed type) — that device has no display contract.
                    display_props = getattr(handler, "display_props", {})
                    # Build the set of missing props for this specific device type.
                    # Meter props are only meaningful for relay/dimmer family devices.
                    missing: dict = {}
                    for key, value in full_cap.items():
                        if key in ("SupportsPowerMeter", "SupportsEnergyMeter"):
                            if type_id not in _METER_CAPABLE_TYPES:
                                continue
                        if key == PROP_SWITCH_FEATURES:
                            if type_id != DEVICE_TYPE_BUTTON:
                                # Only the button handler reads it. An endpoint
                                # can host more than one Indigo device, and
                                # stamping a Switch FeatureMap onto a sibling
                                # sensor would be a props write (and so a device
                                # comm restart) that buys nothing.
                                continue
                            # EXACT assertion, not add-only — the one prop here
                            # that is a VALUE rather than a boolean capability.
                            # Add-only would mean write-once-never-corrected: an
                            # OTA that adds MSL to a gang (0x02 → 0x0A) leaves
                            # the device stamped "2", so InitialPress AND
                            # LongPress both map and pressCount advances TWICE
                            # per long press — the issue #76 double-count,
                            # reintroduced through the prop layer and unfixable
                            # short of deleting the device. Same treatment (and
                            # same reason) as display_props below: a fixed truth
                            # about the endpoint, not an interview-dependent
                            # capability that a flaky pass might under-report.
                            # The add-only rule still protects it from a
                            # NON-informative snapshot, because an absent
                            # FeatureMap never reaches full_cap at all.
                            if str(current_props.get(key) or "") != value:
                                missing[key] = value
                            continue
                        if not current_props.get(key):
                            missing[key] = value
                    for key, value in display_props.items():
                        # Absence is divergence too: a missing SupportsOnState
                        # means Indigo's sensor default (True), so a False must
                        # be written explicitly, not skipped as already-falsy.
                        if key not in current_props or bool(current_props.get(key)) != bool(value):
                            missing[key] = value
                    if (type_id == DEVICE_TYPE_BUTTON
                            and PROP_SWITCH_FEATURES not in current_props
                            and PROP_SWITCH_FEATURES not in full_cap):
                        # The one way the #231 fix can fail while looking
                        # exactly like the bug it fixes. Without a FeatureMap
                        # the handler cannot know whether InitialPress is this
                        # switch's only press event, so it takes the safe
                        # default and discards it — and if the switch really is
                        # momentary-only, every press is dropped, no state ever
                        # changes, no error state is set, and the event log is
                        # empty. That is byte-for-byte the original report, with
                        # the fix installed. Say so once, here, where the node's
                        # own snapshot is in hand to prove the attribute is
                        # genuinely absent rather than merely unread.
                        self._group_warn(
                            dev_id, "switch-features",
                            "Matter button %r (node %s endpoint %s) has not reported its "
                            "Switch FeatureMap (0xFFFC), so this plugin cannot tell whether "
                            "it signals a press with InitialPress alone. Presses that carry "
                            "no release event will be ignored. If this button does nothing "
                            "in Indigo, please open an issue with the output of "
                            "'Explore Matter attributes (advanced)…' for this device.",
                            getattr(dev, "name", dev_id), node.node_id, endpoint.endpoint_id,
                        )
                    # Heal the type-edit guard's stamp onto devices created
                    # before it existed (issue #58). Records the CURRENT type as
                    # canonical — for a pristine fleet that is the created type.
                    if type_id and "createdTypeId" not in current_props:
                        missing["createdTypeId"] = type_id
                    # Heal the address (UI protocol-id column, issue #18) onto
                    # devices created before that stamping existed — it is the
                    # node id a user needs for decommission.
                    if not current_props.get("address"):
                        missing["address"] = node_id_to_str(node.node_id)
                    if not missing:
                        # Props are already correct — but Indigo caches the list
                        # display state at creation and may not re-derive it from
                        # a props replace, so a device created before the issue
                        # #56 fix can be prop-correct yet still display on/off.
                        # Nag (every pass — reconcile and node_updated) with the
                        # remedy. Own guard so a bug in the warning code is never
                        # misreported as a props-replace failure (and never blocks
                        # the meter/battery heal for the next device).
                        try:
                            self._warn_stale_display_state(dev, type_id, display_props)
                        except Exception as exc:  # noqa: BLE001
                            self.logger.warning(
                                "stale-display check failed on device %s: %s", dev_id, exc,
                            )
                        continue
                    props = dict(current_props)
                    props.update(missing)
                    dev.replacePluginPropsOnServer(props)
                    # Verify before claiming success: Indigo silently dropping a
                    # key is exactly the half-happened class issue #56 is about,
                    # and an unverified INFO here would mask it forever (the
                    # stale-display nag only runs on passes with nothing missing,
                    # so a never-converging replace would otherwise loop "success"
                    # without the actionable warning ever firing).
                    applied = indigo.devices[dev_id].pluginProps
                    stuck = [
                        key for key, value in missing.items()
                        if key not in applied
                        # switchFeatureMap is a value, not a flag: bool("2") and
                        # bool("30") are both True, so the boolean check below
                        # would pass a FeatureMap that did not actually persist.
                        or (str(applied.get(key) or "") != value
                            if key == PROP_SWITCH_FEATURES
                            else bool(applied.get(key)) != bool(value))
                    ]
                    if stuck:
                        self.logger.warning(
                            "props replace on device %s (%s) did not persist %s — "
                            "device may keep its previous capabilities/display",
                            dev_id, type_id, stuck,
                        )
                    else:
                        self.logger.info(
                            "re-asserted capability props %s on device %s (%s) for node %s",
                            list(missing.keys()), dev_id, type_id,
                            node_id_to_str(node.node_id),
                        )
                except Exception as exc:  # noqa: BLE001 - props failure must not sink reconcile
                    self.logger.warning(
                        "could not re-assert capability props on device %s: %s",
                        dev_id, exc,
                    )

    def _warn_stale_display_state(self, dev: Any, type_id: str, display_props: dict) -> None:
        """Warn when a device's cached display state is wrongly stuck on on/off.

        Indigo derives ``displayStateId`` from the Supports* props at device
        creation; replacing props afterwards rebuilds states and (verified
        live, 2026-06-12) normally re-derives the display too — this warning
        is dead-man insurance for the case where it doesn't. Applies to any
        type whose display_props disclaim the on/off display: value sensors
        (SupportsSensorValue) and UiDisplayStateId-fallback types like the
        button and air-quality sensor (both Supports* False). The remedy is
        user-side and cheap — deleting the Indigo device and reloading the
        plugin lets reconcile recreate it with the correct creation props —
        so name the device and say exactly that. Deliberately checked only on
        passes where no props needed replacing: right after a replace, Indigo
        may not have re-derived yet, and warning there would be noise (a
        replace that never converges is surfaced separately by the
        did-not-persist warning).
        """
        if display_props.get("SupportsOnState", True):
            return  # on/off IS the right display (binary sensors, non-sensor types)
        display_state = getattr(dev, "displayStateId", None)
        if display_state is None:
            # Unreadable is "unknown", not "healthy" — keep a trace, but a real
            # indigo.Device always has the attribute, so debug-level is enough.
            self.logger.debug("device %s has no readable displayStateId", dev.id)
            return
        if display_state != "onOffState":
            return
        self.logger.warning(
            "device %s (%s, %s) was created before the sensor display fix and "
            "still shows on/off in the device list instead of its reading — "
            "delete the Indigo device and reload this plugin; it will be "
            "recreated automatically with the correct display",
            dev.id, dev.name, type_id,
        )

    def _unique_name(self, name: str) -> str:
        existing = {dev.name for dev in indigo.devices}
        if name not in existing:
            return name
        suffix = 2
        while f"{name} {suffix}" in existing:
            suffix += 1
        return f"{name} {suffix}"

    # ------------------------------------------------------------------
    # Reconciliation (startup)
    # ------------------------------------------------------------------
    def reconcile_all(self, raw_nodes: list) -> None:
        self.rebuild_index()
        live: set[tuple[int, int]] = set()
        seen_nodes: set[int] = set()
        for raw in raw_nodes:
            # one malformed node must not sink reconciliation for the rest
            try:
                node = parse_node(raw)
            except Exception as exc:  # noqa: BLE001
                # Keep the node offered for decommission if its id is salvageable.
                # Parse failure is a property of the node's SHAPE, so it recurs on
                # every pass — dropping it would make a malformed node permanently
                # undecommissionable, and a malformed node is a prime candidate for
                # removal. The id is all _decommission needs.
                salvaged = _salvage_node_id(raw)
                if salvaged is not None:
                    seen_nodes.add(salvaged)
                self.logger.warning(
                    "skipping unparseable Matter node %s: %s",
                    node_id_to_str(salvaged) if salvaged is not None else "(id unreadable)", exc,
                )
                continue
            seen_nodes.add(int(node.node_id))
            for endpoint in node.endpoints:
                live.add((node.node_id, endpoint.endpoint_id))
                # (node_id, 0) — where a matterNode device is indexed
                # (issue #204) — is covered here with no special-casing:
                # endpoint 0 always parses out of a node's own attributes
                # (matter_model.parse_node derives endpoints from attribute
                # paths, and BasicInformation always reports on ep 0), so
                # whenever this pass is informative enough to reach here at
                # all, (node_id, 0) is already `live` and the orphan sweep
                # below leaves the node device alone. A non-informative
                # snapshot (empty attributes → no endpoints at all, so this
                # loop body never runs for that node) IS still possible and
                # DOES orphan (node_id, 0) along with everything else on that
                # node (issue #204 review, fix G.4/E) — harmless, because the
                # sweep only marks unreachable, never deletes.
            try:
                self.create_devices(node)
                # Reconcile reachability from matter-server's availability flag:
                # get_nodes returns ALL commissioned nodes (available or not), so
                # mere presence is not liveness. For a live node, refresh its state
                # (it may have changed while we were away) and clear any stale
                # 'unreachable'; for one matter-server reports offline, mark it.
                self._apply_reachability(node)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("reconcile of node %s failed: %s", node.node_id, exc)
        with self._lock:
            # raw_nodes is matter-server's authoritative list, so a node absent
            # from it is no longer commissioned — stop offering it for
            # decommission rather than inviting an op that cannot succeed.
            dropped = self._known_nodes - seen_nodes
            self._known_nodes = seen_nodes
            orphans = [
                dev_id
                for (node_id, _ep), type_map in self._index.items()
                if (node_id, _ep) not in live
                for dev_id in type_map.values()
            ]
            # Which of those orphaned endpoints is (node_id, 0) with a
            # matterNode entry — the type_map tells us without a second index
            # walk (issue #204 review, fix E: the orphan sweep below already
            # marks these dev_ids unreachable via errorState, but the node
            # device's own `reachable` state needs the same explicit write
            # _apply_reachability makes elsewhere).
            orphan_node_ids = {
                node_id
                for (node_id, ep), type_map in self._index.items()
                if ep == 0 and (node_id, ep) not in live and "matterNode" in type_map
            }
        if dropped:
            # A node with Indigo devices leaves evidence when it disappears (the
            # orphan sweep above marks them unreachable). One with NO devices
            # leaves none at all, so without this line a transient short/empty
            # get_nodes() would silently retract it from the decommission list and
            # read to the user as "it must already be gone".
            #
            # Wording is deliberate: dropping a node from _known_nodes does NOT
            # remove it from the picker, because list_nodes() unions _known_nodes
            # with _index and the orphan sweep leaves _index alone. So a node whose
            # Indigo devices still exist stays listed — which is what you want (it is
            # the only route to cleaning them up), and decommissioning it now
            # succeeds rather than failing forever on "Node N does not exist".
            self.logger.warning(
                "Matter node(s) %s are no longer reported by matter-server, so they are "
                "no longer commissioned on it. Any Indigo devices they left behind are "
                "marked unreachable and stay listed for decommission so you can remove "
                "them; nodes with no devices left drop off the list entirely.",
                ", ".join(node_id_to_str(n) for n in sorted(dropped)),
            )
        # NOTE: `dropped` deliberately does NOT evict the capability caches.
        # The comment above says why it cannot be trusted for that — a transient
        # short or empty get_nodes() puts a live node in here — and forgetting a
        # live node's answers is not the harmless act it first looks like:
        # offered_settings withholds a spec-bounded setting for want of positive
        # evidence, so blanking the cache blanks the whole Device Settings
        # section of a device that is sitting right there working. The re-used
        # node id that motivated eviction (#192) is already handled without this,
        # because _cache_setting_limits REBUILDS a node's entries rather than
        # merging into them. Eviction is left to node_removed, which is an
        # explicit decommission rather than an inference from a list that may
        # just be short.
        for dev_id in orphans:
            self._safe_unreachable(dev_id)
        for nid in orphan_node_ids:
            self._write_node_reachable(nid, False)

    def _apply_reachability(self, node: NodeInfo) -> None:
        """Sync a node's Indigo devices to matter-server's reachability for it."""
        if node.available:
            self._refresh_live_node(node)
        else:
            self.mark_unreachable(node.node_id)
        # The node device's own `reachable` STATE (issue #204) — on top of the
        # errorState marking above, which `mark_unreachable`/`_refresh_live_node`
        # already apply to it like any other device on the node via `_index`.
        # `reachable` is matterNode's UiDisplayStateId, so its live value must
        # track this same evidence directly, both directions, rather than sit
        # at its creation-time True forever the way matterUnknown/
        # matterEnergyMeter's write-once `reachable` currently does.
        #
        # This call is idempotent with `mark_unreachable`'s own write on the
        # False branch above (issue #204 review, fix E) — both land the same
        # value, so the repeat costs nothing. It is also THE ONLY path that
        # ever writes True again: every other caller of `mark_unreachable`/
        # `mark_all_unreachable`/the orphan sweep only has bad news to report
        # and correctly only ever writes False. A reconcile that finds the
        # node available is what un-latches it.
        self._write_node_reachable(node.node_id, bool(node.available))

    def _refresh_live_node(self, node: NodeInfo) -> None:
        # Re-assert any capability props that were missed at creation time (issue #45).
        # Must run BEFORE _prime_states so the states exist when priming fires.
        self._reassert_capability_props(node)
        for endpoint in node.endpoints:
            # Snapshot the type-map once; derive both dev_ids, own_type_id, and
            # ep_sibling_types from it so we only acquire the lock once per endpoint.
            with self._lock:
                ep_key = (int(node.node_id), int(endpoint.endpoint_id))
                type_map = dict(self._index.get(ep_key, {}))
            sibling_types = set(type_map.keys())
            for dev_id in type_map.values():
                own_type_id = next(
                    (tid for tid, did in type_map.items() if did == dev_id), ""
                )
                self._prime_states(
                    node, dev_id, endpoint.endpoint_id, own_type_id,
                    ep_sibling_types=sibling_types,
                )
                self._clear_error(dev_id)

    def mark_unreachable(self, node_id: Any) -> None:
        with self._lock:
            targets = [
                dev_id
                for (nid, _eid), type_map in self._index.items()
                if nid == int(node_id)
                for dev_id in type_map.values()
            ]
        for dev_id in targets:
            self._safe_unreachable(dev_id)
        # errorState above covers every device generically; the node device's
        # OWN `reachable` state (its UiDisplayStateId) needs the same explicit
        # write _apply_reachability makes — otherwise it latches stale True
        # through this path (issue #204 review, fix E: EVT_NODE_REMOVED and
        # other event-driven callers of this method never went through
        # _apply_reachability at all).
        self._write_node_reachable(node_id, False)

    def mark_endpoint_unreachable(self, node_id: Any, endpoint_id: Any) -> None:
        """Mark ALL Indigo devices for a specific (node, endpoint) unreachable.

        Called on endpoint_removed so that every device on the removed bridged
        child (all additive types) is flagged, leaving devices on other endpoints
        untouched.
        """
        for dev_id in self._all_dev_ids_for_endpoint(node_id, endpoint_id):
            self._safe_unreachable(dev_id)

    def mark_all_unreachable(self) -> None:
        """matter-server connection lost (drop / shutdown / sleep) — every Matter
        device is unreachable until we reconnect and reconcile."""
        with self._lock:
            # Deduplicate dev_ids (same dev can't appear twice in the nested map,
            # but be defensive)
            seen: set[int] = set()
            targets: list[int] = []
            node_ids: set[int] = set()
            for (nid, eid), type_map in self._index.items():
                if eid == 0 and "matterNode" in type_map:
                    node_ids.add(nid)
                for dev_id in type_map.values():
                    if dev_id not in seen:
                        seen.add(dev_id)
                        targets.append(dev_id)
        for dev_id in targets:
            self._safe_unreachable(dev_id)
        # A WS drop is the most common outage this plugin sees (issue #204
        # review, fix E) — without this, every matterNode device's own
        # `reachable` state would latch stale True through a disconnect that
        # errorState above already reports on every OTHER device.
        for nid in node_ids:
            self._write_node_reachable(nid, False)

    def _on_endpoint_removed(self, evt: protocol.MatterEvent) -> None:
        """A bridged child endpoint was removed from the bridge.

        Marks ALL of that endpoint's Indigo devices unreachable (never deletes —
        the user may re-pair the device and the history/name should be preserved).
        A malformed frame (missing node_id or endpoint) is logged and dropped.
        """
        if evt.node_id is None or evt.endpoint is None:
            self.logger.warning("ignoring malformed endpoint_removed frame: %r", evt.raw)
            return
        self.mark_endpoint_unreachable(evt.node_id, evt.endpoint)

    def _safe_unreachable(self, dev_id: int) -> None:
        try:
            indigo.devices[dev_id].setErrorStateOnServer("unreachable")
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("could not mark %s unreachable: %s", dev_id, exc)

    def _clear_error(self, dev_id: int) -> None:
        try:
            dev = indigo.devices[dev_id]
            if getattr(dev, "errorState", ""):
                dev.setErrorStateOnServer("")
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("could not clear error on %s: %s", dev_id, exc)

    def _write_node_reachable(self, node_id: Any, reachable: bool) -> None:
        """Write matterNode's own ``reachable`` state directly (issue #204).

        Deliberately NOT routed through :meth:`apply_states`: that helper
        clears any existing ``errorState`` on every call, which for the
        ``reachable=False`` branch would immediately undo the "unreachable"
        marking :meth:`mark_unreachable`/:meth:`_safe_unreachable` just made a
        moment earlier in :meth:`_apply_reachability`. Same direct-call idiom
        as ``_safe_unreachable``/``_clear_error`` for exactly that reason.

        A silent no-op when the node has no ``matterNode`` device yet (most
        nodes, most of the time) — this is not a diagnostic path.

        ``uiValue``-bearing (see ``_reachable_kv``): this is the ONLY path
        that ever writes ``reachable`` again after creation (``_apply_
        reachability``'s docstring), so it is also the only place the
        display column's "yes"/"no" would otherwise never get corrected.
        """
        with self._lock:
            dev_id = self._index.get((int(node_id), 0), {}).get("matterNode")
        if dev_id is None:
            return
        try:
            indigo.devices[dev_id].updateStatesOnServer([_reachable_kv(reachable)])
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(
                "could not write matterNode reachable state for node %s: %s", node_id, exc)

    # ------------------------------------------------------------------
    # Inbound events (asyncio thread) → Indigo state
    # ------------------------------------------------------------------
    def handle_event(self, evt: protocol.MatterEvent) -> None:
        if evt.kind == protocol.EVT_ATTRIBUTE_UPDATED:
            self._on_attribute(evt)
        elif evt.kind == protocol.EVT_NODE_EVENT:
            self._on_node_event(evt)
        elif evt.kind in (protocol.EVT_NODE_ADDED, protocol.EVT_NODE_UPDATED):
            self._on_node_added(evt)
        elif evt.kind == protocol.EVT_NODE_REMOVED and evt.node_id is not None:
            self.mark_unreachable(evt.node_id)
            # The node is decommissioned; its Indigo devices may well outlive it
            # (the user has to delete them). Their cached capability answers must
            # not (issue #192) — see _forget_node_capabilities for why dropping
            # them cannot hide anything.
            self._forget_node_capabilities(evt.node_id)
        elif evt.kind == protocol.EVT_ENDPOINT_ADDED:
            # matter-server ALWAYS fires node_updated (via structureChanged) after
            # endpoint_added — verified against PairedNode.ts #triggerNodeStructureChanges
            # (line 954) in matter.js.  The full node-details object in node_updated
            # lets _on_node_added run create_devices (idempotent) to pick up the new
            # endpoint.  Here we just log at debug so the sequence is visible in the
            # event log, and avoid a redundant create pass with incomplete data.
            if evt.node_id is not None and evt.endpoint is not None:
                self.logger.debug(
                    "endpoint_added: node %s endpoint %s — awaiting node_updated",
                    evt.node_id, evt.endpoint,
                )
            else:
                self.logger.warning("ignoring malformed endpoint_added frame: %r", evt.raw)
        elif evt.kind == protocol.EVT_ENDPOINT_REMOVED:
            self._on_endpoint_removed(evt)
        elif evt.kind == protocol.EVT_SERVER_SHUTDOWN:
            # matter-server announces shutdown just before closing the socket;
            # mark everything unreachable now rather than wait for the drop.
            self.logger.warning("matter-server announced shutdown; marking all Matter devices unreachable")
            self.mark_all_unreachable()

    def _on_node_added(self, evt: protocol.MatterEvent) -> None:
        """A node commissioned/updated out-of-band (e.g. via the dashboard).

        node_added/node_updated carry the full node-details object; create any
        missing Indigo devices (idempotent — existing endpoints are skipped).
        matter-server also fires node_updated whenever a node's availability
        changes, so reflect reachability here too.
        """
        data = evt.raw.get("data") if evt.raw else None
        if not isinstance(data, dict):
            return
        try:
            node = parse_node(data)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("node_added parse failed: %s", exc)
            return
        self.create_devices(node)
        self._apply_reachability(node)

    def _on_node_event(self, evt: protocol.MatterEvent) -> None:
        """A cluster event arrived (button press, lock operation, etc.).

        Routes to the handler's ``on_node_event`` hook using the same device
        lookup and _active gate semantics as the non-node-scoped attribute path.
        A malformed frame (missing node_id / endpoint / cluster) is logged and
        dropped rather than silently ignored, matching the attribute path idiom.

        Ordering mirrors _on_attribute's non-node-scoped path: handler lookup
        (cheap early-exit) first, then device lookup, then _active gate.
        """
        if evt.node_id is None or evt.endpoint is None or evt.cluster is None:
            self.logger.warning("ignoring malformed node_event frame: %r", evt.raw)
            return
        handler = self.registry.handler_for_cluster(evt.cluster)
        if handler is None:
            return
        # Use _lookup_for_cluster so events on a multi-device endpoint reach
        # the correct device (e.g. a cluster event on the AQ cluster goes to
        # the AirQuality device, not the CO2 device).
        dev_id = self._lookup_for_cluster(evt.node_id, evt.endpoint, evt.cluster)
        if dev_id is None:
            return
        if self._active and dev_id not in self._active:
            return  # gate updates to active devices once any are started
        try:
            dev = indigo.devices[dev_id]
        except KeyError:
            # Deletion race, same as _on_attribute's paths (#84) — and this is
            # the chattier route (switch presses, lock operations), where the
            # unguarded lookup used to put a full traceback in the event log
            # per event via plugin's _on_matter_event handler.
            self.logger.debug(
                "device %s vanished mid-event (deleted?); dropped ep%s cl%s evt%s",
                dev_id, evt.endpoint, evt.cluster, evt.event_id,
            )
            return
        try:
            states = handler.on_node_event(dev, evt.event_id, evt.event_data)
        except Exception as exc:  # noqa: BLE001 - one bad event must not silently freeze the device
            self.logger.warning(
                "bad node_event for device %s (ep%s cl%s evt%s): %s",
                dev_id, evt.endpoint, evt.cluster, evt.event_id, exc,
            )
            return
        if states:
            self.apply_states(dev_id, handler.format_kv(states))

    def _on_attribute(self, evt: protocol.MatterEvent) -> None:
        if evt.node_id is None or evt.endpoint is None or evt.cluster is None:
            # a malformed attribute_updated frame (e.g. a truncated "ep/cl/at"
            # path) — surface it rather than dropping silently; protocol.py is the
            # rename firewall and a wire-shape change should be visible here.
            self.logger.warning("ignoring malformed attribute event: %r", evt.raw)
            return
        # BridgedDeviceBasicInformation (0x0039) Reachable (0x0011): per-endpoint
        # liveness.  Handle BEFORE handler dispatch because device_sync owns
        # reachability state; handlers return state dicts and must not set error
        # states directly.  Constants live in matter_model (same home as 0x0028).
        if evt.cluster == CLUSTER_BRIDGED_BASIC and evt.attribute == BBRIDGE_ATTR_REACHABLE:
            # Mark/clear ALL devices on this endpoint (handles additive endpoints)
            dev_ids = self._all_dev_ids_for_endpoint(evt.node_id, evt.endpoint)
            if not dev_ids:
                return
            if evt.value is None:
                pass  # unknown is not offline — do nothing
            elif evt.value:
                for dev_id in dev_ids:
                    self._clear_error(dev_id)
            else:
                for dev_id in dev_ids:
                    self._safe_unreachable(dev_id)
            return
        # Handler lookup must precede endpoint lookup: node-scoped handlers (e.g.
        # PowerSource) have no Indigo device at the event's endpoint and require
        # special fan-out treatment before any per-endpoint device resolution.
        handler = self.registry.handler_for_cluster(evt.cluster)
        if handler is None:
            return
        if handler.node_scoped:
            # Node-scoped clusters (e.g. PowerSource) live on a different endpoint
            # than the devices they augment, so the update goes to the endpoints
            # this source says it powers (issue #205 — its own EndpointList,
            # cached as coverage). Fanning out node-wide unconditionally is what
            # cross-contaminated a bridge's battery children in issue #82. No
            # cached authority (targets is None — e.g. an event arriving before
            # the first create pass has described the node) keeps the pre-#205
            # node-wide fan-out.
            targets = self._battery_targets(evt.node_id, int(evt.endpoint))
            with self._lock:
                if targets is None:
                    dev_ids = [
                        dev_id
                        for (nid, _eid), type_map in self._index.items()
                        if nid == int(evt.node_id)
                        for dev_id in type_map.values()
                    ]
                else:
                    dev_ids = [
                        dev_id
                        for (nid, eid), type_map in self._index.items()
                        if nid == int(evt.node_id) and eid in targets
                        for dev_id in type_map.values()
                    ]
            for dev_id in dev_ids:
                if self._active and dev_id not in self._active:
                    continue  # gate updates to active devices once any are started
                try:
                    dev = indigo.devices[dev_id]
                except KeyError:
                    # Deletion race (see the non-node-scoped path below) — debug,
                    # not the "bad update" warning, and keep fanning out to the
                    # rest of this source's devices (already narrowed to its
                    # coverage above).
                    self.logger.debug(
                        "device %s vanished mid-update (deleted?); dropped ep%s cl%s attr%s",
                        dev_id, evt.endpoint, evt.cluster, evt.attribute,
                    )
                    continue
                try:
                    states = handler.on_attribute_update(dev, evt.attribute, evt.value)
                    if states:
                        self.apply_states(dev_id, handler.format_kv(states))
                except Exception as exc:  # noqa: BLE001 - one bad value must not silently freeze the device
                    self.logger.warning(
                        "bad update for device %s (ep%s cl%s attr%s value=%r): %s",
                        dev_id, evt.endpoint, evt.cluster, evt.attribute, evt.value, exc,
                    )
                    continue
            return
        # Non-node-scoped path: use _lookup_for_cluster to route to the correct
        # device when multiple additive devices share an endpoint.
        dev_id = self._lookup_for_cluster(evt.node_id, evt.endpoint, evt.cluster)
        if dev_id is None:
            return
        if self._active and dev_id not in self._active:
            return  # gate updates to active devices once any are started
        try:
            dev = indigo.devices[dev_id]
        except KeyError:
            # The Indigo device was deleted out-of-band while its Matter node
            # kept reporting — routine deletion race, not a bad value; same
            # tolerate/debug/move-on idiom as _safe_unreachable/_clear_error.
            self.logger.debug(
                "device %s vanished mid-update (deleted?); dropped ep%s cl%s attr%s",
                dev_id, evt.endpoint, evt.cluster, evt.attribute,
            )
            return
        try:
            states = handler.on_attribute_update(dev, evt.attribute, evt.value)
        except Exception as exc:  # noqa: BLE001 - one bad value must not silently freeze the device
            self.logger.warning(
                "bad update for device %s (ep%s cl%s attr%s value=%r): %s",
                dev_id, evt.endpoint, evt.cluster, evt.attribute, evt.value, exc,
            )
            return
        if states:
            self.apply_states(dev_id, handler.format_kv(states))

    def apply_states(self, dev_id: int, kvlist: list) -> None:
        """The single asyncio→Indigo write seam (see module docstring)."""
        try:
            dev = indigo.devices[dev_id]
        except KeyError:
            # Deletion race — the device vanished between the caller's own
            # lookup and this write. Tolerate/debug/move-on, same idiom as
            # _safe_unreachable/_clear_error. updateStatesOnServer failures are
            # deliberately NOT caught here — this only guards the dict lookup.
            self.logger.debug(
                "device %s vanished before state apply (deleted?); dropped %d state(s)",
                dev_id, len(kvlist),
            )
            return
        dev.updateStatesOnServer(kvlist)
        # a fresh value means the device is reachable — clear any stale error
        if getattr(dev, "errorState", ""):
            dev.setErrorStateOnServer("")

    # ------------------------------------------------------------------
    # Indigo action → Matter command (B1 path)
    # ------------------------------------------------------------------
    def build_command(self, dev: Any, action: Any) -> Optional[MatterCommand]:
        handler = self.registry.handler_for_device(dev)
        if handler is None:
            return None
        return handler.handle_indigo_action(dev, action)
