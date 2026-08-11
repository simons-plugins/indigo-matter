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
    EnumValues,
    StaticRange,
    implements,
    ATTR_HOLD_TIME,
    ATTR_HOLD_TIME_LIMITS,
    ATTR_SUPPORTED_SENSITIVITY_LEVELS,
    CLUSTER_BOOLEAN_STATE_CONFIG,
    CLUSTER_OCCUPANCY_SENSING,
    NO_ANSWER,
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


@pytest.mark.parametrize("entered", ["abc", "12.5"])
def test_non_integer_hold_time_is_refused(entered):
    assert "holdTime" in validate_settings(MOTION, {"holdTime": entered}, _limits())


@pytest.mark.parametrize("entered", ["", "   "])
def test_a_blank_field_is_left_alone_rather_than_rejected(entered):
    """config_ui_values seeds blank when the device has not reported the value
    yet. Rejecting that made the dialog unsavable on a freshly adopted device —
    the user could not even rename it, and the error blamed them for a value the
    plugin does not know either."""
    assert validate_settings(MOTION, {"holdTime": entered}, _limits()) == {}
    assert planned_writes(MOTION, {"holdTime": entered}, {}, _limits()) == []


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


def test_no_answer_at_all_is_a_failure_and_says_the_outcome_is_unknown():
    ok, reason = verify_write(_setting("holdTime"), 30, NO_ANSWER)
    assert ok is False
    assert "may or may not" in reason


def test_a_null_answer_is_a_DEFINITE_failure_not_an_unknown_one():
    """The device answered; it just holds no value. StartUpOnOff is nullable, so
    this is a real reply and the write provably did not land."""
    ok, reason = verify_write(_setting("holdTime"), 30, None)
    assert ok is False
    assert "NOT applied" in reason
    assert "may or may not" not in reason


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
        # Recorded, not ignored: the write leg silently fell through to
        # MatterClient.write's 5s default while only the read got 30s, and this
        # double could not see it because it took no timeout at all.
        self.write_timeouts = []
        self.read_timeouts = []

    async def write(self, write, timeout=None):
        self.writes.append(write)
        self.write_timeouts.append(timeout)
        if self._write_errors:
            err = self._write_errors.pop(0)
            if err is not None:
                raise err
        return {"status": 0}

    async def read(self, node_id, endpoint, cluster, attribute, timeout=None):
        self.read_calls += 1
        self.read_timeouts.append(timeout)
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


def test_a_failed_write_is_still_verified_rather_than_assumed_unchanged():
    """A timeout is not evidence of anything — the same argument that justifies
    the 30s timeout. It can land after matter-server accepted it, so claiming
    "the setting was not changed" would assert an outcome nothing verified."""
    client = _Client(reads=10, write_errors=[TimeoutError(), TimeoutError()])
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert client.read_calls > 0, "the device must be asked, not assumed"
    assert "NOT applied" in message      # the read-back proved it
    assert "send also failed" in message  # and the send error is still named


def test_a_write_that_errored_but_landed_is_reported_as_SUCCESS():
    """The case that made the old wording a lie: the send reports a timeout,
    the device has the value anyway."""
    client = _Client(reads=30, write_errors=[TimeoutError("no ack"), TimeoutError("no ack")])
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is True
    assert "is now 30 seconds" in message
    assert "no ack" in message, "the send error is still disclosed"


def test_a_write_and_read_that_both_fail_is_reported_as_unconfirmed():
    client = _Client(write_errors=[TimeoutError(), TimeoutError()],
                     read_errors=[TimeoutError("asleep"), TimeoutError("asleep")])
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert "may or may not" in message
    assert "asleep" in message


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


# ---------------------------------------------------------------------------
# Bounds strategies — issue #186 follow-up
#
# Enumerating the Matter model showed the FP300's "bounds from a device
# attribute" pattern is the RARE one: 2 of 57 writable attributes on the
# clusters this plugin handles. Most take a spec-fixed range or an enum.
# ---------------------------------------------------------------------------

RELAY = "matterRelay"

#: OnOff AttributeList as reported by the two dev-fabric plugs that implement
#: the Lighting feature — 16385/16386/16387 present.
ONOFF_WITH_LIGHTING = [0, 16384, 16385, 16386, 16387, 65528, 65529, 65531, 65532, 65533]
#: …and by the two that do not. Only OnOff itself.
ONOFF_WITHOUT_LIGHTING = [0, 65528, 65529, 65531, 65532, 65533]


def _attr_lists(table):
    return lambda cluster: table.get(cluster)


def test_from_attribute_resolves_through_the_declared_parser():
    setting = _setting("holdTime")
    assert setting.resolve_bounds(_limits()) == Bounds(1, 300)


def test_from_attribute_is_unresolved_when_the_device_has_no_limits():
    assert _setting("holdTime").resolve_bounds(lambda c, a: None) is None


def test_static_range_needs_no_device_read():
    bounds = StaticRange(0, 65534).resolve(0x0006, lambda c, a: None)
    assert bounds == Bounds(0, 65534)


def test_enum_values_bound_the_range_and_restrict_membership():
    bounds = EnumValues(((0, "Off"), (1, "On"), (2, "Toggle"))).resolve(6, lambda c, a: None)
    assert (bounds.minimum, bounds.maximum) == (0, 2)
    assert bounds.contains(1) and not bounds.contains(3)


def test_a_non_contiguous_enum_rejects_the_gap():
    """Range alone would accept 1; the device would not."""
    bounds = EnumValues(((0, "Off"), (2, "Toggle"))).resolve(6, lambda c, a: None)
    assert bounds.contains(0) and bounds.contains(2)
    assert not bounds.contains(1)


def test_enum_values_carry_their_own_labels():
    setting = next(s for s in settings_for_type(RELAY) if s.key == "startUpOnOff")
    bounds = setting.resolve_bounds(lambda c, a: None)
    rows = setting.option_rows(bounds)
    assert [v for v, _ in rows] == ["0", "1", "2"]
    assert rows[0][1] == "Off"


# ---------------------------------------------------------------------------
# The AttributeList capability gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attribute_list,attribute,expected", [
    (ONOFF_WITH_LIGHTING, 16387, True),
    (ONOFF_WITHOUT_LIGHTING, 16387, False),
    (ONOFF_WITH_LIGHTING, 0, True),
    (None, 16387, None),        # unknown, NOT absent
    ([], 16387, None),          # empty list is no evidence either
    ("nonsense", 16387, None),
])
def test_implements_reads_the_devices_own_attribute_list(attribute_list, attribute, expected):
    assert implements(attribute_list, attribute) is expected


def test_a_plug_with_the_lighting_feature_is_offered_the_onoff_setting():
    offered = offered_settings(RELAY, lambda c, a: None,
                               _attr_lists({0x0006: ONOFF_WITH_LIGHTING}))
    assert [o.setting.key for o in offered] == ["startUpOnOff"]


def test_a_plug_without_the_lighting_feature_is_offered_nothing():
    """Two of the four dev-fabric plugs are in this state — the gate is not
    guarding a theoretical case."""
    assert offered_settings(RELAY, lambda c, a: None,
                            _attr_lists({0x0006: ONOFF_WITHOUT_LIGHTING})) == []


def test_an_unknown_attribute_list_does_not_hide_a_setting_with_device_bounds():
    """Unknown is not absent — but only where something ELSE is evidence. A
    FromAttribute setting still has its limits attribute, so it stands or falls
    on that, as before."""
    offered = offered_settings(MOTION, _limits(), _attr_lists({}))
    assert sorted(o.setting.key for o in offered) == ["holdTime", "sensitivityLevel"]


def test_an_unknown_attribute_list_DOES_hide_a_spec_bounded_setting():
    """StaticRange/EnumValues resolve unconditionally, so with no AttributeList
    there is no evidence at all. Treating unknown as "offer" there silently
    became "offer to everything", including the plugs that demonstrably do not
    implement it."""
    assert offered_settings(RELAY, lambda c, a: None, _attr_lists({})) == []
    assert offered_settings(RELAY, lambda c, a: None) == []


def test_the_attribute_list_is_what_admits_a_spec_bounded_setting():
    admitted = offered_settings(RELAY, lambda c, a: None,
                                _attr_lists({0x0006: ONOFF_WITH_LIGHTING}))
    vetoed = offered_settings(RELAY, lambda c, a: None,
                              _attr_lists({0x0006: ONOFF_WITHOUT_LIGHTING}))
    assert [o.setting.key for o in admitted] == ["startUpOnOff"]
    assert vetoed == []


def test_a_device_with_no_node_is_offered_nothing_whatever_its_bounds():
    """The relay case: EnumValues/StaticRange resolve with no device at all, so
    a never-reconciled relay used to be shown a full settings section that could
    not be saved and threw on the way out."""
    assert offered_settings(RELAY, lambda c, a: None,
                            _attr_lists({0x0006: ONOFF_WITH_LIGHTING}),
                            device_known=False) == []
    assert offered_settings(MOTION, _limits(), None, device_known=False) == []


def test_every_rejection_is_reported_to_the_caller():
    """The silent drop was the mechanism of both bugs that shipped on this
    branch. offered_settings must be able to say what it withheld and why."""
    skipped = []
    offered_settings(RELAY, lambda c, a: None,
                     _attr_lists({0x0006: ONOFF_WITHOUT_LIGHTING}),
                     on_skip=lambda setting, reason: skipped.append((setting.key, reason)))
    assert [k for k, _ in skipped] == ["startUpOnOff"]
    assert all("AttributeList" in reason for _, reason in skipped)


def test_unresolvable_bounds_are_reported_not_just_dropped():
    skipped = []
    table = {(CLUSTER_OCCUPANCY_SENSING, ATTR_HOLD_TIME_LIMITS): "1 to 300"}
    offered_settings(MOTION, _limits(table), None,
                     on_skip=lambda setting, reason: skipped.append((setting.key, reason)))
    reasons = dict(skipped)
    assert "permitted values could not be determined" in reasons["holdTime"]


def test_a_missing_node_is_reported_as_such():
    skipped = []
    offered_settings(MOTION, _limits(), None, device_known=False,
                     on_skip=lambda setting, reason: skipped.append((setting.key, reason)))
    assert all("no Matter node" in reason for _, reason in skipped)


def test_the_fp300_gate_still_passes_on_its_real_attribute_lists():
    lists = {0x0406: [0, 1, 2, 3, 4, 65531], 0x0080: [0, 1, 2, 65531]}
    offered = offered_settings(MOTION, _limits(), _attr_lists(lists))
    assert sorted(o.setting.key for o in offered) == ["holdTime", "sensitivityLevel"]


def test_a_pre_1_4_occupancy_sensor_is_vetoed_by_its_attribute_list():
    """No HoldTime in the list — refused before the limits are even consulted."""
    lists = {0x0406: [0, 1, 2, 65531], 0x0080: [0, 1, 2, 65531]}
    offered = offered_settings(MOTION, _limits(), _attr_lists(lists))
    assert [o.setting.key for o in offered] == ["sensitivityLevel"]


def test_relay_settings_validate_against_their_declared_bounds():
    lists = _attr_lists({0x0006: ONOFF_WITH_LIGHTING})
    assert validate_settings(RELAY, {"startUpOnOff": "2"},
                             lambda c, a: None, lists) == {}
    errors = validate_settings(RELAY, {"startUpOnOff": "9"}, lambda c, a: None, lists)
    assert "startUpOnOff" in errors


def test_both_legs_use_the_long_attribute_timeout():
    """A sleepy Thread SED polls at ~9.5s. The write leg used to fall through to
    MatterClient.write's 5s default while only the read-back got 30s, so a
    healthy device was reported as "could not be sent" — and the code returned
    before ever reading back to discover the write had landed."""
    from matter_client import ATTRIBUTE_TIMEOUT
    client = _Client(reads=30)
    ok, _message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is True
    assert client.write_timeouts == [ATTRIBUTE_TIMEOUT]
    assert client.read_timeouts == [ATTRIBUTE_TIMEOUT]


def test_the_timeout_is_injectable_for_both_legs():
    client = _Client(reads=30)
    _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep, timeout=7.5))
    assert client.write_timeouts == [7.5]
    assert client.read_timeouts == [7.5]


def test_an_integral_float_state_seeds_as_a_whole_number():
    """Seeding "10.0" would be rejected by the field's own numeric validator on
    an untouched dialog — the plugin blaming the user for its own formatting."""
    values = config_ui_values(MOTION, {"holdTime": 10.0}, _limits())
    assert values["holdTime"] == "10"
    assert validate_settings(MOTION, values, _limits()) == {}


def test_an_untouched_seeded_dialog_always_revalidates():
    """Round-trip invariant: whatever config_ui_values seeds must pass
    validate_settings untouched, for every state shape a handler might write."""
    for state in (10, 10.0, "10", None):
        values = config_ui_values(MOTION, {"holdTime": state, "sensitivityLevel": 1},
                                  _limits())
        assert validate_settings(MOTION, values, _limits()) == {}, f"state={state!r}"


def test_a_null_read_back_is_not_reported_as_no_answer():
    """StartUpOnOff is nullable. A device holding null after we asked for 1
    definitely did not apply the write — reporting that as an unconfirmed
    timeout hides the exact firmware behaviour this feature exists to expose."""
    client = _Client(reads=None)
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert "did not answer" not in message


def test_a_read_back_failure_names_the_underlying_error():
    client = _Client(read_errors=[TimeoutError("poll window missed"),
                                  TimeoutError("poll window missed")])
    ok, message = _run(apply_setting(client, 56, 1, _plan(), sleep=_no_sleep))
    assert ok is False
    assert "poll window missed" in message


# ===========================================================================
# issue #190 — which STATES a unit should own
# ===========================================================================

def test_a_plug_without_the_lighting_feature_should_not_own_its_states():
    absent = device_settings.unimplemented_states(
        RELAY, _attr_lists({0x0006: ONOFF_WITHOUT_LIGHTING}))
    assert [s.key for s in absent] == ["startUpOnOff"]


def test_a_plug_with_the_lighting_feature_keeps_them():
    absent = device_settings.unimplemented_states(
        RELAY, _attr_lists({0x0006: ONOFF_WITH_LIGHTING}))
    assert absent == []


def test_an_unknown_attribute_list_keeps_every_state():
    """The rule that separates this from offered_settings. Withdrawing a state
    breaks any trigger bound to it, so it needs a positive NO — and there is
    always a window (device start, before the first reconcile) where nothing is
    known. Treating that as absent would make states flap on every launch."""
    assert device_settings.unimplemented_states(RELAY, _attr_lists({})) == []
    assert device_settings.unimplemented_states(RELAY, None) == []


def test_an_unparseable_attribute_list_keeps_every_state():
    assert device_settings.unimplemented_states(
        RELAY, _attr_lists({0x0006: "not a list"})) == []
    assert device_settings.unimplemented_states(
        RELAY, _attr_lists({0x0006: [0, "sixteen thousand", 16387]})) == []


def test_the_state_gate_is_weaker_than_the_offer_gate_on_purpose():
    """Same device, same unknown AttributeList, deliberately opposite answers:
    a spec-bounded setting is NOT offered without positive evidence, but its
    state is still kept. Offering an unusable field is recoverable; silently
    removing a state that a trigger references is not."""
    unknown = _attr_lists({})
    assert offered_settings(RELAY, lambda c, a: None, unknown) == []
    assert device_settings.unimplemented_states(RELAY, unknown) == []


def test_a_pre_1_4_occupancy_sensor_should_not_own_a_hold_time_state():
    lists = {0x0406: [0, 1, 2, 65531], 0x0080: [0, 1, 2, 65531]}
    absent = device_settings.unimplemented_states(MOTION, _attr_lists(lists))
    assert [s.key for s in absent] == ["holdTime"]


def test_every_setting_key_is_a_real_state_id_on_its_device_types():
    """The gate removes states BY SETTING KEY, so the two must not drift — a
    typo would silently gate nothing. Checked against Devices.xml itself."""
    import re
    from pathlib import Path
    from matter_handlers.settings import SETTINGS
    xml = Path(__file__).resolve().parents[1].joinpath(
        "indigo-matter.indigoPlugin/Contents/Server Plugin/Devices.xml").read_text()
    for setting in SETTINGS:
        for device_type in setting.device_types:
            block = re.search(rf'<Device id="{device_type}".*?</Device>', xml, re.S)
            assert block, f"no <Device id={device_type}> in Devices.xml"
            assert f'<State id="{setting.key}">' in block.group(0), (
                f'{device_type} declares no "{setting.key}" state for setting '
                f'{setting.key!r} — the #190 gate would silently do nothing')
            assert f"<UiDisplayStateId>{setting.key}</UiDisplayStateId>" not in block.group(0), (
                f'{device_type} uses "{setting.key}" as its display state, but the #190 gate '
                "can remove that state — the device list's State column would point at nothing")
