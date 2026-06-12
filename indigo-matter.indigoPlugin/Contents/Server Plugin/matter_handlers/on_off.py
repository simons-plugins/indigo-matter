"""OnOff cluster (0x0006) → Indigo Relay device.

If LevelControl (0x0008) is also present on the endpoint, the dimmer handler is
preferred and this handler defers (returns no device) — though it still owns
OnOff attribute updates and on/off commands as part of the dimmer in later
milestones. v1 (M4) only wires the relay path.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ENDPOINT_OWNER_CLUSTERS, ClusterHandler, IndigoDeviceSpec, MatterCommand
from .electrical import CLUSTER_ELECTRICAL_ENERGY, CLUSTER_ELECTRICAL_POWER

CLUSTER_LEVEL_CONTROL = 0x0008
CLUSTER_COLOR_CONTROL = 0x0300


class OnOffHandler(ClusterHandler):
    cluster_id = 0x0006
    cluster_name = "OnOff"
    device_type_id = "matterRelay"

    ATTR_ON_OFF = 0x0000
    CMD_OFF = "Off"
    CMD_ON = "On"
    CMD_TOGGLE = "Toggle"

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        # A richer lighting handler (dimmer OR colour — a colour light is not
        # required to carry LevelControl) owns this endpoint; a rich actuator
        # cluster (fan/thermostat/covering/lock/valve) present → that handler
        # owns it and this OnOff is its subordinate power switch, not a
        # standalone relay (issue #58 — duplicate-device class).
        if endpoint.has(CLUSTER_LEVEL_CONTROL) or endpoint.has(CLUSTER_COLOR_CONTROL):
            return False
        return not any(endpoint.has(c) for c in ENDPOINT_OWNER_CLUSTERS)

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        if not self.is_primary_for(node, endpoint):
            return []
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        props: dict = {
            "nodeId": str(node.node_id),
            "endpointId": str(endpoint.endpoint_id),
            "vendorName": node.vendor_name,
            "productName": node.product_name,
        }
        # Energy support must be set as device props at creation: Indigo does not
        # apply static <Supports*> Devices.xml elements to API-created devices
        # (same lesson as colour support, HANDOVER 2026-06-09 item 4). When these
        # props are True, Indigo automatically adds curEnergyLevel / accumEnergyTotal
        # states that ElectricalPowerHandler / ElectricalEnergyHandler then update.
        if endpoint.has(CLUSTER_ELECTRICAL_POWER):
            props["SupportsPowerMeter"] = True
        if endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
            props["SupportsEnergyMeter"] = True
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props=props,
                initial_states={"onOffState": False},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_ON_OFF]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == self.ATTR_ON_OFF:
            return {"onOffState": bool(value)}
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        device_action = action.deviceAction
        if device_action == indigo.kDeviceAction.TurnOn:
            command = self.CMD_ON
        elif device_action == indigo.kDeviceAction.TurnOff:
            command = self.CMD_OFF
        elif device_action == indigo.kDeviceAction.Toggle:
            command = self.CMD_TOGGLE
        else:
            return None
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id,
            cluster=self.cluster_id, command=command, args={},
        )
