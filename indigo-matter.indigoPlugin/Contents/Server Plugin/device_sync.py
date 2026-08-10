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

import threading
from typing import Any, Optional

import indigo

import protocol
from matter_model import (
    BBRIDGE_ATTR_REACHABLE,
    CLUSTER_BRIDGED_BASIC,
    NodeInfo,
    node_id_to_str,
    parse_node,
)
from matter_handlers.base import IndigoDeviceSpec
from matter_handlers.boolean_state_config import (
    ATTR_SUPPORTED_SENSITIVITY_LEVELS,
    CLUSTER_BOOLEAN_STATE_CONFIG,
)
from matter_handlers.settings import ATTR_ATTRIBUTE_LIST, SETTINGS
from matter_handlers.electrical import (
    ATTR_ACTIVE_ENDPOINTS,
    ATTR_AVAILABLE_ENDPOINTS,
    ATTR_POWER_TOPOLOGY_FEATURE_MAP,
    CLUSTER_ELECTRICAL_ENERGY,
    CLUSTER_ELECTRICAL_POWER,
    CLUSTER_POWER_TOPOLOGY,
    FEATURE_DYNAMIC_POWER_FLOW,
    FEATURE_SET_TOPOLOGY,
)
from matter_handlers.power_source import CLUSTER_POWER_SOURCE
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


#: Friendly role suffix per Indigo device type, used to name the individual
#: devices of a multi-function node.  A HomePod exposing temperature + humidity
#: becomes "<name> - Temperature" / "<name> - Humidity" rather than the opaque
#: "<name> (endpoint 1)".  Endpoint-number naming is kept only as the fallback
#: for a type absent from this map, and to disambiguate genuinely identical
#: siblings (e.g. four outlets on one strip — see create_devices).
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


def _kvlist(states: dict) -> list:
    return [{"key": key, "value": value} for key, value in states.items()]


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
        # PowerSource-bearing endpoints per node (issue #82 — bridge battery
        # cross-contamination). Populated by create_devices/_resolve_meter_links-
        # adjacent bookkeeping on every create pass; consulted by creation,
        # priming, and live fan-out to decide whether PowerSource is node-wide
        # (the common single-endpoint case, e.g. FP300: battery on ep0, sensor
        # on ep1) or must stay confined to its own endpoint (a bridge with
        # more than one battery-bearing child).
        self._power_source_eps: dict[int, set[int]] = {}
        # The raw LIMITS attribute behind every declared writable setting
        # (matter_handlers.settings.SETTINGS), keyed
        # (node_id, endpoint_id, cluster, attribute) — issues #85 and #186.
        # NodeInfo snapshots are transient (this class holds no other
        # node-attribute cache), so the ConfigUI layer in plugin.py needs these
        # captured somewhere durable; refreshed on every create/reconcile pass.
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
        # setting may be offered (issue #186). Needed because most settings take
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
            # Collect all dev_ids across all endpoints for this node
            candidates: list[int] = []
            for (nid, _eid), type_map in self._index.items():
                if nid == target:
                    candidates.extend(type_map.values())
            deleted = []
            for dev_id in candidates:
                try:
                    indigo.device.delete(indigo.devices[dev_id])
                    deleted.append(dev_id)
                except Exception as exc:  # noqa: BLE001
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
            # Detect which endpoint(s) carry PowerSource. Indigo applies
            # Supports* via device props at creation, not Devices.xml statics
            # (the colour-support lesson; issue #56).
            # Single PowerSource-bearing endpoint (the common case — e.g. FP300:
            # battery on ep0, sensor on ep1) keeps the original node-wide
            # behaviour: SupportsBatteryLevel on every device regardless of its
            # own endpoint. More than one PowerSource-bearing endpoint (a
            # bridge with multiple battery-powered children) must NOT fan out
            # node-wide — issue #82's cross-contamination bug — so each
            # device only gets the prop when ITS OWN endpoint bears PowerSource.
            power_source_eps = {
                int(endpoint.endpoint_id) for endpoint in node.endpoints
                if endpoint.has(CLUSTER_POWER_SOURCE)
            }
            multi_power_source = len(power_source_eps) > 1
            with self._lock:
                if power_source_eps:
                    self._power_source_eps[int(node.node_id)] = power_source_eps
                else:
                    self._power_source_eps.pop(int(node.node_id), None)
            self._cache_setting_limits(node)
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
                        # matterUnknown placeholder (issue #79).
                        if ep_key not in self._forward_links:
                            specs = [self._energy_meter_spec(node, endpoint)]
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
                    # Single-PowerSource-endpoint node: fan out to every
                    # device as before. Multi-PowerSource-endpoint node
                    # (issue #82): only the device(s) on the SAME endpoint as
                    # a PowerSource cluster get the prop.
                    if power_source_eps and (not multi_power_source or eid in power_source_eps):
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
                            if src_endpoint.has(CLUSTER_ELECTRICAL_POWER):
                                spec.props.setdefault("SupportsPowerMeter", True)
                            if src_endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
                                spec.props.setdefault("SupportsEnergyMeter", True)
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
            # Pre-compute the FULL set of planned device_type_ids per endpoint so
            # _prime_states can identify merge-into handlers correctly regardless
            # of creation order.  (The index is built incrementally, so checking
            # it inside the loop would miss not-yet-created siblings.)
            ep_planned_types: dict[tuple, set] = {}
            for ep_, spec_ in plan:
                key_ = (int(node.node_id), int(ep_.endpoint_id))
                ep_planned_types.setdefault(key_, set()).add(spec_.device_type_id)
            for endpoint, spec in plan:
                # Bridged endpoints carry their own identity in cluster 0x0039.
                # When a bridge label (node_label or product_name) is present:
                #   - Non-authoritative pass (node_added / reconcile): use the
                #     bridge label as the device name; skip "(endpoint N)" suffix.
                #   - Authoritative pass (user-chosen name in suggested_name):
                #     use spec.name (already encodes suggested_name); still skip
                #     the suffix — bridged children have unique identities.
                # For non-bridged endpoints the suffix is applied for multi-endpoint
                # nodes exactly as before.
                bridge_label = endpoint.node_label or endpoint.product_name
                if bridge_label:
                    # TODO(#43): on an authoritative pass, spec.name (the user's
                    # chosen bridge name) overwrites each child's own NodeLabel,
                    # producing "My Hue Bridge", "My Hue Bridge 2", … and wiping
                    # per-bulb names the user set in the bridge app.  Issue #43
                    # tracks the fix (prefix or bridge-only authoritative stamp).
                    name = spec.name if authoritative else bridge_label
                elif multi:
                    # Name by the device's function ("- Temperature"), not its
                    # Matter endpoint number, which is meaningless to the user.
                    # Fall back to the endpoint suffix only for identical
                    # siblings or an unmapped type.
                    role = _ROLE_LABELS.get(spec.device_type_id, "")
                    if role and role_counts.get(role, 0) > 1:
                        name = f"{spec.name} - {role} {endpoint.endpoint_id}"
                    elif role:
                        name = f"{spec.name} - {role}"
                    else:
                        name = f"{spec.name} (endpoint {endpoint.endpoint_id})"
                else:
                    name = spec.name
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
        result = {
            "indigoDeviceIds": created,
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
            props={
                "nodeId": str(node.node_id),
                "endpointId": str(endpoint.endpoint_id),
                "vendorName": node.vendor_name,
                "productName": node.product_name,
                "supportedClusters": supported,
            },
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
            props={
                "nodeId": str(node.node_id),
                "endpointId": str(endpoint.endpoint_id),
                "vendorName": node.vendor_name,
                "productName": node.product_name,
            },
            initial_states={"curEnergyLevel": 0.0, "accumEnergyTotal": 0.0, "reachable": True},
        )

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
        """
        if int(endpoint.endpoint_id) == 0 or specs:
            return False
        if not (endpoint.has(CLUSTER_ELECTRICAL_POWER) or endpoint.has(CLUSTER_ELECTRICAL_ENERGY)):
            return False
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

    def _move_to_folder(self, dev_id: int, folder_id: int) -> None:
        try:
            try:
                indigo.device.moveToFolder(dev_id, value=folder_id)
            except TypeError:  # older positional signature
                indigo.device.moveToFolder(dev_id, folder_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("could not move device %s to folder %s: %s", dev_id, folder_id, exc)

    def _apply_identity(self, dev_id: int, name: str, folder_id: int) -> None:
        """Stamp a commission-chosen name/folder onto an already-created device.

        Used when node_added raced ahead and created the device with the bare
        product name before the commission job (which carries the user's choices)
        ran. Idempotent: a device that already matches is left untouched.
        """
        try:
            dev = indigo.devices[dev_id]
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("apply_identity: device %s unavailable: %s", dev_id, exc)
            return
        try:
            target = name if dev.name == name else self._unique_name(name)
            if dev.name != target:
                dev.name = target
                dev.replaceOnServer()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("could not rename device %s to %r: %s", dev_id, name, exc)
        if folder_id and getattr(dev, "folderId", 0) != folder_id:
            self._move_to_folder(dev_id, folder_id)

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

        The node-scoped cross-endpoint fan-in is itself confined to the
        device's own endpoint when the node has MORE THAN ONE PowerSource-
        bearing endpoint (issue #82 — a bridge with several battery-powered
        children): otherwise endpoint 2's battery reading would prime
        endpoint 1's device too. A node with zero or one PowerSource endpoint
        keeps the original any-endpoint behaviour (the common single-battery
        case, e.g. FP300).

        Within the own endpoint, skip attributes whose cluster's handler targets
        a *different* existing device on the same endpoint (fix/#44: prevents the
        Pressure device being primed with Flow values or an AQ device being primed
        with CO2 values it doesn't own).

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
            multi_power_source = len(self._power_source_eps.get(int(node.node_id), ())) > 1
        kv: list = []
        for (ep, cluster, attribute), value in node.attributes.items():
            handler = self.registry.handler_for_cluster(cluster)
            if handler is None:
                continue
            # Include attributes from: this device's own endpoint, any
            # node-scoped cluster living on a different endpoint, or a linked
            # meter-source endpoint (issue #79 — split-endpoint energy).
            if ep != endpoint_id and not handler.node_scoped and ep not in linked_sources:
                continue
            # Issue #82: a node-scoped cluster (PowerSource) on a DIFFERENT
            # endpoint only fans in when the node has at most one
            # PowerSource-bearing endpoint — a bridge with several
            # battery-powered children must not cross-contaminate.
            if ep != endpoint_id and handler.node_scoped and multi_power_source:
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

    def _cache_setting_limits(self, node: NodeInfo) -> None:
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
        """
        wanted = set()
        for setting in SETTINGS:
            attribute = getattr(setting.bounds, "attribute", None)
            if attribute is not None:
                wanted.add((setting.cluster, int(attribute)))
        clusters = {s.cluster for s in SETTINGS}
        with self._lock:
            for (ep, cluster, attribute), value in node.attributes.items():
                if (cluster, attribute) in wanted:
                    self._setting_limits[
                        (int(node.node_id), int(ep), int(cluster), int(attribute))] = value
                elif attribute == ATTR_ATTRIBUTE_LIST and cluster in clusters:
                    self._attribute_lists[
                        (int(node.node_id), int(ep), int(cluster))] = value

    def setting_limits(self, node_id: Any, endpoint_id: Any, cluster: Any,
                       attribute: Any) -> Any:
        """Raw limits value for a setting on (node, endpoint), or None if
        unknown — not yet reconciled, or this node does not implement it.

        None is load-bearing: the ConfigUI layer treats "no limits" as "do not
        offer this setting", which is what stops a pre-1.4 occupancy sensor with
        no HoldTime from being shown a hold-time field it would fail to honour.
        """
        with self._lock:
            return self._setting_limits.get(
                (int(node_id), int(endpoint_id), int(cluster), int(attribute)))

    def attribute_list(self, node_id: Any, endpoint_id: Any, cluster: Any) -> Any:
        """The cluster's AttributeList on (node, endpoint), or None if unknown.

        None means "not captured", NOT "implements nothing" — callers must not
        read it as proof a device lacks an attribute (see settings.implements).
        """
        with self._lock:
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
        - SupportsBatteryLevel → batteryLevel      (cluster 0x002F anywhere on the node,
                                                     confined to its own endpoint when
                                                     the node has more than one
                                                     PowerSource-bearing endpoint)

        The cluster constants are imported from their handler modules — no magic
        numbers here.  The battery check mirrors create_devices' central setdefault
        (issue #82): a single PowerSource-bearing endpoint still fans out to every
        device on the node, but more than one (a bridge with several
        battery-powered children) confines the prop to each device's own endpoint
        so siblings don't cross-contaminate.

        No longer a ``@staticmethod``: split-endpoint energy (issue #79 — e.g.
        IKEA GRILLPLATS) needs the instance's meter-link map to fold a linked
        source endpoint's electrical clusters into the TARGET endpoint's props,
        so the reconcile self-heal (below) can add meter props to a relay
        whose energy actually lives on a sibling endpoint.
        """
        props: dict = {}
        if endpoint.has(CLUSTER_ELECTRICAL_POWER):
            props["SupportsPowerMeter"] = True
        if endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
            props["SupportsEnergyMeter"] = True
        with self._lock:
            linked_sources = self._reverse_links.get(
                (int(node.node_id), int(endpoint.endpoint_id)), set()
            )
        for src_eid in linked_sources:
            src_endpoint = self._endpoint_by_id(node, src_eid)
            if src_endpoint is None:
                continue
            if src_endpoint.has(CLUSTER_ELECTRICAL_POWER):
                props["SupportsPowerMeter"] = True
            if src_endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
                props["SupportsEnergyMeter"] = True
        with self._lock:
            power_source_eps = self._power_source_eps.get(int(node.node_id), set())
        if power_source_eps and (
            len(power_source_eps) == 1 or int(endpoint.endpoint_id) in power_source_eps
        ):
            props["SupportsBatteryLevel"] = True
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
                        if not current_props.get(key):
                            missing[key] = value
                    for key, value in display_props.items():
                        # Absence is divergence too: a missing SupportsOnState
                        # means Indigo's sensor default (True), so a False must
                        # be written explicitly, not skipped as already-falsy.
                        if key not in current_props or bool(current_props.get(key)) != bool(value):
                            missing[key] = value
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
                        if key not in applied or bool(applied.get(key)) != bool(value)
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
        for dev_id in orphans:
            self._safe_unreachable(dev_id)

    def _apply_reachability(self, node: NodeInfo) -> None:
        """Sync a node's Indigo devices to matter-server's reachability for it."""
        if node.available:
            self._refresh_live_node(node)
        else:
            self.mark_unreachable(node.node_id)

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
            for type_map in self._index.values():
                for dev_id in type_map.values():
                    if dev_id not in seen:
                        seen.add(dev_id)
                        targets.append(dev_id)
        for dev_id in targets:
            self._safe_unreachable(dev_id)

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
            # than the devices they augment. Fan the update out to ALL Indigo devices
            # for this node so every sensor on the node receives the battery update —
            # UNLESS the node has more than one PowerSource-bearing endpoint (issue
            # #82 — a bridge with several battery-powered children), in which case
            # fanning out node-wide would cross-contaminate siblings; confine the
            # update to devices on the event's own endpoint instead.
            with self._lock:
                multi_power_source = len(self._power_source_eps.get(int(evt.node_id), ())) > 1
                if multi_power_source:
                    dev_ids = [
                        dev_id
                        for (nid, eid), type_map in self._index.items()
                        if nid == int(evt.node_id) and eid == int(evt.endpoint)
                        for dev_id in type_map.values()
                    ]
                else:
                    dev_ids = [
                        dev_id
                        for (nid, _eid), type_map in self._index.items()
                        if nid == int(evt.node_id)
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
                    # rest of this node's devices (endpoint-narrowed only in the
                    # multi_power_source branch).
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
