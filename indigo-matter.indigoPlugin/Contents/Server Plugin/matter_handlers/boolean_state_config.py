"""BooleanStateConfiguration (0x0080) — a sensor's sensitivity level.

Non-primary: the cluster attaches a `sensitivityLevel` state onto an existing
occupancy/contact device (same endpoint), the same merge-into-sibling pattern
used by the electrical handlers (electrical.py). The Matter spec pairs 0x0080
with BooleanState (0x0045, matterContactSensor), but the Aqara FP300 co-locates
it with OccupancySensing (0x0406, matterMotionSensor) instead (issue #85) — this
handler supports both by not caring which primary handler owns the endpoint.

Writing the level is a plain attribute write (no Matter command), sent directly
by the plugin's "Set Sensitivity Level" custom device action (plugin.py) rather
than through ``handle_indigo_action`` — there is no Indigo built-in action shape
for an arbitrary numeric picker, so this is the plugin's first custom device
action (Actions.xml).
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterAction

CLUSTER_BOOLEAN_STATE_CONFIG = 0x0080

# CurrentSensitivityLevel: int8u, writable — the sensor's active sensitivity index.
ATTR_CURRENT_SENSITIVITY = 0x0000
# SupportedSensitivityLevels: int8u, read-only — count of valid indices (0..count-1).
ATTR_SUPPORTED_SENSITIVITY_LEVELS = 0x0001


def _parse_sensitivity(value: Any) -> Optional[int]:
    """Coerce a CurrentSensitivityLevel attribute value to an int, or None.

    Defensive the same way the electrical handlers guard ActivePower/energy
    values — matter-server may report a null/unparseable value on a slow or
    incomplete read.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class BooleanStateConfigHandler(ClusterHandler):
    """BooleanStateConfiguration (0x0080) → ``sensitivityLevel``.

    Non-primary: no Indigo device created. ``sensitivityLevel`` is a plain
    custom state (no Supports* prop gates it — see Devices.xml), declared only
    on matterMotionSensor/matterContactSensor, so the guard below protects
    against writing it onto some OTHER device type this cluster might one day
    be found co-located with.
    """

    cluster_id = CLUSTER_BOOLEAN_STATE_CONFIG
    cluster_name = "BooleanStateConfiguration"

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        return [ATTR_CURRENT_SENSITIVITY]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id != ATTR_CURRENT_SENSITIVITY:
            return {}
        level = _parse_sensitivity(value)
        if level is None:
            return {}
        # Guard: state only exists on device types that declare it in Devices.xml
        # (matterMotionSensor / matterContactSensor) — mirrors the electrical
        # handlers' curEnergyLevel/accumEnergyTotal guard.
        if "sensitivityLevel" not in getattr(indigo_dev, "states", {}):
            return {}
        return {"sensitivityLevel": level}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # Set Sensitivity Level writes the attribute directly (plugin.py)
