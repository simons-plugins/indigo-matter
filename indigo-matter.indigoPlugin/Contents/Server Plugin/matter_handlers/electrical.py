"""ElectricalPowerMeasurement (0x0090) + ElectricalEnergyMeasurement (0x0091).

Both clusters are non-primary: they attach energy/power UI onto an existing
relay or dimmer device (same endpoint), rather than creating a device of their
own — the same merge-into-sibling pattern used by FanControlHandler.

The relay/dimmer creates the Indigo device; these handlers receive attribute
updates for it and keep ``curEnergyLevel`` (Watts) and ``accumEnergyTotal``
(kWh) in sync. Indigo adds those states automatically when the device's
``SupportsPowerMeter`` / ``SupportsEnergyMeter`` props are True — those props
are injected by the relay/dimmer's ``create_indigo_devices`` at device creation
(see on_off.py / level_control.py and HANDOVER §4).
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterAction

CLUSTER_ELECTRICAL_POWER = 0x0090
CLUSTER_ELECTRICAL_ENERGY = 0x0091

# Power Topology (0x009C) — Matter 1.3's "Electrical Sensor" device type
# (0x0510, e.g. IKEA GRILLPLATS) puts the ElectricalPower/Energy clusters on
# their own endpoint rather than co-locating them with the relay/dimmer. This
# cluster tells device_sync which OTHER endpoint(s) the measurement applies
# to (issue #79). It has no ClusterHandler of its own — no live attribute
# routing is needed, device_sync reads its snapshot values directly out of
# node.attributes when resolving the meter-link map.
CLUSTER_POWER_TOPOLOGY = 0x009C

# FeatureMap (global attribute 0xFFFC, present on every cluster).
ATTR_POWER_TOPOLOGY_FEATURE_MAP = 0xFFFC

# Power Topology FeatureMap bits (connectedhomeip power-topology-cluster.xml /
# src/app_clusters/PowerTopology.adoc).
FEATURE_NODE_TOPOLOGY = 1 << 0        # "measures the whole node" — no endpoint list
FEATURE_TREE_TOPOLOGY = 1 << 1
FEATURE_SET_TOPOLOGY = 1 << 2         # gates AvailableEndpoints (0x0000)
FEATURE_DYNAMIC_POWER_FLOW = 1 << 3   # gates ActiveEndpoints (0x0001)

# Power Topology attributes (only meaningful when SetTopology is set).
ATTR_AVAILABLE_ENDPOINTS = 0x0000
ATTR_ACTIVE_ENDPOINTS = 0x0001


# ---------------------------------------------------------------------------
# ElectricalPowerMeasurement helpers
# ---------------------------------------------------------------------------

def _parse_active_power_mw(value: Any) -> Optional[float]:
    """Coerce an ActivePower attribute value to milliwatts, or None.

    matter-server represents nullable integers as None when null on the wire,
    or as a plain int/float otherwise.
    """
    if value is None:
        return None
    try:
        return float(int(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# ElectricalEnergyMeasurement helpers
# ---------------------------------------------------------------------------

def _parse_energy_struct_mwh(value: Any) -> Optional[float]:
    """Extract energy in mWh from a CumulativeEnergyImported attribute value.

    matter-server may serialise the EnergyMeasurementStruct three ways:

    1. ``{"energy": 12345}``                  — string-keyed dict
    2. ``{"0": 12345}``                       — tag-keyed dict (TLV index)
    3. ``12345``                              — bare number (degenerate)

    Any other shape (None, missing key, non-numeric) → None so the caller
    can silently discard it.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # bare numeric — treat directly as mWh
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        # prefer the "energy" string key, fall back to tag "0"
        raw = value.get("energy", value.get("0"))
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

class ElectricalPowerHandler(ClusterHandler):
    """ElectricalPowerMeasurement (0x0090) → ``curEnergyLevel`` (Watts).

    Non-primary: no Indigo device created. The relay/dimmer handler injects
    ``SupportsPowerMeter=True`` into device props at creation when this cluster
    is present, which unlocks the ``curEnergyLevel`` state automatically.
    """

    cluster_id = CLUSTER_ELECTRICAL_POWER
    cluster_name = "ElectricalPowerMeasurement"

    # ActivePower: signed32, nullable, milliwatts
    ATTR_ACTIVE_POWER = 0x0008

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_ACTIVE_POWER]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id != self.ATTR_ACTIVE_POWER:
            return {}
        mw = _parse_active_power_mw(value)
        if mw is None:
            return {}
        # Guard: state only exists when SupportsPowerMeter was True at creation.
        if "curEnergyLevel" not in getattr(indigo_dev, "states", {}):
            return {}
        return {"curEnergyLevel": round(mw / 1000.0, 1)}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # read-only cluster — no actions to send


class ElectricalEnergyHandler(ClusterHandler):
    """ElectricalEnergyMeasurement (0x0091) → ``accumEnergyTotal`` (kWh).

    Non-primary: no Indigo device created. The relay/dimmer handler injects
    ``SupportsEnergyMeter=True`` into device props at creation when this cluster
    is present, which unlocks the ``accumEnergyTotal`` state automatically.
    """

    cluster_id = CLUSTER_ELECTRICAL_ENERGY
    cluster_name = "ElectricalEnergyMeasurement"

    # CumulativeEnergyImported: EnergyMeasurementStruct, nullable
    ATTR_CUMULATIVE_IMPORTED = 0x0001

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_CUMULATIVE_IMPORTED]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id != self.ATTR_CUMULATIVE_IMPORTED:
            return {}
        mwh = _parse_energy_struct_mwh(value)
        if mwh is None:
            return {}
        # Guard: state only exists when SupportsEnergyMeter was True at creation.
        if "accumEnergyTotal" not in getattr(indigo_dev, "states", {}):
            return {}
        return {"accumEnergyTotal": round(mwh / 1_000_000.0, 3)}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # read-only cluster — no actions to send
