"""Reconcile matter-server nodes ↔ Indigo devices, and apply state updates.

The authoritative ``(nodeId, endpointId) → indigoDeviceId`` map is derived from
each Indigo device's ``pluginProps`` (the single source of truth that survives
plugin reloads); an in-memory index caches it. This module is also the single
asyncio→Indigo write seam: ``apply_states`` is the one place ``updateStatesOnServer``
is called, so it can be swapped to ``run_in_executor`` later if the loop stalls.
"""
from __future__ import annotations

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
    def rebuild_index(self) -> None:
        self._index.clear()
        for dev in indigo.devices.iter("self"):
            props = dev.pluginProps
            node_id = props.get("nodeId")
            endpoint_id = props.get("endpointId")
            if node_id and endpoint_id:
                self._index[(int(node_id), int(endpoint_id))] = dev.id

    def lookup(self, node_id: Any, endpoint_id: Any) -> Optional[int]:
        return self._index.get((int(node_id), int(endpoint_id)))

    def note_device(self, dev: Any) -> None:
        """Index a single device from its pluginProps (deviceStartComm)."""
        props = dev.pluginProps
        node_id = props.get("nodeId")
        endpoint_id = props.get("endpointId")
        if node_id and endpoint_id:
            self._index[(int(node_id), int(endpoint_id))] = dev.id

    def delete_node(self, node_id: Any) -> list:
        """Delete all Indigo devices for a node; return their ids."""
        target = int(node_id)
        ids = [dev_id for (nid, _eid), dev_id in self._index.items() if nid == target]
        for dev_id in ids:
            try:
                indigo.device.delete(indigo.devices[dev_id])
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("delete device %s failed: %s", dev_id, exc)
        self._index = {key: val for key, val in self._index.items() if key[0] != target}
        return ids

    def node_count(self) -> int:
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
        multi = len(node.endpoints) > 1
        for endpoint in node.endpoints:
            existing = self.lookup(node.node_id, endpoint.endpoint_id)
            if existing is not None:
                created.append(existing)
                primary = primary if primary is not None else existing
                continue
            for spec in self.registry.handlers_for_endpoint(node, endpoint):
                name = spec.name
                if multi:
                    name = f"{spec.name} (endpoint {endpoint.endpoint_id})"
                dev_id = self._create_one(spec, name, suggested_room)
                if dev_id is None:
                    continue
                self._index[(node.node_id, endpoint.endpoint_id)] = dev_id
                created.append(dev_id)
                primary = primary if primary is not None else dev_id
        return {
            "indigoDeviceIds": created,
            "primaryDeviceId": primary,
            "endpointCount": len(node.endpoints),
            "vendorId": node.vendor_id,
            "productId": node.product_id,
            "vendorName": node.vendor_name,
            "productName": node.product_name,
        }

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
            node = parse_node(raw)
            for endpoint in node.endpoints:
                live.add((node.node_id, endpoint.endpoint_id))
            if not any(self.lookup(node.node_id, ep.endpoint_id) for ep in node.endpoints):
                self.logger.warning("node %s has no Indigo devices — creating", node.node_id)
            self.create_devices(node)
        for (node_id, _ep), dev_id in list(self._index.items()):
            if (node_id, _ep) not in live:
                self._safe_unreachable(dev_id)

    def mark_unreachable(self, node_id: Any) -> None:
        for (nid, _eid), dev_id in self._index.items():
            if nid == int(node_id):
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
