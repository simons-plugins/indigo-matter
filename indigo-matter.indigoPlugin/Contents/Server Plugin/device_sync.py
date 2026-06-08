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
        expected = 0
        failed = 0
        multi = len(node.endpoints) > 1
        with self._lock:
            for endpoint in node.endpoints:
                existing = self._index.get((int(node.node_id), int(endpoint.endpoint_id)))
                if existing is not None:
                    created.append(existing)
                    primary = primary if primary is not None else existing
                    continue
                for spec in self.registry.handlers_for_endpoint(node, endpoint):
                    expected += 1
                    name = spec.name
                    if multi:
                        name = f"{spec.name} (endpoint {endpoint.endpoint_id})"
                    dev_id = self._create_one(spec, name, suggested_room)
                    if dev_id is None:
                        failed += 1
                        continue
                    self._index[(node.node_id, endpoint.endpoint_id)] = dev_id
                    created.append(dev_id)
                    primary = primary if primary is not None else dev_id
        if failed:
            # partial creation must be visible to the commission result, not hidden
            self.logger.warning(
                "node %s: %d of %d expected device(s) failed to create",
                node.node_id, failed, expected,
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

    def _create_one(self, spec: Any, name: str, room: Optional[str]) -> Optional[int]:
        try:
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                deviceTypeId=spec.device_type_id,
                name=self._unique_name(name),
                props=spec.props,
                folder=room or "",
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
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("reconcile of node %s failed: %s", node.node_id, exc)
        with self._lock:
            orphans = [dev_id for (node_id, _ep), dev_id in self._index.items()
                       if (node_id, _ep) not in live]
        for dev_id in orphans:
            self._safe_unreachable(dev_id)

    def mark_unreachable(self, node_id: Any) -> None:
        with self._lock:
            targets = [dev_id for (nid, _eid), dev_id in self._index.items()
                       if nid == int(node_id)]
        for dev_id in targets:
            self._safe_unreachable(dev_id)

    def _safe_unreachable(self, dev_id: int) -> None:
        try:
            indigo.devices[dev_id].setErrorStateOnServer("unreachable")
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("could not mark %s unreachable: %s", dev_id, exc)

    # ------------------------------------------------------------------
    # Inbound events (asyncio thread) → Indigo state
    # ------------------------------------------------------------------
    def handle_event(self, evt: protocol.MatterEvent) -> None:
        if evt.kind == protocol.EVT_ATTRIBUTE_UPDATED:
            self._on_attribute(evt)
        elif evt.kind == protocol.EVT_NODE_REMOVED and evt.node_id is not None:
            self.mark_unreachable(evt.node_id)

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
        indigo.devices[dev_id].updateStatesOnServer(kvlist)

    # ------------------------------------------------------------------
    # Indigo action → Matter command (B1 path)
    # ------------------------------------------------------------------
    def build_command(self, dev: Any, action: Any) -> Optional[MatterCommand]:
        handler = self.registry.handler_for_device(dev)
        if handler is None:
            return None
        return handler.handle_indigo_action(dev, action)
