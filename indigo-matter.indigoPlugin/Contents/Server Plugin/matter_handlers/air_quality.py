"""Air quality + concentration cluster handlers.

v1 scope: AirQuality (0x005B), CO2 (0x040D), PM2.5 (0x042A), TVOC (0x042E).
Other concentration clusters (CO, NO2, Ozone, Formaldehyde, PM1, PM10, Radon)
can be added on demand by following the same pattern.

Reuses _SensorHandler from .sensors — it is package-internal by convention
(leading underscore), but same-package reuse is the coordinator's explicit
decision for this module. The base class handles create_indigo_devices,
attributes_to_subscribe, and handle_indigo_action; each subclass here only
overrides transform (or on_attribute_update for AirQuality's dual-state update).

Cluster / attribute / encoding (Matter spec):
  AirQuality                  0x005B  AirQuality(0x0000) enum8
  CO2Concentration            0x040D  MeasuredValue(0x0000) single float (ppm)
  PM25Concentration           0x042A  MeasuredValue(0x0000) single float (µg/m³)
  TVOCConcentration           0x042E  MeasuredValue(0x0000) single float
      (units depend on device's MeasurementUnit attr, 0x0008; typically ppb or
      µg/m³ — v1 passes the raw value through and does not read MeasurementUnit)
"""
from __future__ import annotations

from typing import Any

from .sensors import _SensorHandler  # same-package reuse — coordinator decision

# ---------------------------------------------------------------------------
# AirQuality enum map (cluster 0x005B, attribute 0x0000)
# ---------------------------------------------------------------------------
_AQ_ENUM: dict[int, str] = {
    0: "unknown",
    1: "good",
    2: "fair",
    3: "moderate",
    4: "poor",
    5: "verypoor",
    6: "extremelypoor",
}

ATTR_AIR_QUALITY = 0x0000


class AirQualityHandler(_SensorHandler):
    """Maps AirQuality cluster (0x005B) to a matterAirQualitySensor device.

    Attribute 0x0000 is an enum; we expose both the raw integer (sensorValue)
    and the human-readable string (airQuality) so automations can trigger on
    the string and display it directly.  The list display IS the string:
    with both Supports* props False the Devices.xml <UiDisplayStateId>
    applies even to API-created devices (verified live — issue #56).
    """

    cluster_id = 0x005B
    cluster_name = "AirQuality"
    device_type_id = "matterAirQualitySensor"
    # Override the value-sensor pair: with BOTH Supports* False, Indigo falls
    # back to <UiDisplayStateId>airQuality</UiDisplayStateId>, so the device
    # list shows "good"/"poor" instead of the raw enum int. Verified live on
    # jarvis (issue #56 follow-up). sensorValue keeps working — it is an
    # XML-declared custom state, not the disabled built-in.
    display_props = {"SupportsOnState": False, "SupportsSensorValue": False}

    measured_attr = ATTR_AIR_QUALITY

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list:
        # Override to add both initial states.
        specs = super().create_indigo_devices(node, endpoint)
        specs[0].initial_states["airQuality"] = "unknown"
        return specs

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        """Return both sensorValue (int) and airQuality (string) together."""
        if attribute_id == self.measured_attr and value is not None:
            int_val = int(value)
            return {
                "sensorValue": int_val,
                "airQuality": _AQ_ENUM.get(int_val, "unknown"),
            }
        return {}


# ---------------------------------------------------------------------------
# Concentration measurement cluster handlers (shared MeasuredValue shape)
# ---------------------------------------------------------------------------

class CO2Handler(_SensorHandler):
    """Maps CO2 concentration cluster (0x040D) → matterCO2Sensor (ppm)."""

    cluster_id = 0x040D
    cluster_name = "CarbonDioxideConcentrationMeasurement"
    device_type_id = "matterCO2Sensor"

    def transform(self, value: Any) -> float:
        return round(float(value), 1)  # ppm, MeasuredValue is a float


class PM25Handler(_SensorHandler):
    """Maps PM2.5 concentration cluster (0x042A) → matterPM25Sensor (µg/m³)."""

    cluster_id = 0x042A
    cluster_name = "PM25ConcentrationMeasurement"
    device_type_id = "matterPM25Sensor"

    def transform(self, value: Any) -> float:
        return round(float(value), 1)  # µg/m³, MeasuredValue is a float


class TVOCHandler(_SensorHandler):
    """Maps TVOC concentration cluster (0x042E) → matterTVOCSensor.

    v1 passes the raw MeasuredValue through without unit conversion.  The
    Matter spec allows ppb or µg/m³ depending on the device's MeasurementUnit
    attribute (0x0008); reading that attribute and annotating the state is
    deferred to a future iteration.
    """

    cluster_id = 0x042E
    cluster_name = "TotalVolatileOrganicCompoundsConcentrationMeasurement"
    device_type_id = "matterTVOCSensor"

    def transform(self, value: Any) -> float:
        return round(float(value), 1)  # units device-dependent (ppb or µg/m³)
