"""Tests for ElectricalPowerMeasurement (0x0090) and ElectricalEnergyMeasurement (0x0091).

Covers:
- Power transform: mW → W, None/nullable, missing-state guard
- Energy struct parsing: string-keyed dict, tag-keyed dict, bare number, garbage
- Relay (OnOff) props decoration with/without the energy clusters present
- Dimmer (LevelControl) props decoration with/without the energy clusters present
"""
from __future__ import annotations

import pytest

from matter_handlers.electrical import (
    CLUSTER_ELECTRICAL_ENERGY,
    CLUSTER_ELECTRICAL_POWER,
    ElectricalEnergyHandler,
    ElectricalPowerHandler,
    _parse_active_power_mw,
    _parse_energy_struct_mwh,
)
from matter_handlers.on_off import OnOffHandler
from matter_handlers.level_control import LevelControlHandler
from matter_handlers.registry import HandlerRegistry


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
    def __init__(self, node_id: int = 1):
        self.node_id = node_id
        self.suggested_name = "Test Plug"
        self.vendor_name = "ACME"
        self.product_name = "Smart Plug"


# ---------------------------------------------------------------------------
# _parse_active_power_mw tests
# ---------------------------------------------------------------------------

def test_parse_active_power_mw_nominal():
    assert _parse_active_power_mw(1500) == 1500.0


def test_parse_active_power_mw_float_value():
    assert _parse_active_power_mw(2000.7) == 2000.0  # truncated via int()


def test_parse_active_power_mw_zero():
    assert _parse_active_power_mw(0) == 0.0


def test_parse_active_power_mw_none_returns_none():
    assert _parse_active_power_mw(None) is None


def test_parse_active_power_mw_bad_type_returns_none():
    assert _parse_active_power_mw("not a number") is None
    assert _parse_active_power_mw([]) is None


# ---------------------------------------------------------------------------
# _parse_energy_struct_mwh tests
# ---------------------------------------------------------------------------

def test_parse_energy_struct_string_keyed_dict():
    """matter-server string-keyed struct: {"energy": <mWh>}."""
    assert _parse_energy_struct_mwh({"energy": 3_600_000}) == 3_600_000.0


def test_parse_energy_struct_tag_keyed_dict():
    """TLV tag-keyed struct: {"0": <mWh>}."""
    assert _parse_energy_struct_mwh({"0": 7_200_000}) == 7_200_000.0


def test_parse_energy_struct_string_key_preferred_over_tag():
    """If both keys present, "energy" wins."""
    assert _parse_energy_struct_mwh({"energy": 100, "0": 999}) == 100.0


def test_parse_energy_struct_bare_number():
    """Degenerate bare int/float — treated directly as mWh."""
    assert _parse_energy_struct_mwh(1_800_000) == 1_800_000.0
    assert _parse_energy_struct_mwh(0.0) == 0.0


def test_parse_energy_struct_none_returns_none():
    assert _parse_energy_struct_mwh(None) is None


def test_parse_energy_struct_dict_missing_keys_returns_none():
    assert _parse_energy_struct_mwh({"voltage": 230}) is None


def test_parse_energy_struct_bad_value_in_dict_returns_none():
    assert _parse_energy_struct_mwh({"energy": "not-a-number"}) is None


def test_parse_energy_struct_unsupported_type_returns_none():
    assert _parse_energy_struct_mwh("garbage") is None
    assert _parse_energy_struct_mwh([1, 2, 3]) is None


# ---------------------------------------------------------------------------
# ElectricalPowerHandler tests
# ---------------------------------------------------------------------------

class TestElectricalPowerHandler:
    def setup_method(self):
        self.h = ElectricalPowerHandler()

    def test_cluster_constants(self):
        assert self.h.cluster_id == 0x0090
        assert self.h.cluster_name == "ElectricalPowerMeasurement"

    def test_is_not_primary(self):
        assert self.h.is_primary_for(None, None) is False

    def test_create_indigo_devices_returns_empty(self):
        assert self.h.create_indigo_devices(None, None) == []

    def test_attributes_to_subscribe(self):
        assert self.h.attributes_to_subscribe() == [0x0008]

    def test_handle_indigo_action_returns_none(self):
        assert self.h.handle_indigo_action(None, None) is None

    def test_on_attribute_update_converts_mw_to_watts(self):
        dev = _Dev(states={"curEnergyLevel": 0.0})
        result = self.h.on_attribute_update(dev, 0x0008, 1500)
        assert result == {"curEnergyLevel": 1.5}

    def test_on_attribute_update_rounds_to_one_decimal(self):
        dev = _Dev(states={"curEnergyLevel": 0.0})
        result = self.h.on_attribute_update(dev, 0x0008, 1234)
        assert result == {"curEnergyLevel": 1.2}

    def test_on_attribute_update_zero_watts(self):
        dev = _Dev(states={"curEnergyLevel": 0.0})
        result = self.h.on_attribute_update(dev, 0x0008, 0)
        assert result == {"curEnergyLevel": 0.0}

    def test_on_attribute_update_null_value_returns_empty(self):
        dev = _Dev(states={"curEnergyLevel": 0.0})
        result = self.h.on_attribute_update(dev, 0x0008, None)
        assert result == {}

    def test_on_attribute_update_missing_state_guard(self):
        """If SupportsPowerMeter was not set at creation, state won't exist."""
        dev = _Dev(states={})  # curEnergyLevel not present
        result = self.h.on_attribute_update(dev, 0x0008, 1500)
        assert result == {}

    def test_on_attribute_update_wrong_attribute_id(self):
        dev = _Dev(states={"curEnergyLevel": 0.0})
        result = self.h.on_attribute_update(dev, 0x0004, 230_000)
        assert result == {}


# ---------------------------------------------------------------------------
# ElectricalEnergyHandler tests
# ---------------------------------------------------------------------------

class TestElectricalEnergyHandler:
    def setup_method(self):
        self.h = ElectricalEnergyHandler()

    def test_cluster_constants(self):
        assert self.h.cluster_id == 0x0091
        assert self.h.cluster_name == "ElectricalEnergyMeasurement"

    def test_is_not_primary(self):
        assert self.h.is_primary_for(None, None) is False

    def test_create_indigo_devices_returns_empty(self):
        assert self.h.create_indigo_devices(None, None) == []

    def test_attributes_to_subscribe(self):
        assert self.h.attributes_to_subscribe() == [0x0001]

    def test_handle_indigo_action_returns_none(self):
        assert self.h.handle_indigo_action(None, None) is None

    def test_on_attribute_update_string_keyed_struct_to_kwh(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        result = self.h.on_attribute_update(dev, 0x0001, {"energy": 3_600_000_000})
        assert result == {"accumEnergyTotal": 3600.0}

    def test_on_attribute_update_tag_keyed_struct_to_kwh(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        result = self.h.on_attribute_update(dev, 0x0001, {"0": 1_800_000_000})
        assert result == {"accumEnergyTotal": 1800.0}

    def test_on_attribute_update_bare_number_to_kwh(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        result = self.h.on_attribute_update(dev, 0x0001, 1_000_000)
        assert result == {"accumEnergyTotal": 1.0}

    def test_on_attribute_update_rounds_to_three_decimals(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        # 1500 mWh = 0.0015 kWh
        result = self.h.on_attribute_update(dev, 0x0001, {"energy": 1500})
        assert result == {"accumEnergyTotal": 0.002}  # round(0.0015, 3) = 0.002

    def test_on_attribute_update_null_struct_returns_empty(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        result = self.h.on_attribute_update(dev, 0x0001, None)
        assert result == {}

    def test_on_attribute_update_garbage_struct_returns_empty(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        result = self.h.on_attribute_update(dev, 0x0001, "bad value")
        assert result == {}

    def test_on_attribute_update_missing_state_guard(self):
        """If SupportsEnergyMeter was not set at creation, state won't exist."""
        dev = _Dev(states={})  # accumEnergyTotal not present
        result = self.h.on_attribute_update(dev, 0x0001, {"energy": 1_000_000})
        assert result == {}

    def test_on_attribute_update_wrong_attribute_id(self):
        dev = _Dev(states={"accumEnergyTotal": 0.0})
        result = self.h.on_attribute_update(dev, 0x0002, {"energy": 1_000_000})
        assert result == {}


# ---------------------------------------------------------------------------
# Props decoration on relay (OnOffHandler) specs
# ---------------------------------------------------------------------------

class TestOnOffEnergyProps:
    def _make_ep(self, clusters: set) -> _Endpoint:
        return _Endpoint(1, clusters)

    def _make_node(self) -> _Node:
        return _Node()

    def test_relay_spec_has_power_and_energy_props_when_both_clusters_present(self):
        ep = self._make_ep({0x0006, CLUSTER_ELECTRICAL_POWER, CLUSTER_ELECTRICAL_ENERGY})
        specs = OnOffHandler().create_indigo_devices(self._make_node(), ep)
        assert len(specs) == 1
        props = specs[0].props
        assert props.get("SupportsPowerMeter") is True
        assert props.get("SupportsEnergyMeter") is True

    def test_relay_spec_has_only_power_prop_when_only_power_cluster_present(self):
        ep = self._make_ep({0x0006, CLUSTER_ELECTRICAL_POWER})
        specs = OnOffHandler().create_indigo_devices(self._make_node(), ep)
        props = specs[0].props
        assert props.get("SupportsPowerMeter") is True
        assert "SupportsEnergyMeter" not in props

    def test_relay_spec_has_only_energy_prop_when_only_energy_cluster_present(self):
        ep = self._make_ep({0x0006, CLUSTER_ELECTRICAL_ENERGY})
        specs = OnOffHandler().create_indigo_devices(self._make_node(), ep)
        props = specs[0].props
        assert "SupportsPowerMeter" not in props
        assert props.get("SupportsEnergyMeter") is True

    def test_relay_spec_has_no_energy_props_without_energy_clusters(self):
        ep = self._make_ep({0x0006})
        specs = OnOffHandler().create_indigo_devices(self._make_node(), ep)
        props = specs[0].props
        assert "SupportsPowerMeter" not in props
        assert "SupportsEnergyMeter" not in props

    def test_relay_is_not_primary_when_level_control_present(self):
        """OnOffHandler still defers to LevelControl even with energy clusters."""
        ep = self._make_ep({0x0006, 0x0008, CLUSTER_ELECTRICAL_POWER})
        handler = OnOffHandler()
        assert handler.is_primary_for(self._make_node(), ep) is False
        # create_indigo_devices short-circuits when not primary
        specs = handler.create_indigo_devices(self._make_node(), ep)
        assert specs == []


# ---------------------------------------------------------------------------
# Props decoration on dimmer (LevelControlHandler) specs
# ---------------------------------------------------------------------------

class TestLevelControlEnergyProps:
    def _make_ep(self, clusters: set) -> _Endpoint:
        return _Endpoint(1, clusters)

    def _make_node(self) -> _Node:
        return _Node()

    def test_dimmer_spec_has_power_and_energy_props_when_both_clusters_present(self):
        ep = self._make_ep({0x0006, 0x0008, CLUSTER_ELECTRICAL_POWER, CLUSTER_ELECTRICAL_ENERGY})
        specs = LevelControlHandler().create_indigo_devices(self._make_node(), ep)
        assert len(specs) == 1
        props = specs[0].props
        assert props.get("SupportsPowerMeter") is True
        assert props.get("SupportsEnergyMeter") is True

    def test_dimmer_spec_has_only_power_prop_when_only_power_cluster_present(self):
        ep = self._make_ep({0x0006, 0x0008, CLUSTER_ELECTRICAL_POWER})
        specs = LevelControlHandler().create_indigo_devices(self._make_node(), ep)
        props = specs[0].props
        assert props.get("SupportsPowerMeter") is True
        assert "SupportsEnergyMeter" not in props

    def test_dimmer_spec_has_no_energy_props_without_energy_clusters(self):
        ep = self._make_ep({0x0006, 0x0008})
        specs = LevelControlHandler().create_indigo_devices(self._make_node(), ep)
        props = specs[0].props
        assert "SupportsPowerMeter" not in props
        assert "SupportsEnergyMeter" not in props

    def test_dimmer_is_not_primary_when_color_control_present(self):
        """LevelControlHandler defers to ColorControlHandler when 0x0300 present."""
        ep = self._make_ep({0x0006, 0x0008, 0x0300, CLUSTER_ELECTRICAL_POWER})
        handler = LevelControlHandler()
        assert handler.is_primary_for(self._make_node(), ep) is False
        specs = handler.create_indigo_devices(self._make_node(), ep)
        assert specs == []


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------

def test_registry_includes_electrical_handlers():
    reg = HandlerRegistry()
    power_h = reg.handler_for_cluster(CLUSTER_ELECTRICAL_POWER)
    energy_h = reg.handler_for_cluster(CLUSTER_ELECTRICAL_ENERGY)
    assert isinstance(power_h, ElectricalPowerHandler)
    assert isinstance(energy_h, ElectricalEnergyHandler)


def test_registry_electrical_handlers_are_non_primary():
    """Non-primary handlers should produce no specs via handlers_for_endpoint."""
    ep = _Endpoint(1, {CLUSTER_ELECTRICAL_POWER, CLUSTER_ELECTRICAL_ENERGY})
    node = _Node()
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, ep)
    # Neither handler is primary, so no specs created by electrical cluster alone
    assert all(s.device_type_id not in ("", None) for s in specs)
    device_types = [s.device_type_id for s in specs]
    # No electrical-specific device type ids should appear
    assert "matterPowerMeter" not in device_types
    assert "matterEnergyMeter" not in device_types
