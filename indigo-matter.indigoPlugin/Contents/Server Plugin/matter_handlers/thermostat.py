"""Thermostat cluster (0x0201) + FanControl (0x0202) → Indigo Thermostat / Fan.

Thermostat setpoints and modes are set by *writing attributes* (not invoking
commands): OccupiedHeatingSetpoint / OccupiedCoolingSetpoint / SystemMode, and
FanControl's FanMode. So this handler's actions return :class:`MatterWrite`.

Temperatures are Matter's int16 0.01 °C; Indigo values are treated as Celsius
(matter-server reports °C). FanControl, when present on the endpoint, is merged
into the same Indigo thermostat device. When FanControl is the *only* cluster
on an endpoint (no Thermostat), FanControlHandler becomes primary and creates a
standalone ``matterFan`` dimmer device (brightness = fan speed %).
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

# FanControl attribute ids (cluster 0x0202)
ATTR_FAN_MODE = 0x0000       # FanMode: Off=0, On=4, Auto=5 (and others)
ATTR_PERCENT_SETTING = 0x0002  # PercentSetting: 0–100, nullable — write target speed
ATTR_PERCENT_CURRENT = 0x0003  # PercentCurrent: 0–100, nullable — actual speed


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
    """FanControl (0x0202) — merged-into-thermostat *or* standalone fan.

    **Co-located with Thermostat (0x0201):** ``is_primary_for`` returns False;
    the handler only maps FanMode updates onto the thermostat's ``hvacFanMode``
    state. Fan *actions* on a matterThermostat are dispatched through
    ThermostatHandler — unchanged behaviour.

    **Standalone (no Thermostat on endpoint):** ``is_primary_for`` returns True;
    creates a ``matterFan`` dimmer device. Brightness maps to fan speed percent
    (PercentSetting write / PercentCurrent read); on/off maps to FanMode On/Off.
    Actions use attribute writes exactly like thermostat setpoints.
    """
    cluster_id = CLUSTER_FAN_CONTROL
    cluster_name = "FanControl"
    #: Indigo deviceTypeId for standalone fan endpoints.
    device_type_id = "matterFan"

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        # Primary (standalone fan) only when no Thermostat cluster is co-located.
        # When Thermostat is present the fan is merged into the matterThermostat device.
        return not endpoint.has(CLUSTER_THERMOSTAT)

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        if not self.is_primary_for(node, endpoint):
            return []
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
                initial_states={"onOffState": False, "brightnessLevel": 0},
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        # matter-server's start_listening is a full-node firehose — this list is
        # documentation of interest, not a wire subscription filter.  When this
        # handler is co-located with Thermostat, on_attribute_update() only acts
        # on ATTR_FAN_MODE; ATTR_PERCENT_SETTING / ATTR_PERCENT_CURRENT updates
        # fall through to {} harmlessly, so the extra entries cause no harm.
        return [ATTR_FAN_MODE, ATTR_PERCENT_SETTING, ATTR_PERCENT_CURRENT]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        # When the device is a matterThermostat (co-located fan), apply the
        # existing hvacFanMode mapping.
        if getattr(indigo_dev, "deviceTypeId", None) == "matterThermostat":
            if attribute_id == ATTR_FAN_MODE and value is not None:
                import indigo
                mode = indigo.kFanMode.Auto if int(value) == FAN_AUTO else indigo.kFanMode.AlwaysOn
                return {"hvacFanMode": mode}
            return {}

        # Standalone matterFan device — only act when deviceTypeId is explicitly
        # "matterFan"; any other type (including None) is a safe no-op.
        if getattr(indigo_dev, "deviceTypeId", None) != "matterFan":
            return {}

        if attribute_id == ATTR_FAN_MODE:
            if value is None:
                return {}
            # FanMode Off (0) → device off; any other value → device on.
            return {"onOffState": int(value) != 0}
        if attribute_id == ATTR_PERCENT_CURRENT:
            if value is None:
                return {}
            return {"brightnessLevel": int(value)}
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterAction]:
        # Co-located fan actions are dispatched through ThermostatHandler.
        if getattr(indigo_dev, "deviceTypeId", None) == "matterThermostat":
            return None

        # Standalone matterFan: dimmer-style actions, attribute-write controlled.
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        device_action = action.deviceAction

        if device_action == indigo.kDeviceAction.TurnOn:
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL,
                               ATTR_FAN_MODE, FAN_ON)
        if device_action == indigo.kDeviceAction.TurnOff:
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL,
                               ATTR_FAN_MODE, 0)
        if device_action == indigo.kDeviceAction.Toggle:
            # Toggle: flip based on current onOffState.
            on = getattr(indigo_dev, "onState", False)
            new_mode = 0 if on else FAN_ON
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL,
                               ATTR_FAN_MODE, new_mode)
        if device_action == indigo.kDeviceAction.SetBrightness:
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL,
                               ATTR_PERCENT_SETTING, int(action.actionValue))
        if device_action == indigo.kDeviceAction.BrightenBy:
            current = getattr(indigo_dev, "brightness", 0) or 0
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL,
                               ATTR_PERCENT_SETTING, min(int(current + action.actionValue), 100))
        if device_action == indigo.kDeviceAction.DimBy:
            current = getattr(indigo_dev, "brightness", 0) or 0
            return MatterWrite(node_id, endpoint_id, CLUSTER_FAN_CONTROL,
                               ATTR_PERCENT_SETTING, max(int(current - action.actionValue), 0))
        return None
