"""Export-catalog contract harness — one row per PRD §5.2 mapping table row.

Same shape as `test_device_zoo.py`, for the opposite direction: a table of
device shapes run through the REAL catalog, with structural invariants
asserted over every entry rather than only the individual expectations.

Invariants:
  1. every row's verdict matches the §5.2 table (role or exclusion);
  2. every emitted role is in the BRIDGE_PROTOCOL §4.2 enum — a role the
     bridge node would reject with `unknown_role` must never reach the UI;
  3. `default_role` is always the FIRST eligible role (the picker's first
     option must be its safest one);
  4. every exclusion carries a non-empty reason (XAC9 shows it in the picker);
  5. the loop guard beats every other rule, for every device shape (XAC6).
"""
from __future__ import annotations

import copy
import plistlib
from pathlib import Path

import pytest

import export_catalog
from bridge_protocol import ROLES
from export_catalog import EligibleDevice, Excluded, classify
from fakes import (
    OTHER_PLUGIN_ID,
    CustomDevice,
    DimmerDevice,
    MultiIODevice,
    RelayDevice,
    SensorDevice,
    SpeedControlDevice,
    SprinklerDevice,
    ThermostatDevice,
    minimal_device,
)

OURS = export_catalog.DEFAULT_PLUGIN_ID

# name → (device, expected verdict). One entry per §5.2 row.
ZOO = {
    # --- Relay rows -------------------------------------------------------
    "relay_plug_default": (
        RelayDevice(1, "Study Plug"),
        EligibleDevice(("onOffPlugInUnit", "onOffLight", "doorLock"), "onOffPlugInUnit"),
    ),
    # --- Dimmer rows ------------------------------------------------------
    "dimmer_plain": (
        DimmerDevice(2, "Hall Dimmer"),
        EligibleDevice(("dimmableLight", "windowCovering"), "dimmableLight"),
    ),
    "dimmer_full_colour": (
        DimmerDevice(3, "Lounge Bulb", supportsColor=True, supportsRGB=True,
                     supportsWhiteTemperature=True),
        EligibleDevice(("extendedColorLight", "dimmableLight", "colorTemperatureLight",
                        "windowCovering"), "extendedColorLight"),
    ),
    "dimmer_colour_temp_only": (
        DimmerDevice(4, "Reading Lamp", supportsColor=True, supportsWhiteTemperature=True),
        EligibleDevice(("colorTemperatureLight", "dimmableLight", "windowCovering"),
                       "colorTemperatureLight"),
    ),
    # --- Sensor rows ------------------------------------------------------
    "sensor_binary": (
        SensorDevice(5, "Hall Motion", supportsOnState=True),
        EligibleDevice(("occupancySensor", "contactSensor"), "occupancySensor"),
    ),
    "sensor_temperature": (
        SensorDevice(6, "Study Temperature", supportsSensorValue=True,
                     displayStateValUi="19.5 °C"),
        EligibleDevice(("temperatureSensor", "humiditySensor", "lightSensor",
                        "pressureSensor", "flowSensor"), "temperatureSensor"),
    ),
    "sensor_humidity": (
        SensorDevice(7, "Bath RH", supportsSensorValue=True, pluginProps={"unit": "%RH"}),
        EligibleDevice(("humiditySensor", "temperatureSensor", "lightSensor",
                        "pressureSensor", "flowSensor"), "humiditySensor"),
    ),
    "sensor_lux": (
        SensorDevice(8, "Porch Light Level", supportsSensorValue=True,
                     pluginProps={"units": "lux"}),
        EligibleDevice(("lightSensor", "temperatureSensor", "humiditySensor",
                        "pressureSensor", "flowSensor"), "lightSensor"),
    ),
    "sensor_pressure": (
        SensorDevice(9, "Barometer", supportsSensorValue=True,
                     displayStateValUi="1013 hPa"),
        EligibleDevice(("pressureSensor", "temperatureSensor", "humiditySensor",
                        "lightSensor", "flowSensor"), "pressureSensor"),
    ),
    "sensor_flow": (
        SensorDevice(10, "Mains Meter", supportsSensorValue=True,
                     pluginProps={"unit": "l/min"}),
        EligibleDevice(("flowSensor", "temperatureSensor", "humiditySensor",
                        "lightSensor", "pressureSensor"), "flowSensor"),
    ),
    "sensor_unmappable_units": (
        SensorDevice(11, "Solar Yield", supportsSensorValue=True,
                     displayStateValUi="4.2 kWh"),
        Excluded(export_catalog.REASON_SENSOR_UNITS),
    ),
    "sensor_neither_state_nor_value": (
        SensorDevice(12, "Odd Sensor"),
        Excluded(export_catalog.REASON_SENSOR_NO_VALUE),
    ),
    # --- Thermostat -------------------------------------------------------
    "thermostat": (
        ThermostatDevice(13, "Hall Stat"),
        EligibleDevice(("thermostat",), "thermostat"),
    ),
    # --- Explicitly not exportable in v1 ----------------------------------
    "speed_control_fan": (
        SpeedControlDevice(14, "Bathroom Fan"),
        Excluded(export_catalog.REASON_FAN),
    ),
    "sprinkler": (
        SprinklerDevice(15, "Irrigation"),
        Excluded(export_catalog.REASON_SPRINKLER),
    ),
    "multi_io": (
        MultiIODevice(16, "IO Board"),
        Excluded(export_catalog.REASON_MULTI_IO),
    ),
    "custom_no_role": (
        CustomDevice(17, "Matter Energy Meter"),
        Excluded(export_catalog.REASON_NO_ROLE),
    ),
    "created_by_this_plugin": (
        RelayDevice(18, "Matter Plug", plugin_id=OURS),
        Excluded(export_catalog.REASON_LOOP_GUARD),
    ),
}


@pytest.mark.parametrize("name", sorted(ZOO))
def test_zoo_row_matches_the_prd_table(name):
    """Invariant 1 — the §5.2 row is what the catalog actually returns."""
    device, expected = ZOO[name]
    assert classify(device, OURS) == expected


@pytest.mark.parametrize("name", sorted(ZOO))
def test_zoo_roles_are_all_in_the_protocol_enum(name):
    """Invariant 2 — no role the bridge node would reject with unknown_role."""
    device, _expected = ZOO[name]
    verdict = classify(device, OURS)
    if isinstance(verdict, EligibleDevice):
        assert set(verdict.eligible_roles) <= ROLES


@pytest.mark.parametrize("name", sorted(ZOO))
def test_zoo_default_role_is_first_and_eligible(name):
    """Invariant 3 — the picker's first option is the safe default."""
    device, _expected = ZOO[name]
    verdict = classify(device, OURS)
    if isinstance(verdict, EligibleDevice):
        assert verdict.eligible_roles[0] == verdict.default_role
        assert len(set(verdict.eligible_roles)) == len(verdict.eligible_roles)


@pytest.mark.parametrize("name", sorted(ZOO))
def test_zoo_exclusions_always_carry_a_reason(name):
    """Invariant 4 — XAC9: excluded devices show *why*."""
    device, _expected = ZOO[name]
    verdict = classify(device, OURS)
    if isinstance(verdict, Excluded):
        assert verdict.reason.strip()


# ---------------------------------------------------------------------------
# XAC6 — the loop guard. Required by name in the PRD's acceptance criteria.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(ZOO))
def test_xac6_our_own_devices_are_excluded_whatever_their_type(name):
    """Invariant 5 — a device carrying OUR pluginId is excluded, always.

    Run over every zoo shape: whatever a future edit does to the type rules,
    the loop guard (XNG3) has to beat all of them.
    """
    device = copy.copy(ZOO[name][0])  # never mutate the shared zoo
    device.pluginId = OURS
    assert classify(device, OURS) == Excluded(export_catalog.REASON_LOOP_GUARD)


def test_xac6_guard_follows_the_supplied_plugin_id_not_a_constant():
    """The guard is parameterised — tests (and a renamed bundle) can vary it."""
    device = RelayDevice(1, "Plug", plugin_id="com.example.other-plugin")
    assert classify(device, OURS) == EligibleDevice(
        ("onOffPlugInUnit", "onOffLight", "doorLock"), "onOffPlugInUnit")
    assert classify(device, "com.example.other-plugin") == Excluded(
        export_catalog.REASON_LOOP_GUARD)


def test_xac6_default_plugin_id_matches_the_bundle_identifier():
    """The fallback constant must never drift from Info.plist."""
    plist = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
             / "Contents" / "Info.plist")
    with plist.open("rb") as handle:
        assert plistlib.load(handle)["CFBundleIdentifier"] == export_catalog.DEFAULT_PLUGIN_ID


# ---------------------------------------------------------------------------
# Type dispatch — the isinstance-free discipline
# ---------------------------------------------------------------------------
def test_dimmer_wins_over_its_relay_base():
    """DimmerDevice subclasses RelayDevice; the specific reading must win."""
    assert export_catalog.device_kind(DimmerDevice(1, "D")) == "DimmerDevice"


def test_speed_control_wins_over_its_relay_base():
    assert export_catalog.device_kind(SpeedControlDevice(1, "F")) == "SpeedControlDevice"
    assert classify(SpeedControlDevice(1, "F"), OURS) == Excluded(export_catalog.REASON_FAN)


def test_unknown_class_has_no_kind():
    assert export_catalog.device_kind(CustomDevice(1, "C")) == ""


def test_device_kind_tolerates_an_exotic_object():
    assert export_catalog.device_kind(object()) == ""


# ---------------------------------------------------------------------------
# Pessimistic Indigo: capability flags we cannot assume exist
# ---------------------------------------------------------------------------
def test_minimal_dimmer_degrades_to_plain_dimmable():
    dev = minimal_device("DimmerDevice", 1, "Bare Dimmer")
    assert classify(dev, OURS) == EligibleDevice(("dimmableLight", "windowCovering"),
                                                 "dimmableLight")


def test_minimal_sensor_is_excluded_not_crashed():
    dev = minimal_device("SensorDevice", 1, "Bare Sensor")
    assert classify(dev, OURS) == Excluded(export_catalog.REASON_SENSOR_NO_VALUE)


def test_minimal_relay_still_classifies():
    dev = minimal_device("RelayDevice", 1, "Bare Relay")
    assert isinstance(classify(dev, OURS), EligibleDevice)


def test_device_without_a_plugin_id_is_not_our_own():
    dev = minimal_device("RelayDevice", 1, "Hardware Relay", plugin_id=None)
    assert isinstance(classify(dev, OURS), EligibleDevice)


def test_truthy_but_non_boolean_flags_do_not_count_as_capability():
    """A MagicMock attribute is truthy for everything — only True/1 counts."""
    dev = DimmerDevice(1, "Mocked", supportsRGB=object())
    assert classify(dev, OURS).default_role == "dimmableLight"


# ---------------------------------------------------------------------------
# Sensor unit heuristics
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("19.5 °C", "temperatureSensor"),
    ("68.2 °F", "temperatureSensor"),
    ("21 degC", "temperatureSensor"),
    ("55 %RH", "humiditySensor"),
    ("340 lux", "lightSensor"),
    ("1013 hPa", "pressureSensor"),
    ("2.4 l/min", "flowSensor"),
    ("4.2 kWh", None),
    ("", None),
])
def test_unit_heuristic_from_the_formatted_ui_value(text, expected):
    dev = SensorDevice(1, "", supportsSensorValue=True, displayStateValUi=text)
    verdict = classify(dev, OURS)
    if expected is None:
        assert verdict == Excluded(export_catalog.REASON_SENSOR_UNITS)
    else:
        assert verdict.default_role == expected


def test_unit_heuristic_falls_back_to_the_device_name():
    dev = SensorDevice(1, "Greenhouse Temperature", supportsSensorValue=True)
    assert classify(dev, OURS).default_role == "temperatureSensor"


def test_humidity_beats_temperature_in_a_combined_name():
    """First match wins, and 'humidity' is the unambiguous word."""
    dev = SensorDevice(1, "Temperature/Humidity — Humidity", supportsSensorValue=True,
                       pluginProps={"unit": "%RH"})
    assert classify(dev, OURS).default_role == "humiditySensor"


def test_binary_sensor_beats_the_unit_heuristic():
    dev = SensorDevice(1, "Door Temperature", supportsOnState=True, supportsSensorValue=True)
    assert classify(dev, OURS).default_role == "occupancySensor"


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------
def test_every_labelled_role_is_a_protocol_role():
    assert set(export_catalog.ROLE_LABELS) == set(ROLES)


def test_excluded_roles_are_documented_with_reasons():
    assert set(export_catalog.EXCLUDED_ROLES) == {"valve", "garageDoor", "fan"}
    assert all(reason.strip() for reason in export_catalog.EXCLUDED_ROLES.values())


def test_excluded_roles_are_never_offered():
    offered = set()
    for device, _expected in ZOO.values():
        verdict = classify(device, OTHER_PLUGIN_ID)
        if isinstance(verdict, EligibleDevice):
            offered.update(verdict.eligible_roles)
    assert offered.isdisjoint(export_catalog.EXCLUDED_ROLES)


def test_is_exportable_mirrors_classify():
    assert export_catalog.is_exportable(RelayDevice(1, "Plug"), OURS) is True
    assert export_catalog.is_exportable(SprinklerDevice(2, "Zones"), OURS) is False


def test_role_label_falls_back_to_the_role_name():
    assert export_catalog.role_label("onOffLight") == "Light (On/Off)"
    assert export_catalog.role_label("somethingNew") == "somethingNew"
