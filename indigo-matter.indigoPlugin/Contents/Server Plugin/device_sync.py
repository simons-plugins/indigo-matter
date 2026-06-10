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
from matter_handlers.power_source import CLUSTER_POWER_SOURCE
from protocol import MatterCommand


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
                for spec in self.registry.handlers_for_endpoint(node, endpoint):
                    if node_has_power_source:
                        spec.props.setdefault("SupportsBatteryLevel", True)
                    plan.append((endpoint, spec))

            multi = len(plan) > 1
            folder_id = self._resolve_folder_id(suggested_room) if authoritative else 0
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
                self._index.setdefault(ep_key, {})[type_id] = dev_id
                created.append(dev_id)
                new_ids.append(dev_id)
                primary = primary if primary is not None else dev_id
                self._prime_states(node, dev_id, endpoint.endpoint_id, type_id)
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

    def _create_one(self, spec: Any, name: str, folder_id: int = 0) -> Optional[int]:
        try:
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId=spec.device_type_id,
                name=self._unique_name(name),
                props=spec.props,
                folder=folder_id,
            )
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
                      own_type_id: str) -> None:
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
        """
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
            # We skip unconditionally — whether or not the sibling has been created
            # yet — so creation order does not affect priming correctness.
            if ep == endpoint_id and not handler.node_scoped:
                handler_type_id = getattr(handler, "device_type_id", "") or ""
                if handler_type_id and handler_type_id != own_type_id:
                    continue
            try:
                states.update(handler.on_attribute_update(dev, attribute, value))
            except Exception as exc:  # noqa: BLE001 - one bad attr must not abort priming
                self.logger.warning("prime %s attr %s/%s failed: %s", dev_id, cluster, attribute, exc)
        if states:
            self.apply_states(dev_id, _kvlist(states))

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
        for endpoint in node.endpoints:
            # Refresh ALL devices on this endpoint (handles additive multi-device eps)
            for dev_id in self._all_dev_ids_for_endpoint(node.node_id, endpoint.endpoint_id):
                type_map = {}
                with self._lock:
                    ep_key = (int(node.node_id), int(endpoint.endpoint_id))
                    type_map = dict(self._index.get(ep_key, {}))
                own_type_id = next(
                    (tid for tid, did in type_map.items() if did == dev_id), ""
                )
                self._prime_states(node, dev_id, endpoint.endpoint_id, own_type_id)
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
