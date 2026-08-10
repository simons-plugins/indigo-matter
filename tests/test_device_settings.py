"""Tests for writable device settings — issue #186.

The registry (``matter_handlers/settings.py``) and the Edit Device dialog layer
(``device_settings.py``). Both are Indigo-free and matter-server-free by design,
so everything below runs without a Matter stack — which matters because the
behaviour most worth testing is one only the real hardware exhibits: an Aqara
FP300 that ACKs a write and silently ignores it. ``_LyingClient`` reproduces it.

Covers:
- Bounds parsing: limits struct, level count, and every way both can be junk
- Sensitivity labels driven by the device's own level count, not a constant
- Honest degradation: no limits ⇒ the setting is not offered at all
- Dialog seeding: markers, current values from STATE, per-device range text
- Validation against the device's own bounds, including the messages
- planned_writes sending only what CHANGED
- apply_setting: verified success, the lying device, an unreadable read-back,
  a failed write, and the retry on both legs
"""
from __future__ import annotations

import asyncio

import pytest

import device_settings
from device_settings import (
    MARKER_NO,
    MARKER_YES,
    PlannedWrite,
    apply_setting,
    config_ui_values,
    offered_settings,
    planned_writes,
    validate_settings,
)
from matter_handlers.settings import (
    ATTR_CURRENT_SENSITIVITY,
    ATTR_HOLD_TIME,
    ATTR_HOLD_TIME_LIMITS,
    ATTR_SUPPORTED_SENSITIVITY_LEVELS,
    CLUSTER_BOOLEAN_STATE_CONFIG,
    CLUSTER_OCCUPANCY_SENSING,
    Bounds,
    coerce_value,
    parse_level_count,
    parse_limits_struct,
    sensitivity_options,
    settings_for_type,
    verify_write,
)

MOTION = "matterMotionSensor"
CONTACT = "matterContactSensor"

#: EXACTLY what the real FP300 reports, copied from the raw get_node dump —
#: HoldTimeLimits is a struct with field-INDEX keys (0=min, 1=max, 2=default),
#: not {"min":…,"max":…}. Fixtures previously used the research doc's prose
#: rendering, which is why a parser that could not read the real thing passed
#: every test and then showed no hold-time field on jarvis.
FP300_LIMITS = {
    (CLUSTER_OCCUPANCY_SENSING, ATTR_HOLD_TIME_LIMITS): {"0": 1, "1": 300, "2": 10},
    (CLUSTER_BOOLEAN_STATE_CONFIG, ATTR_SUPPORTED_SENSITIVITY_LEVELS): 3,
}


def _limits(table=None):
    """A limits lookup over a ``{(cluster, attribute): value}`` table."""
    table = FP300_LIMITS if table is None else table
    return lambda cluster, attribute: table.get((cluster, attribute))


def _setting(key):
    for setting in settings_for_type(MOTION):
        if setting.key == key:
            return setting
    raise AssertionError(f"no setting {key!r}")


# ---------------------------------------------------------------------------
# Bounds parsing
# ---------------------------------------------------------------------------

def test_parse_limits_struct_reads_the_REAL_fp300_holdtimelimits():
    """The exact bytes off the wire: struct fields keyed by INDEX."""
    assert parse_limits_struct({"0": 1, "1": 300, "2": 10}) == Bounds(1, 300)


def test_parse_limits_struct_accepts_integer_struct_keys():
    # JSON keys are strings, but no parser guarantees it stays that way.
    assert parse_limits_struct({0: 1, 1: 300, 2: 10}) == Bounds(1, 300)


def test_parse_limits_struct_still_accepts_resolved_field_names():
    # Fallback, in case matter-server ever resolves struct fields to names.
    assert parse_limits_struct({"min": 1, "max": 300, "default": 10}) == Bounds(1, 300)
    assert parse_limits_struct({"holdTimeMin": 2, "holdTimeMax": 60}) == Bounds(2, 60)


@pytest.mark.parametrize("value", [
    None, 5, "1-300", [], {}, {"min": 1}, {"min": "x", "max": 300},
    {"min": 300, "max": 1},   # a device contradicting itself is not a range
    {"0": 1},                 # index-keyed but truncated
    {"0": 300, "1": 1},       # index-keyed and self-contradictory
    {"2": 10},                # only the default — no range to validate against
])
def test_parse_limits_struct_refuses_junk(value):
    assert parse_limits_struct(value) is None


def test_parse_level_count_turns_a_count_into_indices():
    assert parse_level_count(3) == Bounds(0, 2)


@pytest.mark.parametrize("value", [None, 0, -1, "many", {}, []])
def test_parse_level_count_refuses_junk(value):
    assert parse_level_count(value) is None


# ---------------------------------------------------------------------------
# Sensitivity labels — spec §1.8.6.2 fixes the ordering, not the middle names
# ---------------------------------------------------------------------------

def test_three_levels_get_names_and_0_is_least_sensitive():
    rows = sensitivity_options(Bounds(0, 2))
    assert [value for value, _label in rows] == ["0", "1", "2"]
    assert "least sensitive" in rows[0][1]
    assert "most sensitive" in rows[-1][1]


def test_five_levels_fall_back_to_numbering_rather_than_inventing_names():
    rows = sensitivity_options(Bounds(0, 4))
    assert len(rows) == 5
    assert "least sensitive" in rows[0][1]
    assert "most sensitive" in rows[-1][1]
    # The spec blesses no wording for the middle of a 5-level device.
    assert rows[2][1] == "Level 2"


# ---------------------------------------------------------------------------
# Honest degradation — the capability gate
# ---------------------------------------------------------------------------

def test_fp300_is_offered_both_settings():
    keys = [o.setting.key for o in offered_settings(MOTION, _limits())]
    assert sorted(keys) == ["holdTime", "sensitivityLevel"]


def test_pre_1_4_sensor_is_not_offered_hold_time():
    """No HoldTimeLimits ⇒ no HoldTime ⇒ no field, rather than a field that fails."""
    table = {(CLUSTER_BOOLEAN_STATE_CONFIG, ATTR_SUPPORTED_SENSITIVITY_LEVELS): 3}
    keys = [o.setting.key for o in offered_settings(MOTION, _limits(table))]
    assert keys == ["sensitivityLevel"]


def test_unparseable_limits_are_treated_as_absent_not_guessed():
    table = {(CLUSTER_OCCUPANCY_SENSING, ATTR_HOLD_TIME_LIMITS): "1 to 300"}
    assert offered_settings(MOTION, _limits(table)) == []


def test_a_device_with_no_node_yet_is_offered_nothing():
    assert offered_settings(MOTION, lambda c, a: None) == []


def test_contact_sensor_never_gets_hold_time():
    """HoldTime is OccupancySensing's; a contact sensor has no such cluster."""
    keys = [o.setting.key for o in offered_settings(CONTACT, _limits())]
    assert keys == ["sensitivityLevel"]


def test_a_type_with_no_settings_declares_none():
    assert settings_for_type("matterTemperatureSensor") == []


# ---------------------------------------------------------------------------
# Dialog seeding
# ---------------------------------------------------------------------------

def test_config_ui_values_seeds_markers_current_values_and_range():
    values = config_ui_values(MOTION, {"holdTime": 10, "sensitivityLevel": 1}, _limits())
    assert values["hasHoldTime"] == MARKER_YES
    assert values["hasSensitivity"] == MARKER_YES
    assert values["hasAnySetting"] == MARKER_YES
    assert values["holdTime"] == "10"
    assert values["sensitivityLevel"] == "1"     # a menu matches on string ids
    assert values["holdTimeRange"] == "1-300 seconds"


def test_config_ui_values_hides_a_setting_the_device_lacks():
    table = {(CLUSTER_BOOLEAN_STATE_CONFIG, ATTR_SUPPORTED_SENSITIVITY_LEVELS): 3}
    values = config_ui_values(MOTION, {"sensitivityLevel": 1}, _limits(table))
    assert values["hasHoldTime"] == MARKER_NO
    assert values["hasSensitivity"] == MARKER_YES
    assert "holdTime" not in values


def test_the_whole_section_hides_when_nothing_is_offered():
    values = config_ui_values(MOTION, {}, lambda c, a: None)
    assert values["hasAnySetting"] == MARKER_NO
    assert values["hasHoldTime"] == MARKER_NO


def test_markers_are_never_substrings_of_one_another():
    """visibleBindingValue is a CONTAINS match, so "no" must not match "yes"."""
    assert MARKER_YES not in MARKER_NO
    assert MARKER_NO not in MARKER_YES


def test_an_unknown_current_value_leaves_the_field_blank_rather_than_guessing():
    values = config_ui_values(MOTION, {}, _limits())
    assert values["holdTime"] == ""


def test_seeding_prefers_device_state_over_a_stale_saved_prop():
    """The device is the source of truth — a prop left over from a failed write
    must not be shown back as if it were the setting."""
    values = config_ui_values(MOTION, {"holdTime": 10}, _limits(),
                              values={"holdTime": "250"})
    assert values["holdTime"] == "10"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_in_range_values_validate():
    assert validate_settings(MOTION, {"holdTime": "45", "sensitivityLevel": "2"},
                             _limits()) == {}


@pytest.mark.parametrize("entered", ["0", "301", "-5"])
def test_out_of_range_hold_time_is_refused_with_the_devices_real_range(entered):
    errors = validate_settings(MOTION, {"holdTime": entered}, _limits())
    assert "holdTime" in errors
    assert "1 and 300" in errors["holdTime"]


def test_the_boundaries_themselves_are_allowed():
    assert validate_settings(MOTION, {"holdTime": "1"}, _limits()) == {}
    assert validate_settings(MOTION, {"holdTime": "300"}, _limits()) == {}


@pytest.mark.parametrize("entered", ["abc", "12.5", ""])
def test_non_integer_hold_time_is_refused(entered):
    assert "holdTime" in validate_settings(MOTION, {"holdTime": entered}, _limits())


def test_sensitivity_above_the_supported_count_is_refused_before_sending():
    """Spec §1.8.6.1: the device would answer CONSTRAINT_ERROR — no point asking."""
    errors = validate_settings(MOTION, {"sensitivityLevel": "3"}, _limits())
    assert "sensitivityLevel" in errors


def test_a_setting_the_device_does_not_offer_is_not_validated():
    """A field that cannot be shown cannot be wrong."""
    assert validate_settings(MOTION, {"holdTime": "9999"}, lambda c, a: None) == {}


def test_coerce_value_reports_the_unit_in_its_message():
    _value, reason = coerce_value(_setting("holdTime"), "999", Bounds(1, 300))
    assert "seconds" in reason


# ---------------------------------------------------------------------------
# planned_writes — only what changed
# ---------------------------------------------------------------------------

def test_only_changed_settings_are_written():
    plans = planned_writes(MOTION, {"holdTime": "30", "sensitivityLevel": "1"},
                           {"holdTime": 10, "sensitivityLevel": 1}, _limits())
    assert [(p.setting.key, p.value, p.previous) for p in plans] == [("holdTime", 30, 10)]


def test_saving_an_untouched_dialog_writes_nothing():
    """Opening a dialog to READ a value must not spend a round trip on a
    battery-powered device telling it what it already knows."""
    assert planned_writes(MOTION, {"holdTime": "10", "sensitivityLevel": "1"},
                          {"holdTime": 10, "sensitivityLevel": 1}, _limits()) == []


def test_an_unknown_previous_value_counts_as_changed():
    plans = planned_writes(MOTION, {"holdTime": "30"}, {}, _limits())
    assert [p.value for p in plans] == [30]


def test_an_invalid_value_is_never_planned():
    assert planned_writes(MOTION, {"holdTime": "9999"}, {"holdTime": 10}, _limits()) == []


def test_both_settings_can_change_at_once():
    plans = planned_writes(MOTION, {"holdTime": "30", "sensitivityLevel": "2"},
                           {"holdTime": 10, "sensitivityLevel": 1}, _limits())
    assert sorted(p.setting.key for p in plans) == ["holdTime", "sensitivityLevel"]


# ---------------------------------------------------------------------------
# verify_write — rule 1, in isolation
# ---------------------------------------------------------------------------

def test_a_matching_read_back_is_success():
    ok, _reason = verify_write(_setting("holdTime"), 30, 30)
    assert ok is True


def test_a_mismatched_read_back_is_a_failure_that_names_both_values():
    ok, reason = verify_write(_setting("holdTime"), 30, 10)
    assert ok is False
    assert "30" in reason and "10" in reason
    assert "NOT applied" in reason


def test_an_absent_read_back_is_a_failure_not_a_success():
    ok, reason = verify_write(_setting("holdTime"), 30, None)
    assert ok is False
    assert "may or may not" in reason


def test_an_unreadable_read_back_is_a_failure():
    ok, _reason = verify_write(_setting("holdTime"), 30, "soon")
    assert ok is False


# ---------------------------------------------------------------------------
# apply_setting — the write path, against clients that misbehave
# ---------------------------------------------------------------------------

class _Client:
    """A matter-server client double. ``reads`` is what read() will return."""

    def __init__(self, reads=None, write_errors=None, read_errors=None):
        self.writes = []
        self.reads = reads
        self._write_errors = list(write_errors or [])
        self._read_errors = list(read_errors or [])
        self.read_calls = 0

    async def write(self, write):
        self.writes.append(write)
        if self._write_errors:
            err = self._write_errors.pop(0)
            if err is not None:
                raise err
        return {"status": 0}

    async def read(self, node_id, endpoint, cluster, attribute):
        self.read_calls += 1
        if self._read_errors:
            err = self._read_errors.pop(0)
            if err is not None:
                raise err
        return self.reads


class _LyingClient(_Client):
    """The Aqara behaviour that makes verification non-negotiable: the write is
    acknowledged, and the value never changes."""

    def __init__(self, keeps=10):
        super().__init__(reads=keeps)


def _plan(key="holdTime", value=30, previous=10):
    return PlannedWrite(_setting(key), value, previous)


async def _no_sleep(_delay):
    return None


def _run(coro):
    return asyncio.run(coro)


def test_a_verified_write_succeeds_and_says_so():
    client = _Client(reads=30)
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is True
    assert "30 seconds" in message
    assert len(client.writes) == 1


def test_the_write_carries_the_right_cluster_attribute_and_value():
    client = _Client(reads=30)
    _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    write = client.writes[0]
    assert (write.node_id, write.endpoint) == (56, 1)
    assert (write.cluster, write.attribute) == (CLUSTER_OCCUPANCY_SENSING, ATTR_HOLD_TIME)
    assert write.value == 30


def test_a_device_that_acks_and_ignores_is_reported_as_a_FAILURE():
    """The whole reason read-back verification exists."""
    client = _LyingClient(keeps=10)
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert "NOT applied" in message
    assert client.writes, "the write was still attempted"


def test_a_read_back_that_never_answers_is_not_reported_as_success():
    client = _Client(read_errors=[TimeoutError(), TimeoutError()])
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert "could not be confirmed" in message


def test_the_read_back_is_retried_once_before_giving_up():
    """A sleepy Thread device can miss a poll window; one miss is not a verdict."""
    client = _Client(reads=30, read_errors=[TimeoutError()])
    ok, _message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is True
    assert client.read_calls == 2


def test_the_write_is_retried_once_before_giving_up():
    client = _Client(reads=30, write_errors=[TimeoutError()])
    ok, _message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is True
    assert len(client.writes) == 2


def test_a_write_that_fails_twice_is_reported_and_never_verified():
    client = _Client(write_errors=[TimeoutError(), TimeoutError()])
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert "was not changed" in message
    assert client.read_calls == 0, "a write that never landed must not be read back"


def test_sensitivity_writes_go_to_the_boolean_state_config_cluster():
    client = _Client(reads=2)
    ok, _message = _run(apply_setting(
        client, 56, 1, _plan("sensitivityLevel", 2, 1), sleep=_no_sleep))
    assert ok is True
    write = client.writes[0]
    assert (write.cluster, write.attribute) == (CLUSTER_BOOLEAN_STATE_CONFIG,
                                                ATTR_CURRENT_SENSITIVITY)


def test_apply_setting_is_exported_for_the_plugin_layer():
    assert hasattr(device_settings, "apply_setting")
