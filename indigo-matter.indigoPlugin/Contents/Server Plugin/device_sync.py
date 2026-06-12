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
from matter_handlers.electrical import CLUSTER_ELECTRICAL_ENERGY, CLUSTER_ELECTRICAL_POWER
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
})


def _kvlist(states: dict) -> list:
    return [{"key": key, "value": value} for key, value in states.items()]


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

        1. Resolve the handler's ``device_type_id`` (if any).
        2. If that type is present in the endpoint's type-map → return it.
        3. Else fall back to ``lookup(node, ep)`` (single/first device) —
           covers merge-into cases: FanControl→thermostat, Electrical→relay.
        """
        handler = self.registry.handler_for_cluster(cluster)
        if handler is None:
            return None
        type_id = getattr(handler, "device_type_id", "") or ""
        if type_id:
            with self._lock:
                type_map = self._index.get((int(node_id), int(endpoint_id)))
                if type_map and type_id in type_map:
                    return type_map[type_id]
        # Fallback: use the single/first device on the endpoint
        return self.lookup(node_id, endpoint_id)

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

    def delete_node(self, node_id: Any) -> list:
        """Delete all Indigo devices for a node; return the ids actually deleted.

        Ids whose Indigo delete fails are NOT included in the returned list, so
        the decommission response never claims a device was removed when it
        wasn't.
        """
        target = int(node_id)
        with self._lock:
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

    def node_count(self) -> int:
        with self._lock:
            return len({nid for (nid, _eid) in self._index})

    def list_nodes(self) -> list:
        """Per-node summary for UI pickers: ``[(node_id, [device names])]``.

        Sorted by node id; device names resolved outside the lock so a slow
        Indigo lookup can't stall state/command dispatch.
        """
        with self._lock:
            by_node: dict[int, set] = {}
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
            # Plan over every mappable endpoint (existing or not) so the
            # "(endpoint N)" suffix is decided by the node's true device count,
            # not by how many happen to be missing on this pass. A plug's root
            # endpoint 0 produces no handler, so this is not len(node.endpoints).
            plan: list[tuple] = []  # (endpoint, spec)
            # Detect PowerSource anywhere on the node so we can set
            # SupportsBatteryLevel on every device we create. Indigo applies
            # Supports* via device props at creation, not Devices.xml statics
            # (the colour-support lesson — see HANDOVER 2026-06-09 item 4).
            node_has_power_source = any(
                endpoint.has(CLUSTER_POWER_SOURCE) for endpoint in node.endpoints
            )
            for endpoint in node.endpoints:
                specs = self.registry.handlers_for_endpoint(node, endpoint)
                if not specs:
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
                elif self._index.get(
                    (int(node.node_id), int(endpoint.endpoint_id)), {}
                ).get("matterUnknown"):
                    # The endpoint used to be unsupported (placeholder exists)
                    # but now maps to real device(s) — e.g. a firmware update
                    # added clusters. Never auto-delete a user's device; tell
                    # them the placeholder is obsolete instead.
                    self.logger.info(
                        "node %s endpoint %s is now supported — the 'Matter Device "
                        "(unsupported clusters)' placeholder device can be deleted",
                        node_id_to_str(node.node_id), endpoint.endpoint_id,
                    )
                for spec in specs:
                    if node_has_power_source:
                        spec.props.setdefault("SupportsBatteryLevel", True)
                    plan.append((endpoint, spec))

            multi = len(plan) > 1
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
                dev_id = self._create_one(spec, name, folder_id)
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
        return IndigoDeviceSpec(
            device_type_id="matterUnknown",
            name=name,
            props={
                "nodeId": str(node.node_id),
                "endpointId": str(endpoint.endpoint_id),
                "vendorName": node.vendor_name,
                "productName": node.product_name,
                "supportedClusters": ", ".join(f"0x{c:04X}" for c in unmapped),
            },
            initial_states={"reachable": True},
        )

    def _create_one(self, spec: Any, name: str, folder_id: int = 0) -> Optional[int]:
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
        endpoint 0 primes battery level into a sensor on endpoint 1).

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
        states: dict = {}
        for (ep, cluster, attribute), value in node.attributes.items():
            handler = self.registry.handler_for_cluster(cluster)
            if handler is None:
                continue
            # Include attributes from: this device's own endpoint, OR any
            # node-scoped cluster living on a different endpoint.
            if ep != endpoint_id and not handler.node_scoped:
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
                states.update(handler.on_attribute_update(dev, attribute, value))
            except Exception as exc:  # noqa: BLE001 - one bad attr must not abort priming
                self.logger.warning("prime %s attr %s/%s failed: %s", dev_id, cluster, attribute, exc)
        if states:
            self.apply_states(dev_id, _kvlist(states))

    # ------------------------------------------------------------------
    # Capability-prop helpers (issue #45 — self-heal mid-interview creations)
    # ------------------------------------------------------------------

    @staticmethod
    def _capability_props(node: NodeInfo, endpoint: Any) -> dict:
        """Return capability props implied by the node's CURRENT cluster set.

        These props unlock Indigo states that handlers write into:
        - SupportsPowerMeter   → curEnergyLevel   (cluster 0x0090 on the endpoint)
        - SupportsEnergyMeter  → accumEnergyTotal  (cluster 0x0091 on the endpoint)
        - SupportsBatteryLevel → batteryLevel      (cluster 0x002F anywhere on the node)

        The cluster constants are imported from their handler modules — no magic
        numbers here.  The battery check fans across ALL node endpoints, mirroring
        the create_devices central setdefault that was the original source of truth.
        """
        props: dict = {}
        if endpoint.has(CLUSTER_ELECTRICAL_POWER):
            props["SupportsPowerMeter"] = True
        if endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
            props["SupportsEnergyMeter"] = True
        node_has_power_source = any(
            ep.has(CLUSTER_POWER_SOURCE) for ep in node.endpoints
        )
        if node_has_power_source:
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
        for raw in raw_nodes:
            # one malformed node must not sink reconciliation for the rest
            try:
                node = parse_node(raw)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("skipping unparseable Matter node: %s", exc)
                continue
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
            orphans = [
                dev_id
                for (node_id, _ep), type_map in self._index.items()
                if (node_id, _ep) not in live
                for dev_id in type_map.values()
            ]
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
        dev = indigo.devices[dev_id]
        try:
            states = handler.on_node_event(dev, evt.event_id, evt.event_data)
        except Exception as exc:  # noqa: BLE001 - one bad event must not silently freeze the device
            self.logger.warning(
                "bad node_event for device %s (ep%s cl%s evt%s): %s",
                dev_id, evt.endpoint, evt.cluster, evt.event_id, exc,
            )
            return
        if states:
            self.apply_states(dev_id, _kvlist(states))

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
            # for this node so every sensor on the node receives the battery update.
            with self._lock:
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
                    states = handler.on_attribute_update(dev, evt.attribute, evt.value)
                    if states:
                        self.apply_states(dev_id, _kvlist(states))
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
        dev = indigo.devices[dev_id]
        try:
            states = handler.on_attribute_update(dev, evt.attribute, evt.value)
        except Exception as exc:  # noqa: BLE001 - one bad value must not silently freeze the device
            self.logger.warning(
                "bad update for device %s (ep%s cl%s attr%s value=%r): %s",
                dev_id, evt.endpoint, evt.cluster, evt.attribute, evt.value, exc,
            )
            return
        if states:
            self.apply_states(dev_id, _kvlist(states))

    def apply_states(self, dev_id: int, kvlist: list) -> None:
        """The single asyncio→Indigo write seam (see module docstring)."""
        dev = indigo.devices[dev_id]
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
