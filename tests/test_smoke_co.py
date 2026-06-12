"""Tests for SmokeCOAlarmHandler (cluster 0x005C).

Covers:
- Every ExpressedState enum value: alarm vs non-alarm onOffState
- All boolean attribute mappings (SmokeState, COState, BatteryAlert, EndOfServiceAlert)
- None handling for every subscribed attribute
- Unknown ExpressedState integer (falls back to str(int))
- create_indigo_devices spec shape
- Registry registration and cluster lookup
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from matter_handlers.smoke_co import (
    ATTR_BATTERY_ALERT,
    ATTR_CO_STATE,
    ATTR_END_OF_SERVICE,
    ATTR_EXPRESSED_STATE,
    ATTR_SMOKE_STATE,
    CLUSTER_SMOKE_CO_ALARM,
    SmokeCOAlarmHandler,
)
from matter_handlers.registry import HandlerRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _handler() -> SmokeCOAlarmHandler:
    return SmokeCOAlarmHandler()


def _fake_dev():
    return MagicMock()


# ---------------------------------------------------------------------------
# ExpressedState — alarm vs non-alarm onOffState
# ---------------------------------------------------------------------------

EXPRESSED_ALARM_CASES = [
    # (value, expected_state_name, expected_onOffState)
    (0, "normal",            False),  # Normal — no alarm
    (1, "smokeAlarm",        True),   # SmokeAlarm — trips sensor
    (2, "coAlarm",           True),   # COAlarm — trips sensor
    (3, "batteryAlert",      False),  # BatteryAlert — maintenance, not alarm
    (4, "testing",           False),  # Testing — does NOT trip sensor
    (5, "hardwareFault",     False),  # HardwareFault — maintenance, not alarm
    (6, "endOfService",      False),  # EndOfService — maintenance, not alarm
    (7, "interconnectSmoke", True),   # InterconnectSmoke — trips sensor
    (8, "interconnectCO",    True),   # InterconnectCO — trips sensor
]


@pytest.mark.parametrize("value,expected_name,expected_alarm", EXPRESSED_ALARM_CASES)
def test_expressed_state_known_values(value, expected_name, expected_alarm):
    h = _handler()
    result = h.on_attribute_update(_fake_dev(), ATTR_EXPRESSED_STATE, value)
    assert result["expressedState"] == expected_name
    assert result["onOffState"] is expected_alarm


def test_expressed_state_unknown_int_falls_back_to_string():
    """Unknown ExpressedState values are preserved as str(int) — not silently dropped."""
    h = _handler()
    result = h.on_attribute_update(_fake_dev(), ATTR_EXPRESSED_STATE, 99)
    assert result["expressedState"] == "99"
    assert result["onOffState"] is False  # unknown → not an alarm


# ---------------------------------------------------------------------------
# Boolean attribute mappings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("alarm_value,expected", [(0, False), (1, True), (2, True)])
def test_smoke_state_boolean(alarm_value, expected):
    """SmokeState > 0 → smokeAlarm True (Warning=1, Critical=2 both alarm)."""
    result = _handler().on_attribute_update(_fake_dev(), ATTR_SMOKE_STATE, alarm_value)
    assert result == {"smokeAlarm": expected}


@pytest.mark.parametrize("alarm_value,expected", [(0, False), (1, True), (2, True)])
def test_co_state_boolean(alarm_value, expected):
    """COState > 0 → coAlarm True."""
    result = _handler().on_attribute_update(_fake_dev(), ATTR_CO_STATE, alarm_value)
    assert result == {"coAlarm": expected}


@pytest.mark.parametrize("alarm_value,expected", [(0, False), (1, True), (2, True)])
def test_battery_alert_boolean(alarm_value, expected):
    """BatteryAlert > 0 → batteryAlert True."""
    result = _handler().on_attribute_update(_fake_dev(), ATTR_BATTERY_ALERT, alarm_value)
    assert result == {"batteryAlert": expected}


@pytest.mark.parametrize("alarm_value,expected", [(0, False), (1, True), (2, True)])
def test_end_of_service_boolean(alarm_value, expected):
    """EndOfServiceAlert > 0 → endOfService True."""
    result = _handler().on_attribute_update(_fake_dev(), ATTR_END_OF_SERVICE, alarm_value)
    assert result == {"endOfService": expected}


# ---------------------------------------------------------------------------
# None handling — every subscribed attribute
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attr_id", [
    ATTR_EXPRESSED_STATE,
    ATTR_SMOKE_STATE,
    ATTR_CO_STATE,
    ATTR_BATTERY_ALERT,
    ATTR_END_OF_SERVICE,
])
def test_none_value_always_returns_empty_dict(attr_id):
    """None → {} for every attribute; life-safety device must never fabricate Normal."""
    result = _handler().on_attribute_update(_fake_dev(), attr_id, None)
    assert result == {}, (
        f"Attribute 0x{attr_id:04X}: None must return {{}} — "
        "never fabricate a 'Normal' reading from a null"
    )


# ---------------------------------------------------------------------------
# Unknown attribute id
# ---------------------------------------------------------------------------

def test_unknown_attribute_returns_empty_dict():
    result = _handler().on_attribute_update(_fake_dev(), 0xFFFF, 42)
    assert result == {}


# ---------------------------------------------------------------------------
# handle_indigo_action — read-only
# ---------------------------------------------------------------------------

def test_handle_indigo_action_is_none():
    assert _handler().handle_indigo_action(_fake_dev(), object()) is None


# ---------------------------------------------------------------------------
# attributes_to_subscribe
# ---------------------------------------------------------------------------

def test_attributes_to_subscribe_contains_all_five():
    attrs = _handler().attributes_to_subscribe()
    assert ATTR_EXPRESSED_STATE in attrs
    assert ATTR_SMOKE_STATE     in attrs
    assert ATTR_CO_STATE        in attrs
    assert ATTR_BATTERY_ALERT   in attrs
    assert ATTR_END_OF_SERVICE  in attrs
    assert len(attrs) == 5


# ---------------------------------------------------------------------------
# create_indigo_devices spec
# ---------------------------------------------------------------------------

def _fake_node(node_id=42, product_name="Smoke Alarm", vendor_name="Acme",
               suggested_name=None):
    node = MagicMock()
    node.node_id       = node_id
    node.product_name  = product_name
    node.vendor_name   = vendor_name
    node.suggested_name = suggested_name
    return node


def _fake_endpoint(endpoint_id=1):
    ep = MagicMock()
    ep.endpoint_id = endpoint_id
    return ep


def test_create_devices_returns_one_spec():
    specs = _handler().create_indigo_devices(_fake_node(), _fake_endpoint())
    assert len(specs) == 1


def test_create_devices_spec_shape():
    node = _fake_node(node_id=7, product_name="Alarm", vendor_name="Kidde",
                      suggested_name="Hall Alarm")
    ep   = _fake_endpoint(endpoint_id=2)
    spec = _handler().create_indigo_devices(node, ep)[0]

    assert spec.device_type_id == "matterSmokeCOAlarm"
    assert spec.name == "Hall Alarm"
    assert spec.props["nodeId"]      == "7"
    assert spec.props["endpointId"]  == "2"
    assert spec.props["vendorName"]  == "Kidde"
    assert spec.props["productName"] == "Alarm"
    assert spec.initial_states == {"onOffState": False}


def test_create_devices_uses_product_name_when_no_suggested_name():
    node = _fake_node(suggested_name=None, product_name="Nest Protect")
    spec = _handler().create_indigo_devices(node, _fake_endpoint())[0]
    assert spec.name == "Nest Protect"


def test_create_devices_fallback_name_when_no_product_or_suggested():
    node = _fake_node(node_id=3, suggested_name=None, product_name=None)
    spec = _handler().create_indigo_devices(node, _fake_endpoint())[0]
    assert spec.name == "Matter 3"


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

def test_registry_resolves_smoke_co_cluster():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_SMOKE_CO_ALARM)
    assert handler is not None
    assert isinstance(handler, SmokeCOAlarmHandler)


def test_registry_resolves_by_device_type():
    reg = HandlerRegistry()
    handler = reg.handler_for_device(MagicMock(deviceTypeId="matterSmokeCOAlarm"))
    assert isinstance(handler, SmokeCOAlarmHandler)


def test_registry_cluster_id_is_correct():
    assert CLUSTER_SMOKE_CO_ALARM == 0x005C


def test_create_devices_sets_binary_display_props():
    # Issue #56: Supports* must be creation props (Devices.xml statics don't
    # apply to API-created devices). on/off IS the alarm display.
    spec = _handler().create_indigo_devices(_fake_node(), _fake_endpoint())[0]
    assert spec.props["SupportsOnState"] is True
    assert spec.props["SupportsSensorValue"] is False
