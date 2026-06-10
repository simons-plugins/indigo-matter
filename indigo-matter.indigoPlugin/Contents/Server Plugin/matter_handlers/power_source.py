"""PowerSource cluster (0x002F) → battery level on sensor devices.

Battery-powered Matter devices expose the PowerSource cluster, typically on
endpoint 0 (the root endpoint) rather than on the sensor endpoint itself.
Because it lives on a different endpoint from the sensor cluster, it is
node-scoped: dispatch fans the update out to ALL Indigo devices for the node
rather than the single device at the event's endpoint.

Matter spec: BatPercentRemaining (0x000C) uint8, half-percent units 0–200
(divide by 2 to get 0–100 %); nullable.

Like FanControlHandler, this handler is non-primary (creates no Indigo device
of its own) and merges a single state into sibling devices. The batteryLevel
state guard mirrors color_control.py's whiteTemperature guard: a device
created before this feature lacks the state, so we degrade quietly rather
than erroring on every update.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand

CLUSTER_POWER_SOURCE = 0x002F
ATTR_BAT_PERCENT_REMAINING = 0x000C


class PowerSourceHandler(ClusterHandler):
    """Merges battery level from the PowerSource cluster into sibling devices.

    Node-scoped: attribute updates are fanned out to all Indigo devices for
    the node, regardless of which endpoint the PowerSource cluster lives on.
    """
    cluster_id = CLUSTER_POWER_SOURCE
    cluster_name = "PowerSource"
    node_scoped = True

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False  # never creates its own device; merges into siblings

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        return [ATTR_BAT_PERCENT_REMAINING]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id != ATTR_BAT_PERCENT_REMAINING or value is None:
            return {}
        # Guard: devices created before this feature lack the batteryLevel state.
        # Mirrors the 'if channel in dev.states' SDK pattern (same as the
        # whiteTemperature guard in color_control.py).
        if "batteryLevel" not in getattr(indigo_dev, "states", {}):
            return {}
        # Matter BatPercentRemaining is in half-percent units (0–200); clamp
        # the result to [0, 100] to defend against out-of-spec values.
        return {"batteryLevel": max(0, min(100, int(value) // 2))}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        return None  # read-only; no writable attributes
