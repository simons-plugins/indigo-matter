"""Device-zoo contract harness (issue #58).

Structural invariants checked over a zoo of cluster combinations, run through
the REAL handler registry — including combinations no validated device has
shipped yet (a Matter fan mandates OnOff alongside FanControl since 1.2, some
thermostats/locks expose OnOff, etc.). The point is the *invariants*, not the
individual expectations: when a strange device shows up in the wild, add its
cluster set to ZOO and every invariant runs over it automatically.

Invariants:
  1. the endpoint maps to exactly the expected device types;
  2. at most ONE actuator device per endpoint (the duplicate-relay class);
  3. every spec's device_type_id exists in Devices.xml;
  4. every initial state is XML-declared or an Indigo built-in;
  5. every sensor-type spec carries explicit SupportsOnState /
     SupportsSensorValue with exactly one True (the issue #56 class).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from matter_handlers.registry import HandlerRegistry
from matter_model import parse_node

DEVICES_XML = (
    Path(__file__).parent.parent
    / "indigo-matter.indigoPlugin" / "Contents" / "Server Plugin" / "Devices.xml"
)
_DEVICE_ELEMENTS = ET.parse(DEVICES_XML).findall("Device")
XML_TYPE_BY_ID = {d.get("id"): d.get("type") for d in _DEVICE_ELEMENTS}
XML_STATES_BY_ID = {
    d.get("id"): {s.get("id") for s in d.findall("./States/State")}
    for d in _DEVICE_ELEMENTS
}
XML_UI_DISPLAY_BY_ID = {
    d.get("id"): (d.findtext("UiDisplayStateId") or "").strip()
    for d in _DEVICE_ELEMENTS
}

# Indigo built-in states implied by the device type / Supports* props
# (SDK: Example Device - Sensor and Example Device - Relay and Dimmer).
BUILTIN_STATES = {
    "onOffState", "brightnessLevel", "sensorValue", "batteryLevel",
    "curEnergyLevel", "accumEnergyTotal",
}

# Device types that *control* the physical endpoint. Two of these for one
# endpoint means two Indigo devices fighting over one piece of hardware.
ACTUATOR_TYPES = {
    "matterRelay", "matterDimmer", "matterColorDimmer", "matterFan",
    "matterThermostat", "matterWindowCovering", "matterLock", "matterValve",
}



def _raw(node_id, eps):
    """Build a raw matter-server node dict from {ep: {"cluster/attr": value}}."""
    attributes = {}
    for ep, attrs in eps.items():
        for suffix, value in attrs.items():
            attributes[f"{ep}/{suffix}"] = value
    return {"node_id": node_id, "available": True, "attributes": attributes}


# name → (raw node, {endpoint: sorted expected device_type_ids})
ZOO = {
    "plug": (
        _raw(1, {1: {"29/0": [{"0": 266}], "6/0": False}}),
        {1: ["matterRelay"]},
    ),
    "energy_plug": (
        _raw(2, {1: {"29/0": [{"0": 266}], "6/0": False,
                     "144/8": 1200, "145/1": {"energy": 1}}}),
        {1: ["matterRelay"]},
    ),
    "dimmer": (
        _raw(3, {1: {"29/0": [{"0": 257}], "6/0": False, "8/0": 128}}),
        {1: ["matterDimmer"]},
    ),
    "color_light": (
        _raw(4, {1: {"29/0": [{"0": 269}], "6/0": False, "8/0": 128, "768/0": 0}}),
        {1: ["matterColorDimmer"]},
    ),
    # ColorControl without LevelControl — colour handler still owns the endpoint.
    "color_no_level": (
        _raw(5, {1: {"29/0": [{"0": 269}], "6/0": False, "768/0": 0}}),
        {1: ["matterColorDimmer"]},
    ),
    # Matter 1.2+ Fan device type mandates OnOff + FanControl: one fan, NOT
    # fan + duplicate relay (issue #58).
    "fan_with_onoff": (
        _raw(6, {1: {"29/0": [{"0": 43}], "6/0": True, "514/0": 2}}),
        {1: ["matterFan"]},
    ),
    "thermostat_with_onoff": (
        _raw(7, {1: {"29/0": [{"0": 769}], "6/0": True, "513/0": 2150}}),
        {1: ["matterThermostat"]},
    ),
    # FanControl co-located with Thermostat merges into the thermostat device.
    "thermostat_with_fan": (
        _raw(8, {1: {"29/0": [{"0": 769}], "513/0": 2150, "514/0": 2}}),
        {1: ["matterThermostat"]},
    ),
    "covering_with_onoff_and_level": (
        _raw(9, {1: {"29/0": [{"0": 514}], "6/0": True, "8/0": 10, "258/0": 0}}),
        {1: ["matterWindowCovering"]},
    ),
    "lock_with_onoff": (
        _raw(10, {1: {"29/0": [{"0": 10}], "6/0": False, "257/0": 1}}),
        {1: ["matterLock"]},
    ),
    "valve_with_onoff": (
        _raw(11, {1: {"29/0": [{"0": 66}], "6/0": False, "129/0": None}}),
        {1: ["matterValve"]},
    ),
    "climate_sensor": (
        _raw(12, {1: {"29/0": [{"0": 770}], "1026/0": 2185, "1029/0": 4750}}),
        {1: ["matterHumiditySensor", "matterTemperatureSensor"]},
    ),
    "presence_lux_sensor": (
        _raw(13, {1: {"29/0": [{"0": 263}], "1030/0": 1, "1024/0": 10000}}),
        {1: ["matterIlluminanceSensor", "matterMotionSensor"]},
    ),
    "contact_sensor": (
        _raw(14, {1: {"29/0": [{"0": 21}], "69/0": True}}),
        {1: ["matterContactSensor"]},
    ),
    "smoke_alarm": (
        _raw(15, {1: {"29/0": [{"0": 118}], "92/0": 0}}),
        {1: ["matterSmokeCOAlarm"]},
    ),
    "air_quality_station": (
        _raw(16, {1: {"29/0": [{"0": 44}], "91/0": 1, "1037/0": 400.0,
                      "1066/0": 5.0, "1070/0": 100.0}}),
        {1: ["matterAirQualitySensor", "matterCO2Sensor",
             "matterPM25Sensor", "matterTVOCSensor"]},
    ),
    "button": (
        _raw(17, {1: {"29/0": [{"0": 15}], "59/1": 0}}),
        {1: ["matterButton"]},
    ),
    "pressure_flow_sensor": (
        _raw(18, {1: {"29/0": [{"0": 773}], "1027/0": 1013, "1028/0": 25}}),
        {1: ["matterFlowSensor", "matterPressureSensor"]},
    ),
    # IKEA ALPSTUGA (real Matter-over-Thread air-quality sensor): AirQuality +
    # CO2 + PM2.5 + temperature + humidity, plus an OnOff cluster that toggles
    # the front DISPLAY (a "dead front" control), not power. CHARACTERISES the
    # issue #64 wrinkle: the display OnOff produces a generic matterRelay
    # alongside the five sensors. Kept in `expected` deliberately — when #64 is
    # fixed (OnOff with the DeadFront feature should not become a relay) the
    # matterRelay entry is removed here in the same change. The at-most-one-
    # actuator invariant still holds (one relay), so this stays a phantom-device
    # note, not a duplicate-actuator failure.
    "ikea_alpstuga": (
        _raw(0x99, {1: {"29/0": [{"0": 44}], "91/0": 1, "1037/0": 600.0,
                        "1066/0": 8.0, "1026/0": 2150, "1029/0": 4800,
                        "6/0": False}}),
        {1: ["matterAirQualitySensor", "matterCO2Sensor", "matterHumiditySensor",
             "matterPM25Sensor", "matterRelay", "matterTemperatureSensor"]},
    ),
    # An endpoint made only of clusters we don't handle (RVC RunMode 0x0054):
    # the registry yields nothing — device_sync's matterUnknown fallback takes
    # over (covered in test_device_sync.py).
    "unknown_only": (
        _raw(19, {1: {"29/0": [{"0": 116}], "84/0": 0}}),
        {1: []},
    ),
    # IKEA GRILLPLATS-shaped split-endpoint energy (issue #79): the relay
    # lives on ep1, ElectricalPower/Energy/PowerTopology on a dedicated ep2
    # (Matter 1.3 "Electrical Sensor" device type 0x0510). The registry has no
    # DeviceSync, so it only sees per-endpoint cluster→handler mapping — ep2's
    # electrical clusters are merge-only and produce no primary spec of their
    # own; the meter-link resolution that attributes them to ep1 is a
    # DeviceSync concern, asserted in test_device_sync.py instead.
    "grillplats_split_energy": (
        _raw(0x34, {1: {"29/0": [{"0": 266}], "3/0": 0, "4/0": 0, "6/0": False},
                    2: {"29/0": [{"0": 0x0510}], "144/8": 1200,
                        "145/1": {"energy": 1}, "156/65532": 1}}),
        {1: ["matterRelay"], 2: []},
    ),
    # A pump-shaped endpoint (issue #81): OnOff + an extra cluster (0x0200)
    # this plugin has no handler for at all. The registry still maps OnOff to
    # a relay — the "leftover cluster" diagnostic that flags 0x0200 is a
    # DeviceSync concern (asserted in test_device_sync.py), not visible here.
    "mixed_unmapped": (
        _raw(0x9B, {1: {"29/0": [{"0": 266}], "6/0": False, "512/0": 0}}),
        {1: ["matterRelay"]},
    ),
}


def _specs_by_endpoint(raw):
    node = parse_node(raw)
    registry = HandlerRegistry()
    return {
        ep.endpoint_id: registry.handlers_for_endpoint(node, ep)
        for ep in node.endpoints
    }


@pytest.mark.parametrize("name", ZOO)
def test_zoo_maps_to_expected_device_types(name):
    raw, expected = ZOO[name]
    specs = _specs_by_endpoint(raw)
    actual = {ep: sorted(s.device_type_id for s in eps) for ep, eps in specs.items()}
    for ep, types in expected.items():
        assert actual.get(ep, []) == types, f"{name} ep{ep}: {actual.get(ep)}"


@pytest.mark.parametrize("name", ZOO)
def test_zoo_at_most_one_actuator_per_endpoint(name):
    raw, _ = ZOO[name]
    for ep, specs in _specs_by_endpoint(raw).items():
        actuators = [s.device_type_id for s in specs if s.device_type_id in ACTUATOR_TYPES]
        assert len(actuators) <= 1, (
            f"{name} ep{ep}: {actuators} would fight over one physical device"
        )


@pytest.mark.parametrize("name", ZOO)
def test_zoo_spec_types_exist_in_devices_xml(name):
    raw, _ = ZOO[name]
    for specs in _specs_by_endpoint(raw).values():
        for spec in specs:
            assert spec.device_type_id in XML_TYPE_BY_ID, (
                f"{name}: {spec.device_type_id} not declared in Devices.xml"
            )


@pytest.mark.parametrize("name", ZOO)
def test_zoo_initial_states_are_declared_or_builtin(name):
    raw, _ = ZOO[name]
    for specs in _specs_by_endpoint(raw).values():
        for spec in specs:
            allowed = XML_STATES_BY_ID.get(spec.device_type_id, set()) | BUILTIN_STATES
            unknown = set(spec.initial_states) - allowed
            assert not unknown, (
                f"{name}: {spec.device_type_id} seeds undeclared states {unknown}"
            )


@pytest.mark.parametrize("name", ZOO)
def test_zoo_sensor_specs_carry_explicit_display_props(name):
    # The issue #56 class: Indigo derives a sensor's list display from these
    # creation props. Precedence (verified live on jarvis): a True Supports*
    # wins; with BOTH explicitly False, Devices.xml <UiDisplayStateId> applies
    # — so (False, False) is only legal when the XML declares one that points
    # at an XML-declared custom state.
    raw, _ = ZOO[name]
    for specs in _specs_by_endpoint(raw).values():
        for spec in specs:
            if XML_TYPE_BY_ID.get(spec.device_type_id) != "sensor":
                continue
            on_state = spec.props.get("SupportsOnState")
            sensor_value = spec.props.get("SupportsSensorValue")
            assert (on_state, sensor_value) in {
                (True, False), (False, True), (False, False),
            }, (
                f"{name}: {spec.device_type_id} display props "
                f"({on_state!r}, {sensor_value!r}) — would display 'off' forever"
            )
            if (on_state, sensor_value) == (False, False):
                ui_display = XML_UI_DISPLAY_BY_ID.get(spec.device_type_id, "")
                assert ui_display, (
                    f"{name}: {spec.device_type_id} disclaims both built-in "
                    "displays but Devices.xml has no UiDisplayStateId fallback"
                )
                assert ui_display in XML_STATES_BY_ID.get(spec.device_type_id, set()), (
                    f"{name}: {spec.device_type_id} UiDisplayStateId "
                    f"'{ui_display}' is not an XML-declared custom state"
                )


def test_every_registered_handler_type_exists_in_devices_xml():
    # Registry-wide, zoo-independent: a handler pointing at a typo'd or
    # removed Devices.xml id would create devices that Indigo rejects.
    for handler in HandlerRegistry().handlers:
        if not handler.device_type_id:
            continue  # merge-only handlers (PowerSource, Electrical*)
        assert handler.device_type_id in XML_TYPE_BY_ID, (
            f"{type(handler).__name__} → {handler.device_type_id} not in Devices.xml"
        )
