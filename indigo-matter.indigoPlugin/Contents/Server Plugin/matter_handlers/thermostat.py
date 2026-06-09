"""Thermostat cluster (0x0201) + FanControl (0x0202) → Indigo Thermostat.

Thermostat setpoints and modes are set by *writing attributes* (not invoking
commands): OccupiedHeatingSetpoint / OccupiedCoolingSetpoint / SystemMode, and
FanControl's FanMode. So this handler's actions return :class:`MatterWrite`.

Temperatures are Matter's int16 0.01 °C; Indigo values are treated as Celsius
(matter-server reports °C). FanControl, when present on the endpoint, is merged
into the same Indigo thermostat device.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterAction, MatterWrite

CLUSTER_THERMOSTAT = 0x0201
CLUSTER_FAN_CONTROL = 0x0202

# Thermostat attribute ids
ATTR_LOCAL_TEMPERATURE = 0x0000
ATTR_OCCUPIED_COOLING_SETPOINT = 0x0011
ATTR_OCCUPIED_HEATING_SETPOINT = 0x0012
ATTR_SYSTEM_MODE = 0x001C
ATTR_RUNNING_STATE = 0x0029

# Matter SystemMode enum
SYS_OFF, SYS_AUTO, SYS_COOL, SYS_HEAT = 0, 1, 3, 4
# Matter FanControl FanMode enum
FAN_ON, FAN_AUTO = 4, 5


def _c(value: Any) -> float:
    """Matter 0.01 °C → Celsius."""
    return round(int(value) / 100.0, 1)


def _centi(celsius: float) -> int:
    """Celsius → Matter 0.01 °C int16."""
    return int(round(float(celsius) * 100))


class ThermostatHandler(ClusterHandler):
    cluster_id = CLUSTER_THERMOSTAT
    cluster_name = "Thermostat"
    device_type_id = "matterThermostat"

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
                    "NumTemperatureInputs": "1",
                    "NumHumidityInputs": "0",
                    "SupportsHeatSetpoint": "true",
                    "SupportsCoolSetpoint": "true",
                    "SupportsHvacOperationMode": "true",
                    "SupportsHvacFanMode": "true" if endpoint.has(CLUSTER_FAN_CONTROL) else "false",
                    "ShowCoolHeatEquipmentStateUI": "true",
                },
                initial_states={},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [ATTR_LOCAL_TEMPERATURE, ATTR_OCCUPIED_COOLING_SETPOINT,
                ATTR_OCCUPIED_HEATING_SETPOINT, ATTR_SYSTEM_MODE, ATTR_RUNNING_STATE]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if value is None:
            return {}
        if attribute_id == ATTR_LOCAL_TEMPERATURE:
            return {"temperatureInput1": _c(value)}
        if attribute_id == ATTR_OCCUPIED_HEATING_SETPOINT:
            return {"setpointHeat": _c(value)}
        if attribute_id == ATTR_OCCUPIED_COOLING_SETPOINT:
            return {"setpointCool": _c(value)}
        if attribute_id == ATTR_SYSTEM_MODE:
            return {"hvacOperationMode": self._matter_to_indigo_mode(int(value))}
        if attribute_id == ATTR_RUNNING_STATE:
            running = int(value)
            return {"hvacHeaterIsOn": bool(running & 0x01), "hvacCoolerIsOn": bool(running & 0x02)}
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        ta = action.thermostatAction
        act = indigo.kThermostatAction

        if ta == act.SetHeatSetpoint:
            return self._write_heat(node_id, endpoint_id, action.actionValue)
        if ta == act.SetCoolSetpoint:
            return self._write_cool(node_id, endpoint_id, action.actionValue)
        if ta == act.IncreaseHeatSetpoint:
            return self._write_heat(node_id, endpoint_id, indigo_dev.heatSetpoint + action.actionValue)
        if ta == act.DecreaseHeatSetpoint:
            return self._write_heat(node_id, endpoint_id, indigo_dev.heatSetpoint - action.actionValue)
        if ta == act.IncreaseCoolSetpoint:
            return self._write_cool(node_id, endpoint_id, indigo_dev.coolSetpoint + action.actionValue)
        if ta == act.DecreaseCoolSetpoint:
            return self._write_cool(node_id, endpoint_id, indigo_dev.coolSetpoint - action.actionValue)
        if ta == act.SetHvacMode:
            return MatterWrite(node_id, endpoint_id, CLUSTER_THERMOSTAT, ATTR_SYSTEM_MODE,
                               self._indigo_to_matter_mode(action.actionMode))
        if ta == act.SetFanMode:
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL, 0x0000,
                               self._indigo_to_matter_fan(action.actionMode))
        return None

    def _write_heat(self, node_id: int, endpoint_id: int, celsius: float) -> MatterWrite:
        return MatterWrite(node_id, endpoint_id, CLUSTER_THERMOSTAT, ATTR_OCCUPIED_HEATING_SETPOINT, _centi(celsius))

    def _write_cool(self, node_id: int, endpoint_id: int, celsius: float) -> MatterWrite:
        return MatterWrite(node_id, endpoint_id, CLUSTER_THERMOSTAT, ATTR_OCCUPIED_COOLING_SETPOINT, _centi(celsius))

    @staticmethod
    def _matter_to_indigo_mode(mode: int):
        import indigo
        return {
            SYS_OFF: indigo.kHvacMode.Off,
            SYS_AUTO: indigo.kHvacMode.HeatCool,
            SYS_COOL: indigo.kHvacMode.Cool,
            SYS_HEAT: indigo.kHvacMode.Heat,
        }.get(mode, indigo.kHvacMode.Off)

    @staticmethod
    def _indigo_to_matter_mode(mode) -> int:
        import indigo
        return {
            indigo.kHvacMode.Off: SYS_OFF,
            indigo.kHvacMode.Heat: SYS_HEAT,
            indigo.kHvacMode.Cool: SYS_COOL,
            indigo.kHvacMode.HeatCool: SYS_AUTO,
            indigo.kHvacMode.ProgramHeat: SYS_HEAT,
            indigo.kHvacMode.ProgramCool: SYS_COOL,
            indigo.kHvacMode.ProgramHeatCool: SYS_AUTO,
        }.get(mode, SYS_OFF)

    @staticmethod
    def _indigo_to_matter_fan(mode) -> int:
        import indigo
        return FAN_AUTO if mode == indigo.kFanMode.Auto else FAN_ON


class FanControlHandler(ClusterHandler):
    """FanControl (0x0202) merged into the thermostat device.

    Creates no device of its own (is_primary_for → False); it only handles the
    FanMode attribute update, mapping it onto the thermostat's hvacFanMode. Fan
    *actions* are dispatched through ThermostatHandler (the matterThermostat
    device's action handler).
    """
    cluster_id = CLUSTER_FAN_CONTROL
    cluster_name = "FanControl"

    ATTR_FAN_MODE = 0x0000

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        return False

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        return []

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_FAN_MODE]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == self.ATTR_FAN_MODE and value is not None:
            import indigo
            mode = indigo.kFanMode.Auto if int(value) == FAN_AUTO else indigo.kFanMode.AlwaysOn
            return {"hvacFanMode": mode}
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        return None  # fan actions go through ThermostatHandler
