"""LevelControl cluster (0x0008) → Indigo Dimmer.

Owns an endpoint that has OnOff + LevelControl but NOT ColorControl (the color
handler takes precedence when ColorControl is present). Handles on/off and
brightness; attribute updates for OnOff (cluster 6) are dispatched to the OnOff
handler by cluster id, and LevelControl (cluster 8) here.

Matter CurrentLevel is 0–254; Indigo brightness is 0–100.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand
from .electrical import CLUSTER_ELECTRICAL_ENERGY, CLUSTER_ELECTRICAL_POWER

CLUSTER_ON_OFF = 0x0006
CLUSTER_LEVEL_CONTROL = 0x0008
CLUSTER_COLOR_CONTROL = 0x0300


def level_to_pct(level: int) -> int:
    return max(0, min(100, round(level * 100 / 254)))


def pct_to_level(pct: float) -> int:
    return max(0, min(254, round(pct * 254 / 100)))


class LevelControlHandler(ClusterHandler):
    cluster_id = 0x0008
    cluster_name = "LevelControl"
    device_type_id = "matterDimmer"

    ATTR_CURRENT_LEVEL = 0x0000

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        # ColorControl present → the color handler owns this endpoint.
        return not endpoint.has(CLUSTER_COLOR_CONTROL)

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
                initial_states={"onOffState": False, "brightnessLevel": 0},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_CURRENT_LEVEL]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == self.ATTR_CURRENT_LEVEL and value is not None:
            return {"brightnessLevel": level_to_pct(int(value))}
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        device_action = action.deviceAction

        # On/off route through the OnOff cluster.
        if device_action == indigo.kDeviceAction.TurnOn:
            return self._onoff(node_id, endpoint_id, "On")
        if device_action == indigo.kDeviceAction.TurnOff:
            return self._onoff(node_id, endpoint_id, "Off")
        if device_action == indigo.kDeviceAction.Toggle:
            return self._onoff(node_id, endpoint_id, "Toggle")

        # Brightness routes through LevelControl (MoveToLevelWithOnOff so 0 turns off
        # and >0 turns on).
        if device_action == indigo.kDeviceAction.SetBrightness:
            return self._set_level(node_id, endpoint_id, action.actionValue)
        if device_action == indigo.kDeviceAction.BrightenBy:
            return self._set_level(node_id, endpoint_id, min(indigo_dev.brightness + action.actionValue, 100))
        if device_action == indigo.kDeviceAction.DimBy:
            return self._set_level(node_id, endpoint_id, max(indigo_dev.brightness - action.actionValue, 0))
        return None

    @staticmethod
    def _onoff(node_id: int, endpoint_id: int, command: str) -> MatterCommand:
        return MatterCommand(node_id=node_id, endpoint=endpoint_id,
                             cluster=CLUSTER_ON_OFF, command=command, args={})

    def _set_level(self, node_id: int, endpoint_id: int, pct: float) -> MatterCommand:
        # Always the LevelControl cluster — NOT self.cluster_id, which is 0x0300
        # when this method is inherited by ColorControlHandler.
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id, cluster=CLUSTER_LEVEL_CONTROL,
            command="MoveToLevelWithOnOff",
            args={"level": pct_to_level(pct), "transitionTime": 0,
                  "optionsMask": 0, "optionsOverride": 0},
        )
