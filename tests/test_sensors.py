"""M6: sensor cluster handlers (temperature, humidity, occupancy, contact, lux, pressure, flow)."""
from __future__ import annotations

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.sensors import (
    ContactHandler,
    FlowHandler,
    HumidityHandler,
    IlluminanceHandler,
    OccupancyHandler,
    PressureHandler,
    TemperatureHandler,
)

TEMP_HUMIDITY_NODE = {
    "node_id": 20,
    "attributes": {
        # one endpoint exposing BOTH temperature and humidity
        "1/1026/0": 2185,   # TemperatureMeasurement MeasuredValue = 21.85 °C
        "1/1029/0": 4750,   # RelativeHumidity MeasuredValue = 47.5 %RH
        "1/29/0": [{"0": 770, "1": 1}],
    },
}


def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


def test_combined_endpoint_makes_two_sensor_devices():
    node = parse_node(TEMP_HUMIDITY_NODE, "Climate")
    specs = HandlerRegistry().handlers_for_endpoint(node, _ep(node, 1))
    types = sorted(s.device_type_id for s in specs)
    assert types == ["matterHumiditySensor", "matterTemperatureSensor"]


def test_temperature_transform():
    h = TemperatureHandler()
    assert h.on_attribute_update(None, 0x0000, 2185) == {"sensorValue": 21.85}
    assert h.on_attribute_update(None, 0x0000, -500) == {"sensorValue": -5.0}
    assert h.on_attribute_update(None, 0x0000, None) == {}


def test_humidity_transform():
    assert HumidityHandler().on_attribute_update(None, 0x0000, 4750) == {"sensorValue": 47.5}


def test_occupancy_bitmap():
    h = OccupancyHandler()
    assert h.on_attribute_update(None, 0x0000, 1) == {"onOffState": True}
    assert h.on_attribute_update(None, 0x0000, 0) == {"onOffState": False}


def test_contact_boolean():
    h = ContactHandler()
    assert h.on_attribute_update(None, 0x0000, True) == {"onOffState": True}
    assert h.on_attribute_update(None, 0x0000, False) == {"onOffState": False}


def test_illuminance_lux_decode():
    h = IlluminanceHandler()
    # raw = 10000*log10(lux)+1; lux 1000 → raw 30001
    out = h.on_attribute_update(None, 0x0000, 30001)
    assert abs(out["sensorValue"] - 1000) < 5
    assert h.on_attribute_update(None, 0x0000, 0) == {"sensorValue": 0.0}


def test_sensors_are_read_only():
    for handler in (TemperatureHandler(), HumidityHandler(), OccupancyHandler(),
                    ContactHandler(), IlluminanceHandler(),
                    PressureHandler(), FlowHandler()):
        assert handler.handle_indigo_action(None, object()) is None


def test_sensor_device_type_lookup():
    reg = HandlerRegistry()
    assert reg.handler_for_cluster(0x0402).device_type_id == "matterTemperatureSensor"
    assert reg.handler_for_cluster(0x0405).device_type_id == "matterHumiditySensor"
    assert reg.handler_for_cluster(0x0406).device_type_id == "matterMotionSensor"
    assert reg.handler_for_cluster(0x0045).device_type_id == "matterContactSensor"
    assert reg.handler_for_cluster(0x0400).device_type_id == "matterIlluminanceSensor"
    assert reg.handler_for_cluster(0x0403).device_type_id == "matterPressureSensor"
    assert reg.handler_for_cluster(0x0404).device_type_id == "matterFlowSensor"


# ── Pressure (0x0403) ────────────────────────────────────────────────────────

def test_pressure_transform():
    h = PressureHandler()
    # 0.1 kPa = 1 hPa: unit conversion is an identity (raw value → hPa as float)
    assert h.on_attribute_update(None, 0x0000, 1013) == {"sensorValue": 1013.0}  # sea level ≈ 1013 hPa
    assert h.on_attribute_update(None, 0x0000, 870) == {"sensorValue": 870.0}    # high altitude


def test_pressure_none_skipped():
    assert PressureHandler().on_attribute_update(None, 0x0000, None) == {}


def test_pressure_create_spec():
    h = PressureHandler()
    assert h.device_type_id == "matterPressureSensor"
    assert h.cluster_id == 0x0403
    assert h.cluster_name == "PressureMeasurement"


# ── Flow (0x0404) ────────────────────────────────────────────────────────────

def test_flow_transform():
    h = FlowHandler()
    assert h.on_attribute_update(None, 0x0000, 25) == {"sensorValue": 2.5}   # 25 × 0.1 = 2.5 m³/h
    assert h.on_attribute_update(None, 0x0000, 100) == {"sensorValue": 10.0}
    assert h.on_attribute_update(None, 0x0000, 0) == {"sensorValue": 0.0}


def test_flow_none_skipped():
    assert FlowHandler().on_attribute_update(None, 0x0000, None) == {}


def test_flow_create_spec():
    h = FlowHandler()
    assert h.device_type_id == "matterFlowSensor"
    assert h.cluster_id == 0x0404
    assert h.cluster_name == "FlowMeasurement"
