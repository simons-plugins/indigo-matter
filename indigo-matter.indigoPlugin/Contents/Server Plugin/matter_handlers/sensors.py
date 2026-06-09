"""Sensor clusters → Indigo sensor devices (read-only).

Each sensor cluster maps a single measured attribute to one Indigo sensor state.
Sensors are additive per endpoint — a combined temperature+humidity device
produces two Indigo sensors (the registry collects all matching handlers).

Cluster / attribute / encoding (Matter spec):
  TemperatureMeasurement   0x0402  MeasuredValue(0)  int16, 0.01 °C
  RelativeHumidityMeasure  0x0405  MeasuredValue(0)  uint16, 0.01 %RH
  OccupancySensing         0x0406  Occupancy(0)      bitmap8, bit0 = occupied
  BooleanState             0x0045  StateValue(0)     bool (contact)
  IlluminanceMeasurement   0x0400  MeasuredValue(0)  uint16, 10000*log10(lux)+1
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand

ATTR_MEASURED_VALUE = 0x0000


class _SensorHandler(ClusterHandler):
    """Base for read-only single-attribute sensors."""

    measured_attr = ATTR_MEASURED_VALUE
    state_key = "sensorValue"

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
                initial_states={self.state_key: 0},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [self.measured_attr]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == self.measured_attr and value is not None:
            return {self.state_key: self.transform(value)}
        return {}

    def transform(self, value: Any) -> Any:  # noqa: D401
        return value

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        return None  # sensors are read-only


class TemperatureHandler(_SensorHandler):
    cluster_id = 0x0402
    cluster_name = "TemperatureMeasurement"
    device_type_id = "matterTemperatureSensor"

    def transform(self, value: Any) -> float:
        return round(int(value) / 100.0, 2)  # 0.01 °C → °C


class HumidityHandler(_SensorHandler):
    cluster_id = 0x0405
    cluster_name = "RelativeHumidityMeasurement"
    device_type_id = "matterHumiditySensor"

    def transform(self, value: Any) -> float:
        return round(int(value) / 100.0, 1)  # 0.01 %RH → %RH


class OccupancyHandler(_SensorHandler):
    cluster_id = 0x0406
    cluster_name = "OccupancySensing"
    device_type_id = "matterMotionSensor"
    state_key = "onOffState"

    def transform(self, value: Any) -> bool:
        return bool(int(value) & 0x01)  # bit0 = occupied


class ContactHandler(_SensorHandler):
    cluster_id = 0x0045
    cluster_name = "BooleanState"
    device_type_id = "matterContactSensor"
    state_key = "onOffState"

    def transform(self, value: Any) -> bool:
        return bool(value)


class IlluminanceHandler(_SensorHandler):
    cluster_id = 0x0400
    cluster_name = "IlluminanceMeasurement"
    device_type_id = "matterIlluminanceSensor"

    def transform(self, value: Any) -> float:
        raw = int(value)
        if raw <= 0:
            return 0.0  # 0 = unknown/invalid per spec
        return round(10 ** ((raw - 1) / 10000.0), 1)  # → lux
