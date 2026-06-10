"""WindowCovering cluster (0x0102) → Indigo Dimmer.

Maps motorised blinds, shades, and curtains to an Indigo dimmer device so that
control pages, triggers, and schedules work out of the box:
  - brightness 100 = fully open, brightness 0 = fully closed
  - TurnOn  → UpOrOpen   (fully open)
  - TurnOff → DownOrClose (fully close)
  - SetBrightness b → GoToLiftPercentage (inverted to percent-closed)

Inversion note (Matter-spec-correct, hardware-unverified):
  Matter ``CurrentPositionLiftPercent100ths`` is *percent closed* (0 = fully
  open, 10 000 = fully closed), which is the opposite of the Indigo brightness
  convention (100 = fully open).  The helper :func:`lift_pct100ths_to_brightness`
  normalises so Indigo 100 means open.  Some firmware implementations invert
  this attribute (reporting 0 = closed, 10 000 = open); live validation against
  real hardware is required before treating this as confirmed.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand

CLUSTER_WINDOW_COVERING = 0x0102

# CurrentPositionLiftPercent100ths: 0 = fully open, 10 000 = fully closed.
ATTR_CURRENT_LIFT_PERCENT100THS = 0x000E


def lift_pct100ths_to_brightness(value: int) -> int:
    """Convert Matter lift percent100ths (0=open, 10000=closed) to Indigo brightness (0–100, 100=open).

    Clamped to [0, 100] so that out-of-spec firmware values (e.g. 10100) do not
    produce negative or >100 brightness levels.
    """
    return max(0, min(100, 100 - round(int(value) / 100)))


def brightness_to_lift_pct100ths(brightness: float) -> int:
    """Convert Indigo brightness (0–100, 100=open) to Matter lift percent100ths (0=open, 10000=closed)."""
    return (100 - int(brightness)) * 100


class WindowCoveringHandler(ClusterHandler):
    """Handles the WindowCovering cluster (0x0102) as an Indigo dimmer.

    Brightness slider in Indigo controls the open position; 100 = fully open,
    0 = fully closed.
    """

    cluster_id = CLUSTER_WINDOW_COVERING
    cluster_name = "WindowCovering"
    device_type_id = "matterWindowCovering"

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props={
                    "nodeId": str(node.node_id),
                    "endpointId": str(endpoint.endpoint_id),
                    "vendorName": node.vendor_name,
                    "productName": node.product_name,
                },
                initial_states={},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [ATTR_CURRENT_LIFT_PERCENT100THS]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == ATTR_CURRENT_LIFT_PERCENT100THS:
            if value is None:
                return {}
            b = lift_pct100ths_to_brightness(value)
            return {"brightnessLevel": b, "onOffState": b > 0}
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        device_action = action.deviceAction

        if device_action == indigo.kDeviceAction.TurnOn:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=CLUSTER_WINDOW_COVERING, command="UpOrOpen", args={},
            )
        if device_action == indigo.kDeviceAction.TurnOff:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=CLUSTER_WINDOW_COVERING, command="DownOrClose", args={},
            )
        if device_action == indigo.kDeviceAction.SetBrightness:
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=CLUSTER_WINDOW_COVERING, command="GoToLiftPercentage",
                args={"liftPercent100thsValue": brightness_to_lift_pct100ths(action.actionValue)},
            )
        if device_action == indigo.kDeviceAction.BrightenBy:
            new_b = min(indigo_dev.brightness + action.actionValue, 100)
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=CLUSTER_WINDOW_COVERING, command="GoToLiftPercentage",
                args={"liftPercent100thsValue": brightness_to_lift_pct100ths(new_b)},
            )
        if device_action == indigo.kDeviceAction.DimBy:
            new_b = max(indigo_dev.brightness - action.actionValue, 0)
            return MatterCommand(
                node_id=node_id, endpoint=endpoint_id,
                cluster=CLUSTER_WINDOW_COVERING, command="GoToLiftPercentage",
                args={"liftPercent100thsValue": brightness_to_lift_pct100ths(new_b)},
            )
        # Toggle is not meaningful for window coverings (no simple invert);
        # StopMotion has no Indigo dimmer action equivalent — omitted.
        return None
