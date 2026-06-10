"""M7: Thermostat (0x0201) + FanControl (0x0202) handlers (attribute writes)."""
from __future__ import annotations

from types import SimpleNamespace

from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry
from matter_handlers.thermostat import ThermostatHandler, FanControlHandler
from protocol import MatterWrite

# thermostat endpoint with Thermostat (513) + FanControl (514)
THERMOSTAT_NODE = {
    "node_id": 30,
    "attributes": {
        "1/513/0": 2050,    # LocalTemperature = 20.50 C
        "1/513/18": 2100,   # OccupiedHeatingSetpoint (0x12) = 21.00 C
        "1/513/17": 2400,   # OccupiedCoolingSetpoint (0x11) = 24.00 C
        "1/513/28": 4,      # SystemMode (0x1C) = Heat
        "1/514/0": 5,       # FanControl FanMode = Auto
        "1/29/0": [{"0": 769}],  # Thermostat device type
    },
}


def _ep(node, eid):
    return next(e for e in node.endpoints if e.endpoint_id == eid)


def test_thermostat_endpoint_makes_one_thermostat_device():
    node = parse_node(THERMOSTAT_NODE, "Hall Stat")
    specs = HandlerRegistry().handlers_for_endpoint(node, _ep(node, 1))
    # Thermostat owns the endpoint; FanControl merges (no extra device)
    assert [s.device_type_id for s in specs] == ["matterThermostat"]
    assert specs[0].props["SupportsHvacFanMode"] == "true"


def test_registry_cluster_routing():
    reg = HandlerRegistry()
    assert isinstance(reg.handler_for_cluster(0x0201), ThermostatHandler)
    assert isinstance(reg.handler_for_cluster(0x0202), FanControlHandler)


def test_thermostat_attribute_updates(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    assert h.on_attribute_update(None, 0x0000, 2050) == {"temperatureInput1": 20.5}
    assert h.on_attribute_update(None, 0x0012, 2100) == {"setpointHeat": 21.0}
    assert h.on_attribute_update(None, 0x0011, 2400) == {"setpointCool": 24.0}
    assert h.on_attribute_update(None, 0x001C, 4) == {"hvacOperationMode": indigo.kHvacMode.Heat}
    running = h.on_attribute_update(None, 0x0029, 0x01)
    assert running == {"hvacHeaterIsOn": True, "hvacCoolerIsOn": False}


def test_fan_attribute_update(mock_indigo_base):
    import indigo
    h = FanControlHandler()
    # When the target device is a matterThermostat (co-located fan), FanMode maps
    # to hvacFanMode — the merged-thermostat path.
    # Note: stub uses deviceTypeId="matterThermostat" (not None) to exercise the
    # thermostat-merge branch; this reflects the new deviceTypeId dispatch introduced
    # in the standalone-fan refactor (behavioural change, not just a mechanical stub fix).
    thermo_dev = SimpleNamespace(deviceTypeId="matterThermostat")
    assert h.on_attribute_update(thermo_dev, 0x0000, 5) == {"hvacFanMode": indigo.kFanMode.Auto}
    assert h.on_attribute_update(thermo_dev, 0x0000, 4) == {"hvacFanMode": indigo.kFanMode.AlwaysOn}


def _dev():
    return SimpleNamespace(pluginProps={"nodeId": "30", "endpointId": "1"},
                           heatSetpoint=20.0, coolSetpoint=24.0)


def test_set_heat_setpoint_writes_attribute(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    w = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.SetHeatSetpoint, actionValue=21.5))
    assert isinstance(w, MatterWrite)
    assert (w.cluster, w.attribute, w.value) == (0x0201, 0x0012, 2150)


def test_set_cool_setpoint_writes_attribute(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    w = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.SetCoolSetpoint, actionValue=23.0))
    assert (w.cluster, w.attribute, w.value) == (0x0201, 0x0011, 2300)


def test_increase_heat_setpoint_uses_current(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    w = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.IncreaseHeatSetpoint, actionValue=1.0))
    assert w.value == 2100  # 20.0 + 1.0 = 21.0 C


def test_set_hvac_mode_writes_system_mode(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    w = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.SetHvacMode, actionMode=indigo.kHvacMode.Cool))
    assert (w.cluster, w.attribute, w.value) == (0x0201, 0x001C, 3)  # Cool=3


def test_set_fan_mode_writes_fancontrol(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    w = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.SetFanMode, actionMode=indigo.kFanMode.Auto))
    assert (w.cluster, w.attribute, w.value) == (0x0202, 0x0000, 5)  # FanControl FanMode Auto=5


def test_decrease_setpoints(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    dec_h = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.DecreaseHeatSetpoint, actionValue=1.5))
    assert (dec_h.cluster, dec_h.attribute, dec_h.value) == (0x0201, 0x0012, 1850)  # 20.0-1.5
    dec_c = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.DecreaseCoolSetpoint, actionValue=2.0))
    assert (dec_c.cluster, dec_c.attribute, dec_c.value) == (0x0201, 0x0011, 2200)  # 24.0-2.0


def test_system_mode_mapping_and_unknown_fallback(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    assert h.on_attribute_update(None, 0x001C, 0) == {"hvacOperationMode": indigo.kHvacMode.Off}
    assert h.on_attribute_update(None, 0x001C, 1) == {"hvacOperationMode": indigo.kHvacMode.HeatCool}
    assert h.on_attribute_update(None, 0x001C, 3) == {"hvacOperationMode": indigo.kHvacMode.Cool}
    # an unknown SystemMode must fall back to Off, not raise
    assert h.on_attribute_update(None, 0x001C, 99) == {"hvacOperationMode": indigo.kHvacMode.Off}


def test_set_hvac_mode_auto_writes_system_mode(mock_indigo_base):
    import indigo
    h = ThermostatHandler()
    w = h.handle_indigo_action(_dev(), SimpleNamespace(
        thermostatAction=indigo.kThermostatAction.SetHvacMode, actionMode=indigo.kHvacMode.HeatCool))
    assert (w.cluster, w.attribute, w.value) == (0x0201, 0x001C, 1)  # SYS_AUTO
