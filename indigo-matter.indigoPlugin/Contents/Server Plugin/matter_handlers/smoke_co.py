"""Smoke/CO Alarm cluster (0x005C) → Indigo sensor device.

The SmokeCOAlarm cluster is present on Matter smoke/CO alarms (SmartThings,
Aeotec, Kidde Matter-module, upcoming Nest-class devices).  This handler is
multi-attribute (5 subscribed attributes feeding separate Indigo states) so it
subclasses ClusterHandler directly rather than the single-attribute
_SensorHandler base in sensors.py.

Conservative null-handling (life-safety device):
  Any attribute delivering ``None`` produces an empty state dict so Indigo
  retains its last-known value. We deliberately never fabricate a "Normal"
  reading from a null — a device that goes silent is more dangerous than one
  that stays latched.  Each mapping has a comment flagging this policy.

Matter spec refs:
  SmokeCOAlarm cluster 0x005C, Matter 1.2 §2.11
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec

CLUSTER_SMOKE_CO_ALARM = 0x005C

# Attribute ids
ATTR_EXPRESSED_STATE   = 0x0000  # ExpressedState (overall alarm state)
ATTR_SMOKE_STATE       = 0x0001  # SmokeState     (AlarmStateEnum)
ATTR_CO_STATE          = 0x0002  # COState        (AlarmStateEnum)
ATTR_BATTERY_ALERT     = 0x0003  # BatteryAlert   (AlarmStateEnum)
ATTR_END_OF_SERVICE    = 0x0007  # EndOfServiceAlert (AlarmStateEnum)

# ExpressedState enum values
_EXPRESSED_STATE_NAMES = {
    0: "normal",
    1: "smokeAlarm",
    2: "coAlarm",
    3: "batteryAlert",
    4: "testing",
    5: "hardwareFault",
    6: "endOfService",
    7: "interconnectSmoke",
    8: "interconnectCO",
}

# ExpressedState values that represent an active alarm condition.
# Testing (4), BatteryAlert (3), HardwareFault (5), and EndOfService (6) do
# NOT trip the sensor — they require attention but are not life-safety alarms.
_EXPRESSED_ALARM_STATES = frozenset({1, 2, 7, 8})  # SmokeAlarm, COAlarm, InterconnectSmoke, InterconnectCO


class SmokeCOAlarmHandler(ClusterHandler):
    """Maps the SmokeCOAlarm cluster (0x005C) to a matterSmokeCOAlarm sensor.

    One Indigo sensor device per endpoint. ``onOffState`` is True only for
    active smoke/CO alarm conditions (ExpressedState ∈ {1,2,7,8}); testing
    and maintenance states do not trigger it.
    """
    cluster_id    = CLUSTER_SMOKE_CO_ALARM
    cluster_name  = "SmokeCOAlarm"
    device_type_id = "matterSmokeCOAlarm"

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props={
                    "nodeId":      str(node.node_id),
                    "endpointId":  str(endpoint.endpoint_id),
                    "vendorName":  node.vendor_name,
                    "productName": node.product_name,
                },
                initial_states={"onOffState": False},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [
            ATTR_EXPRESSED_STATE,
            ATTR_SMOKE_STATE,
            ATTR_CO_STATE,
            ATTR_BATTERY_ALERT,
            ATTR_END_OF_SERVICE,
        ]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        # None values always → {} (life-safety: never fabricate a "Normal"
        # reading from a null — keep last-known Indigo state instead).
        if value is None:
            return {}

        if attribute_id == ATTR_EXPRESSED_STATE:
            iv = int(value)
            state_name = _EXPRESSED_STATE_NAMES.get(iv, str(iv))
            is_alarm   = iv in _EXPRESSED_ALARM_STATES
            return {
                "expressedState": state_name,
                "onOffState":     is_alarm,
            }

        if attribute_id == ATTR_SMOKE_STATE:
            return {"smokeAlarm": int(value) > 0}

        if attribute_id == ATTR_CO_STATE:
            return {"coAlarm": int(value) > 0}

        if attribute_id == ATTR_BATTERY_ALERT:
            # Subscribed for future use / state visibility; maps to batteryAlert.
            return {"batteryAlert": int(value) > 0}

        if attribute_id == ATTR_END_OF_SERVICE:
            return {"endOfService": int(value) > 0}

        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> None:
        return None  # SmokeCOAlarm is read-only; no writable attributes
