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
  5. the loop guard beats every other rule, for every device shape (XAC6);
  6. reverse coverage — the union of every eligible role across the zoo IS
     `bridge_protocol.ROLES`, so a role added to the protocol cannot ship
     UI-unreachable (and `classify` never raises, whatever the device does).
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
    CustomDevice,
    DimmerDevice,
    HostileDevice,
    HostilePluginIdDevice,
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
    # RGB WITHOUT white-temperature — the shape a plain colour bulb actually
    # ships in. Its own row because the mixed-capability row above cannot tell
    # "rgb implies extendedColorLight" apart from "whiteTemp implies it".
    "dimmer_rgb_only": (
        DimmerDevice(19, "Party Bulb", supportsColor=True, supportsRGB=True),
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
        EligibleDevice(("occupancySensor", "contactSensor", "waterLeakDetector",
                        "waterFreezeDetector", "rainSensor"), "occupancySensor"),
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


def test_zoo_offers_every_protocol_role_somewhere():
    """Invariant 6 (reverse coverage) — no role can land UI-unreachable.

    The forward invariant says every role we emit is a real §4.2 role. This is
    its mirror: every §4.2 role must be reachable from SOME device shape in the
    zoo. Adding a role to `bridge_protocol.ROLES` (and to the node) without a
    device that can ever be declared as it would ship a role the picker never
    offers — dead protocol surface nobody notices for a milestone.
    """
    offered = set()
    for device, _expected in ZOO.values():
        # OURS, not OTHER_PLUGIN_ID: the zoo devices themselves carry
        # OTHER_PLUGIN_ID, so classifying against it would loop-guard the
        # entire zoo and make this invariant vacuous.
        verdict = classify(device, OURS)
        if isinstance(verdict, EligibleDevice):
            offered.update(verdict.eligible_roles)
    assert offered == set(ROLES), (
        f"unreachable roles: {sorted(set(ROLES) - offered)}; "
        f"non-protocol roles offered: {sorted(offered - set(ROLES))}"
    )


def test_every_role_the_picker_offers_can_actually_be_bridged(mock_indigo_base):
    """The third side of the triangle, closed by E4.

    The two invariants above tie the catalog to the *protocol*. This ties it to
    the *implementation*: a role the picker offers but no handler serves is an
    export the user selects, sees confirmed in the dialog, and then never finds
    in any ecosystem — the failure mode E4 exists to remove. Before E4 this
    could not be asserted; now it can, so it is.
    """
    import importlib
    import export_handlers
    importlib.reload(export_handlers)
    offered = set()
    for device, _expected in ZOO.values():
        verdict = classify(device, OURS)
        if isinstance(verdict, EligibleDevice):
            offered.update(verdict.eligible_roles)
    assert offered <= export_handlers.BRIDGEABLE_ROLES, (
        f"offered but not bridgeable: {sorted(offered - export_handlers.BRIDGEABLE_ROLES)}"
    )


# ---------------------------------------------------------------------------
# C1 — classify() never propagates. Indigo proxies die mid-iteration.
# ---------------------------------------------------------------------------
def test_a_device_whose_every_attribute_raises_is_excluded_not_fatal():
    assert classify(HostileDevice(), OURS) == Excluded(export_catalog.REASON_DEVICE_ERROR)


def test_an_unreadable_plugin_id_fails_closed():
    """If the loop guard's input cannot be read, the answer is NOT eligible."""
    verdict = classify(HostilePluginIdDevice(1, "Flaky Plug"), OURS)
    assert verdict == Excluded(export_catalog.REASON_DEVICE_ERROR)
    assert not isinstance(verdict, EligibleDevice)


def test_classification_failure_is_logged_once(caplog):
    with caplog.at_level("ERROR"):
        classify(HostileDevice(), OURS)
    assert len([r for r in caplog.records if "could not classify" in r.message]) == 1


def test_a_raising_capability_flag_is_excluded_not_fatal():
    class ExplodingSensor(SensorDevice):
        @property
        def supportsOnState(self):  # noqa: N802 - mirrors Indigo's own naming
            raise RuntimeError("state read failed")

        @supportsOnState.setter
        def supportsOnState(self, value):
            pass

    assert classify(ExplodingSensor(1, "Odd"), OURS) == Excluded(
        export_catalog.REASON_DEVICE_ERROR)


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


# ---------------------------------------------------------------------------
# C2 — the needles are WORDS, not substrings
# ---------------------------------------------------------------------------
# Substring matching turned ordinary English into sensor units: every name
# below contains a needle ("flux" ⊃ lux, "attempt" ⊃ temp, "epsilon" ⊃ psi,
# "overflow" ⊃ flow, "moist"…), and each was silently mis-defaulted.
@pytest.mark.parametrize("name", [
    "Fluxcapacitor",        # lux
    "Attempt Counter",      # temp
    "Epsilon Meter",        # psi
    "Overflow Alarm",       # flow
    "Deluxe Panel",         # lux
    "Contempt Index",       # temp
])
def test_a_word_that_merely_contains_a_unit_is_not_a_unit(name):
    dev = SensorDevice(1, name, supportsSensorValue=True)
    assert classify(dev, OURS) == Excluded(export_catalog.REASON_SENSOR_UNITS), name


@pytest.mark.parametrize("text,expected", [
    ("19.5 °C", "temperatureSensor"),
    ("Humidity 47%RH", "humiditySensor"),
    ("Flow (l/min)", "flowSensor"),
    ("340 lux", "lightSensor"),
    ("1013 hPa", "pressureSensor"),
    ("Tank Pressure", "pressureSensor"),
    ("Soil moisture", "humiditySensor"),
    ("Light level", "lightSensor"),
], ids=lambda v: str(v))
def test_genuine_units_still_classify_after_the_word_boundaries(text, expected):
    dev = SensorDevice(1, "", supportsSensorValue=True, displayStateValUi=text)
    assert classify(dev, OURS).default_role == expected


def test_a_declared_unit_outranks_the_device_name():
    """Names are the weakest evidence: a declared unit must win outright.

    "Flow" in a name would otherwise beat a props-declared "%RH" purely
    because flow sorts before temperature in the pattern table.
    """
    dev = SensorDevice(1, "Bathroom Flow Sensor", supportsSensorValue=True,
                       pluginProps={"unit": "%RH"})
    assert classify(dev, OURS).default_role == "humiditySensor"


def test_the_formatted_value_outranks_the_device_name():
    dev = SensorDevice(1, "Kitchen Temperature", supportsSensorValue=True,
                       displayStateValUi="1013 hPa")
    assert classify(dev, OURS).default_role == "pressureSensor"


def test_humidity_beats_temperature_in_a_combined_name():
    """First match wins, and 'humidity' is the unambiguous word."""
    dev = SensorDevice(1, "Temperature/Humidity — Humidity", supportsSensorValue=True,
                       pluginProps={"unit": "%RH"})
    assert classify(dev, OURS).default_role == "humiditySensor"


def test_binary_sensor_beats_the_unit_heuristic():
    """A device claiming BOTH flags is read as binary, not numeric.

    The role asserted is `contactSensor` because "Door" is what the binary name
    heuristic sees (issue #236) — the point of the test is the *branch*: a
    numeric-sounding name does not drag an on/off sensor into the numeric five.
    """
    dev = SensorDevice(1, "Door Temperature", supportsOnState=True, supportsSensorValue=True)
    verdict = classify(dev, OURS)
    assert verdict.default_role == "contactSensor"
    assert set(verdict.eligible_roles) == set(export_catalog.BINARY_SENSOR_ROLES)


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
        # OURS for the same reason as the reverse-coverage invariant above.
        verdict = classify(device, OURS)
        if isinstance(verdict, EligibleDevice):
            offered.update(verdict.eligible_roles)
    assert offered.isdisjoint(export_catalog.EXCLUDED_ROLES)


def test_is_exportable_mirrors_classify():
    assert export_catalog.is_exportable(RelayDevice(1, "Plug"), OURS) is True
    assert export_catalog.is_exportable(SprinklerDevice(2, "Zones"), OURS) is False


def test_the_occupancy_label_names_motion():
    """Issue #252 — Matter has no motion device type, so the occupancy role IS
    the export of a PIR. A user hunting the picker for "motion" must find it
    there, or read its absence as "my motion sensor cannot be exported".
    """
    label = export_catalog.role_label(export_catalog.ROLE_OCCUPANCY_SENSOR)
    assert "motion" in label.lower()
    assert "occupancy" in label.lower()


def test_role_label_falls_back_to_the_role_name():
    assert export_catalog.role_label("onOffLight") == "Light (On/Off)"
    assert export_catalog.role_label("somethingNew") == "somethingNew"
