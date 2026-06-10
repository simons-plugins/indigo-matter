"""M-AQ: air quality + concentration cluster handlers (AirQuality, CO2, PM2.5, TVOC)."""
from __future__ import annotations

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.air_quality import (
    AirQualityHandler,
    CO2Handler,
    PM25Handler,
    TVOCHandler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


# ---------------------------------------------------------------------------
# AirQuality enum mapping
# ---------------------------------------------------------------------------

def test_air_quality_enum_known_values():
    h = AirQualityHandler()
    expected = {
        0: "unknown",
        1: "good",
        2: "fair",
        3: "moderate",
        4: "poor",
        5: "verypoor",
        6: "extremelypoor",
    }
    for int_val, str_val in expected.items():
        result = h.on_attribute_update(None, 0x0000, int_val)
        assert result == {"sensorValue": int_val, "airQuality": str_val}, (
            f"enum {int_val} should map to {str_val!r}"
        )


def test_air_quality_unknown_int_falls_back_to_unknown():
    """Out-of-spec enum values (e.g. 99) should map to 'unknown', not raise."""
    h = AirQualityHandler()
    result = h.on_attribute_update(None, 0x0000, 99)
    assert result == {"sensorValue": 99, "airQuality": "unknown"}


def test_air_quality_none_returns_empty():
    h = AirQualityHandler()
    assert h.on_attribute_update(None, 0x0000, None) == {}


def test_air_quality_wrong_attribute_returns_empty():
    h = AirQualityHandler()
    assert h.on_attribute_update(None, 0x0001, 1) == {}


# ---------------------------------------------------------------------------
# CO2 transform
# ---------------------------------------------------------------------------

def test_co2_transform_rounds_to_one_decimal():
    h = CO2Handler()
    assert h.on_attribute_update(None, 0x0000, 412.789) == {"sensorValue": 412.8}
    assert h.on_attribute_update(None, 0x0000, 400.0) == {"sensorValue": 400.0}


def test_co2_transform_none_returns_empty():
    h = CO2Handler()
    assert h.on_attribute_update(None, 0x0000, None) == {}


# ---------------------------------------------------------------------------
# PM2.5 transform
# ---------------------------------------------------------------------------

def test_pm25_transform_rounds_to_one_decimal():
    h = PM25Handler()
    assert h.on_attribute_update(None, 0x0000, 35.256) == {"sensorValue": 35.3}
    assert h.on_attribute_update(None, 0x0000, 12.0) == {"sensorValue": 12.0}


def test_pm25_transform_none_returns_empty():
    h = PM25Handler()
    assert h.on_attribute_update(None, 0x0000, None) == {}


# ---------------------------------------------------------------------------
# TVOC transform
# ---------------------------------------------------------------------------

def test_tvoc_transform_rounds_to_one_decimal():
    h = TVOCHandler()
    assert h.on_attribute_update(None, 0x0000, 120.567) == {"sensorValue": 120.6}
    assert h.on_attribute_update(None, 0x0000, 0.0) == {"sensorValue": 0.0}


def test_tvoc_transform_none_returns_empty():
    h = TVOCHandler()
    assert h.on_attribute_update(None, 0x0000, None) == {}


# ---------------------------------------------------------------------------
# Read-only (sensors must not produce commands)
# ---------------------------------------------------------------------------

def test_air_quality_handlers_are_read_only():
    for handler in (AirQualityHandler(), CO2Handler(), PM25Handler(), TVOCHandler()):
        assert handler.handle_indigo_action(None, object()) is None, (
            f"{type(handler).__name__} should be read-only"
        )


# ---------------------------------------------------------------------------
# create_indigo_devices — spec shape
# ---------------------------------------------------------------------------

_MINIMAL_NODE_AQ = {
    "node_id": 30,
    "attributes": {
        "1/91/0": 1,    # AirQuality cluster (0x005B = 91), attr 0 = 1 (Good)
        "1/29/0": [{"0": 0x0073, "1": 1}],  # descriptor
    },
}

_MINIMAL_NODE_CO2 = {
    "node_id": 31,
    "attributes": {
        "1/1037/0": 412.5,  # CO2 cluster (0x040D = 1037), MeasuredValue
        "1/29/0": [{"0": 0x0073, "1": 1}],
    },
}


def test_air_quality_create_spec_has_both_initial_states():
    node = parse_node(_MINIMAL_NODE_AQ, "AQ Monitor")
    ep = _ep(node, 1)
    specs = AirQualityHandler().create_indigo_devices(node, ep)
    assert len(specs) == 1
    s = specs[0]
    assert s.device_type_id == "matterAirQualitySensor"
    assert "sensorValue" in s.initial_states
    assert "airQuality" in s.initial_states
    assert s.initial_states["airQuality"] == "unknown"


def test_co2_create_spec():
    node = parse_node(_MINIMAL_NODE_CO2, "CO2 Sensor")
    ep = _ep(node, 1)
    specs = CO2Handler().create_indigo_devices(node, ep)
    assert len(specs) == 1
    assert specs[0].device_type_id == "matterCO2Sensor"
    assert specs[0].initial_states == {"sensorValue": 0}


# ---------------------------------------------------------------------------
# Registry-level: multi-cluster AQ endpoint → multiple Indigo devices
# ---------------------------------------------------------------------------

_MULTI_AQ_NODE = {
    "node_id": 40,
    "attributes": {
        # Endpoint 1: AirQuality + CO2 + PM2.5 + TVOC all present
        "1/91/0": 2,          # AirQuality = 2 (Fair)   cluster 0x005B = 91
        "1/1037/0": 500.0,    # CO2 (ppm)               cluster 0x040D = 1037
        "1/1066/0": 25.0,     # PM2.5 (µg/m³)           cluster 0x042A = 1066
        "1/1070/0": 300.0,    # TVOC                    cluster 0x042E = 1070
        "1/29/0": [{"0": 0x0073, "1": 1}],
    },
}


def test_multi_aq_endpoint_produces_four_sensors():
    """An endpoint exposing all four AQ clusters should produce four Indigo devices."""
    node = parse_node(_MULTI_AQ_NODE, "Eve Room")
    specs = HandlerRegistry().handlers_for_endpoint(node, _ep(node, 1))
    types = sorted(s.device_type_id for s in specs)
    assert types == [
        "matterAirQualitySensor",
        "matterCO2Sensor",
        "matterPM25Sensor",
        "matterTVOCSensor",
    ]


# ---------------------------------------------------------------------------
# Registry cluster-id lookup
# ---------------------------------------------------------------------------

def test_registry_cluster_id_lookup():
    reg = HandlerRegistry()
    assert reg.handler_for_cluster(0x005B).device_type_id == "matterAirQualitySensor"
    assert reg.handler_for_cluster(0x040D).device_type_id == "matterCO2Sensor"
    assert reg.handler_for_cluster(0x042A).device_type_id == "matterPM25Sensor"
    assert reg.handler_for_cluster(0x042E).device_type_id == "matterTVOCSensor"
