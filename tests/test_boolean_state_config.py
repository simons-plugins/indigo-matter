"""Tests for BooleanStateConfiguration (0x0080) — issue #85.

Covers:
- CurrentSensitivityLevel parsing: nominal, zero, None, garbage
- Handler contract: non-primary, no devices created, no Matter action
- on_attribute_update: sensitivityLevel state, missing-state guard, wrong attribute
- Registry registration + non-primary behaviour alongside a real primary handler
  (occupancy) — mirrors test_electrical.py's TestOnOffEnergyProps shape.
"""
from __future__ import annotations

from matter_handlers.boolean_state_config import (
    ATTR_CURRENT_SENSITIVITY,
    ATTR_SUPPORTED_SENSITIVITY_LEVELS,
    CLUSTER_BOOLEAN_STATE_CONFIG,
    BooleanStateConfigHandler,
    _parse_sensitivity,
)
from matter_handlers.registry import HandlerRegistry
from matter_handlers.sensors import OccupancyHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Dev:
    """Minimal Indigo device stub."""
    def __init__(self, states=None, props=None):
        self.states = states or {}
        self.pluginProps = props or {}


class _Endpoint:
    """Minimal endpoint stub that advertises a set of cluster ids."""
    def __init__(self, endpoint_id: int, clusters: set):
        self.endpoint_id = endpoint_id
        self._clusters = clusters

    def has(self, cluster_id: int) -> bool:
        return cluster_id in self._clusters


class _Node:
    """Minimal node stub."""
    def __init__(self, node_id: int = 0x2D):
        self.node_id = node_id
        self.suggested_name = "Test Sensor"
        self.vendor_name = "Aqara"
        self.product_name = "FP300"


# ---------------------------------------------------------------------------
# _parse_sensitivity tests
# ---------------------------------------------------------------------------

def test_parse_sensitivity_nominal():
    assert _parse_sensitivity(1) == 1


def test_parse_sensitivity_zero():
    assert _parse_sensitivity(0) == 0


def test_parse_sensitivity_none_returns_none():
    assert _parse_sensitivity(None) is None


def test_parse_sensitivity_bad_type_returns_none():
    assert _parse_sensitivity("not a number") is None
    assert _parse_sensitivity([]) is None


# ---------------------------------------------------------------------------
# BooleanStateConfigHandler contract
# ---------------------------------------------------------------------------

class TestBooleanStateConfigHandler:
    def setup_method(self):
        self.h = BooleanStateConfigHandler()

    def test_cluster_constants(self):
        assert self.h.cluster_id == CLUSTER_BOOLEAN_STATE_CONFIG == 0x0080
        assert self.h.cluster_name == "BooleanStateConfiguration"

    def test_is_not_primary(self):
        assert self.h.is_primary_for(None, None) is False

    def test_create_indigo_devices_returns_empty(self):
        assert self.h.create_indigo_devices(None, None) == []

    def test_attributes_to_subscribe(self):
        assert self.h.attributes_to_subscribe() == [ATTR_CURRENT_SENSITIVITY]

    def test_handle_indigo_action_returns_none(self):
        assert self.h.handle_indigo_action(None, None) is None

    def test_on_attribute_update_sets_sensitivity_level(self):
        dev = _Dev(states={"sensitivityLevel": 0})
        result = self.h.on_attribute_update(dev, ATTR_CURRENT_SENSITIVITY, 2)
        assert result == {"sensitivityLevel": 2}

    def test_on_attribute_update_zero_level(self):
        dev = _Dev(states={"sensitivityLevel": 1})
        result = self.h.on_attribute_update(dev, ATTR_CURRENT_SENSITIVITY, 0)
        assert result == {"sensitivityLevel": 0}

    def test_on_attribute_update_null_value_returns_empty(self):
        dev = _Dev(states={"sensitivityLevel": 0})
        result = self.h.on_attribute_update(dev, ATTR_CURRENT_SENSITIVITY, None)
        assert result == {}

    def test_on_attribute_update_missing_state_guard(self):
        """If the device type doesn't declare sensitivityLevel, drop the update."""
        dev = _Dev(states={})  # sensitivityLevel not present
        result = self.h.on_attribute_update(dev, ATTR_CURRENT_SENSITIVITY, 2)
        assert result == {}

    def test_on_attribute_update_ignores_supported_levels_attribute(self):
        """SupportedSensitivityLevels (0x0001) is not a state — never written."""
        dev = _Dev(states={"sensitivityLevel": 0})
        result = self.h.on_attribute_update(dev, ATTR_SUPPORTED_SENSITIVITY_LEVELS, 3)
        assert result == {}

    def test_on_attribute_update_wrong_attribute_id(self):
        dev = _Dev(states={"sensitivityLevel": 0})
        result = self.h.on_attribute_update(dev, 0x0099, 2)
        assert result == {}


# ---------------------------------------------------------------------------
# Registry registration + non-primary behaviour
# ---------------------------------------------------------------------------

def test_registry_includes_boolean_state_config_handler():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_BOOLEAN_STATE_CONFIG)
    assert isinstance(handler, BooleanStateConfigHandler)


def test_registry_boolean_state_config_is_non_primary():
    """Non-primary handlers produce no specs when no primary handler is present.

    (a) BooleanStateConfig-only endpoint -> empty spec list.
    (b) OccupancySensing + BooleanStateConfig endpoint -> exactly one spec
        (matterMotionSensor), proving BooleanStateConfig contributes nothing
        of its own while a real primary (occupancy) exists — the FP300 shape
        from issue #85.
    """
    reg = HandlerRegistry()

    ep_config_only = _Endpoint(1, {CLUSTER_BOOLEAN_STATE_CONFIG})
    specs = reg.handlers_for_endpoint(_Node(), ep_config_only)
    assert specs == []

    ep_occupancy_with_config = _Endpoint(1, {OccupancyHandler().cluster_id, CLUSTER_BOOLEAN_STATE_CONFIG})
    specs = reg.handlers_for_endpoint(_Node(), ep_occupancy_with_config)
    assert len(specs) == 1
    assert specs[0].device_type_id == "matterMotionSensor"
