"""Reconcile matter-server nodes ↔ Indigo devices, and apply state updates.

The authoritative ``(nodeId, endpointId) → indigoDeviceId`` map is derived from
each Indigo device's ``pluginProps`` (the single source of truth that survives
plugin reloads); an in-memory index caches it. The index is read/written from
both the asyncio loop (reconcile, attribute events) and Indigo threads
(``deviceStartComm`` → ``note_device``), so every access is guarded by
``self._lock`` (a re-entrant lock, since the index methods call one another).

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
from matter_model import NodeInfo, parse_node
from protocol import MatterCommand


def _kvlist(states: dict) -> list:
    return [{"key": key, "value": value} for key, value in states.items()]


class DeviceSync:
    def __init__(self, registry: Any, logger: Any) -> None:
        self.registry = registry
        self.logger = logger
        self._index: dict[tuple[int, int], int] = {}
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
                    self._index[(int(node_id), int(endpoint_id))] = dev.id

    def lookup(self, node_id: Any, endpoint_id: Any) -> Optional[int]:
        with self._lock:
            return self._index.get((int(node_id), int(endpoint_id)))

    def note_device(self, dev: Any) -> None:
        """Index a single device from its pluginProps (deviceStartComm)."""
        props = dev.pluginProps
        node_id = props.get("nodeId")
        endpoint_id = props.get("endpointId")
        if self._prop_present(node_id) and self._prop_present(endpoint_id):
            with self._lock:
                self._index[(int(node_id), int(endpoint_id))] = dev.id

    def delete_node(self, node_id: Any) -> list:
        """Delete all Indigo devices for a node; return the ids actually deleted.

        Ids whose Indigo delete fails are NOT included in the returned list, so
        the decommission response never claims a device was removed when it
        wasn't.
        """
        target = int(node_id)
        with self._lock:
            candidates = [dev_id for (nid, _eid), dev_id in self._index.items() if nid == target]
            deleted = []
            for dev_id in candidates:
                try:
                    indigo.device.delete(indigo.devices[dev_id])
                    deleted.append(dev_id)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("could not delete Indigo device %s: %s", dev_id, exc)
            # only drop successfully-deleted devices from the index
            self._index = {
                key: val for key, val in self._index.items()
                if not (key[0] == target and val in deleted)
            }
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
            for endpoint in node.endpoints:
                for spec in self.registry.handlers_for_endpoint(node, endpoint):
                    plan.append((endpoint, spec))

            multi = len(plan) > 1
            folder_id = self._resolve_folder_id(suggested_room) if authoritative else 0
            for endpoint, spec in plan:
                name = f"{spec.name} (endpoint {endpoint.endpoint_id})" if multi else spec.name
                key = (int(node.node_id), int(endpoint.endpoint_id))
                existing = self._index.get(key)
                if existing is not None:
                    # Already created (e.g. node_added won the race). On the
                    # authoritative commission pass, stamp the chosen name/room.
                    if authoritative:
                        self._apply_identity(existing, name, folder_id)
                    created.append(existing)
                    primary = primary if primary is not None else existing
                    continue
                dev_id = self._create_one(spec, name, folder_id)
                if dev_id is None:
                    failed += 1
                    continue
                self._index[key] = dev_id
                created.append(dev_id)
                primary = primary if primary is not None else dev_id
                self._prime_states(node, dev_id, endpoint.endpoint_id)
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

    def _prime_states(self, node: NodeInfo, dev_id: int, endpoint_id: int) -> None:
        """Apply the node's current attribute values to a freshly-created device.

        get_node carries a snapshot of every attribute; matter-server only emits
        attribute_updated on subsequent *changes*, so without this a device whose
        value is static at connect time would sit at its hardcoded initial state.
        """
        dev = indigo.devices[dev_id]
        states: dict = {}
        for (ep, cluster, attribute), value in node.attributes.items():
            if ep != endpoint_id:
                continue
            handler = self.registry.handler_for_cluster(cluster)
            if handler is None:
                continue
            try:
                states.update(handler.on_attribute_update(dev, attribute, value))
            except Exception as exc:  # noqa: BLE001 - one bad attr must not abort priming
                self.logger.debug("prime %s attr %s/%s failed: %s", dev_id, cluster, attribute, exc)
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
            orphans = [dev_id for (node_id, _ep), dev_id in self._index.items()
                       if (node_id, _ep) not in live]
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
            dev_id = self.lookup(node.node_id, endpoint.endpoint_id)
            if dev_id is None:
                continue
            self._prime_states(node, dev_id, endpoint.endpoint_id)
            self._clear_error(dev_id)

    def mark_unreachable(self, node_id: Any) -> None:
        with self._lock:
            targets = [dev_id for (nid, _eid), dev_id in self._index.items()
                       if nid == int(node_id)]
        for dev_id in targets:
            self._safe_unreachable(dev_id)

    def mark_all_unreachable(self) -> None:
        """matter-server connection lost (drop / shutdown / sleep) — every Matter
        device is unreachable until we reconnect and reconcile."""
        with self._lock:
            targets = list(dict.fromkeys(self._index.values()))
        for dev_id in targets:
            self._safe_unreachable(dev_id)

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
        elif evt.kind in (protocol.EVT_NODE_ADDED, protocol.EVT_NODE_UPDATED):
            self._on_node_added(evt)
        elif evt.kind == protocol.EVT_NODE_REMOVED and evt.node_id is not None:
            self.mark_unreachable(evt.node_id)
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

    def _on_attribute(self, evt: protocol.MatterEvent) -> None:
        if evt.node_id is None or evt.endpoint is None or evt.cluster is None:
            return
        dev_id = self.lookup(evt.node_id, evt.endpoint)
        if dev_id is None:
            return
        if self._active and dev_id not in self._active:
            return  # gate updates to active devices once any are started
        handler = self.registry.handler_for_cluster(evt.cluster)
        if handler is None:
            return
        dev = indigo.devices[dev_id]
        states = handler.on_attribute_update(dev, evt.attribute, evt.value)
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
