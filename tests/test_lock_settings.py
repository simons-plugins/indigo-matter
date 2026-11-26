"""DoorLock writable settings — issue #368 (AutoRelockTime, WrongCodeEntryLimit,
UserCodeTemporaryDisableTime).

Same registry mechanism as every other setting (ADR-0002), gated on the lock's
own AttributeList (ADR-0003). The ranges are the Matter model's own, as carried
by the pinned ``@matter/model`` 0.17.8 (``door-lock-cluster.element.js``):

* AutoRelockTime (0x0023) — uint32, no constraint, "0=disabled"
* WrongCodeEntryLimit (0x0030) — uint8, ``1 to 255``
* UserCodeTemporaryDisableTime (0x0031) — uint8, ``1 to 255``

Deliberately NOT settings here: OperatingMode (0x0025, needs
SupportedOperatingModes handling) and RequirePinForRemoteOperation (0x0033 —
the plugin locks and unlocks without a PIN, so enabling it would cut Indigo off
from the lock).
"""
from __future__ import annotations

import asyncio

import pytest

from device_settings import (
    PlannedWrite,
    apply_setting,
    config_ui_values,
    offered_settings,
    planned_writes,
    unimplemented_states,
    validate_settings,
)
from matter_handlers.door_lock import DoorLockHandler
from matter_handlers.settings import Bounds, settings_for_type

LOCK = "matterLock"
CLUSTER_DOOR_LOCK = 0x0101

#: The Aqara U500's DoorLock AttributeList as the issue #368 reporter dumped it
#: (settable ones: 0x23, 0x25, 0x30, 0x31, 0x33), plus the mandatory ids.
FULL_LIST = [0, 1, 2, 3, 0x23, 0x25, 0x30, 0x31, 0x33, 65528, 65529, 65531, 65532, 65533]
#: A lock that implements only AutoRelockTime.
AUTO_RELOCK_ONLY = [0, 1, 2, 3, 0x23, 65528, 65529, 65531, 65532, 65533]
#: A lock that implements none of the three.
NONE_OF_THEM = [0, 1, 2, 3, 65528, 65529, 65531, 65532, 65533]

KEYS = ["autoRelockTime", "wrongCodeEntryLimit", "userCodeTemporaryDisableTime"]


def _lists(listed):
    return lambda cluster: listed if cluster == CLUSTER_DOOR_LOCK else None


def _no_limits(_cluster, _attribute):
    return None


def _setting(key):
    return next(s for s in settings_for_type(LOCK) if s.key == key)


# --- offered ---------------------------------------------------------------

def test_a_lock_with_all_three_is_offered_all_three_with_their_ranges():
    offered = offered_settings(LOCK, _no_limits, _lists(FULL_LIST))
    assert sorted(o.setting.key for o in offered) == sorted(KEYS)
    bounds = {o.setting.key: o.bounds for o in offered}
    # uint32 with no spec constraint; the 86400 cap is OURS (one day) so the value
    # always fits an Indigo Integer state (INT4 in SQL Logger).
    assert bounds["autoRelockTime"] == Bounds(0, 86400)
    assert bounds["wrongCodeEntryLimit"] == Bounds(1, 255)         # "1 to 255"
    assert bounds["userCodeTemporaryDisableTime"] == Bounds(1, 255)


def test_a_lock_with_only_one_is_offered_only_that_one():
    offered = offered_settings(LOCK, _no_limits, _lists(AUTO_RELOCK_ONLY))
    assert [o.setting.key for o in offered] == ["autoRelockTime"]


def test_a_lock_with_none_of_them_is_offered_nothing():
    assert offered_settings(LOCK, _no_limits, _lists(NONE_OF_THEM)) == []


def test_operating_mode_and_require_pin_are_never_offered():
    """Both are in the Aqara's AttributeList and must stay un-offered."""
    declared = {(s.cluster, s.attribute) for s in settings_for_type(LOCK)}
    assert (CLUSTER_DOOR_LOCK, 0x0025) not in declared    # OperatingMode
    assert (CLUSTER_DOOR_LOCK, 0x0033) not in declared    # RequirePinForRemoteOperation
    offered = offered_settings(LOCK, _no_limits, _lists(FULL_LIST))
    assert {(o.setting.cluster, o.setting.attribute) for o in offered} == {
        (CLUSTER_DOOR_LOCK, 0x0023), (CLUSTER_DOOR_LOCK, 0x0030), (CLUSTER_DOOR_LOCK, 0x0031)}


def test_unknown_capability_is_not_offered_but_is_not_a_positive_no_either():
    """ADR-0003: None is neither 'supported' nor 'unsupported forever'.

    The field is withheld (a spec-bounded setting needs positive evidence), but
    the STATE is kept — withdrawing it would break a bound trigger."""
    assert offered_settings(LOCK, _no_limits, _lists(None)) == []
    assert offered_settings(LOCK, _no_limits, None) == []
    assert unimplemented_states(LOCK, _lists(None)) == []
    assert unimplemented_states(LOCK, None) == []
    # …and once the list arrives the very same call offers them: not forever.
    assert len(offered_settings(LOCK, _no_limits, _lists(FULL_LIST))) == 3


def test_a_positive_no_withdraws_exactly_the_missing_states():
    absent = unimplemented_states(LOCK, _lists(AUTO_RELOCK_ONLY))
    assert sorted(s.key for s in absent) == ["userCodeTemporaryDisableTime", "wrongCodeEntryLimit"]


def test_no_node_means_nothing_is_offered():
    assert offered_settings(LOCK, _no_limits, _lists(FULL_LIST), device_known=False) == []


# --- dialog seeding ---------------------------------------------------------

def test_the_dialog_shows_current_values_and_units():
    states = {"autoRelockTime": 30, "wrongCodeEntryLimit": 5, "userCodeTemporaryDisableTime": 60}
    values = config_ui_values(LOCK, states, _no_limits, attribute_list=_lists(FULL_LIST))
    assert values["hasAutoRelockTime"] == "yes"
    assert values["hasWrongCodeEntryLimit"] == "yes"
    assert values["hasUserCodeTemporaryDisableTime"] == "yes"
    assert values["hasAnySetting"] == "yes"
    assert (values["autoRelockTime"], values["wrongCodeEntryLimit"],
            values["userCodeTemporaryDisableTime"]) == ("30", "5", "60")
    assert values["autoRelockTimeRange"].endswith(" seconds")
    assert values["wrongCodeEntryLimitRange"] == "1-255 attempts"
    assert values["userCodeTemporaryDisableTimeRange"] == "1-255 seconds"


def test_an_auto_relock_of_zero_is_shown_as_the_plain_number():
    """The spec text says 0=disabled, but the dialog field shows the number; the
    meaning ("0 turns auto-relock off") is a note under the field in Devices.xml, not part of the value."""
    values = config_ui_values(LOCK, {"autoRelockTime": 0}, _no_limits,
                              attribute_list=_lists(AUTO_RELOCK_ONLY))
    assert values["autoRelockTime"] == "0"


def test_only_the_implemented_settings_are_marked_present():
    values = config_ui_values(LOCK, {}, _no_limits, attribute_list=_lists(AUTO_RELOCK_ONLY))
    assert values["hasAutoRelockTime"] == "yes"
    assert values["hasWrongCodeEntryLimit"] == "no"
    assert values["hasUserCodeTemporaryDisableTime"] == "no"


def test_the_section_hides_when_the_lock_has_none():
    values = config_ui_values(LOCK, {}, _no_limits, attribute_list=_lists(NONE_OF_THEM))
    assert values["hasAnySetting"] == "no"


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("key,bad", [
    ("wrongCodeEntryLimit", "0"),                      # spec: writing 0 is CONSTRAINT_ERROR
    ("wrongCodeEntryLimit", "256"),
    ("userCodeTemporaryDisableTime", "0"),
    ("userCodeTemporaryDisableTime", "256"),
    ("autoRelockTime", "-1"),
    ("autoRelockTime", "86401"),                       # one past our one-day cap
    ("autoRelockTime", "4294967295"),                  # legal uint32, not storable in an Indigo state
    ("autoRelockTime", "abc"),
    ("wrongCodeEntryLimit", "3.5"),
])
def test_out_of_range_and_non_numeric_input_is_a_field_error(key, bad):
    errors = validate_settings(LOCK, {key: bad}, _no_limits, _lists(FULL_LIST))
    assert key in errors and errors[key]


@pytest.mark.parametrize("key,good", [
    ("autoRelockTime", "0"), ("autoRelockTime", "86400"),
    ("wrongCodeEntryLimit", "1"), ("wrongCodeEntryLimit", "255"),
    ("userCodeTemporaryDisableTime", "1"), ("userCodeTemporaryDisableTime", "255"),
])
def test_the_boundaries_are_accepted(key, good):
    assert validate_settings(LOCK, {key: good}, _no_limits, _lists(FULL_LIST)) == {}


def test_the_error_names_the_range_and_the_unit():
    errors = validate_settings(LOCK, {"wrongCodeEntryLimit": "300"}, _no_limits, _lists(FULL_LIST))
    assert "between 1 and 255 attempts" in errors["wrongCodeEntryLimit"]


def test_a_blank_field_is_left_alone():
    assert validate_settings(LOCK, {"autoRelockTime": "  "}, _no_limits, _lists(FULL_LIST)) == {}


def test_a_setting_the_lock_lacks_is_not_validated():
    assert validate_settings(LOCK, {"wrongCodeEntryLimit": "999"}, _no_limits,
                             _lists(AUTO_RELOCK_ONLY)) == {}


def test_only_changed_values_are_planned():
    states = {"autoRelockTime": 30, "wrongCodeEntryLimit": 5}
    plans = planned_writes(
        LOCK, {"autoRelockTime": "30", "wrongCodeEntryLimit": "3"}, states,
        _no_limits, _lists(FULL_LIST))
    assert [(p.setting.key, p.value, p.previous) for p in plans] == [("wrongCodeEntryLimit", 3, 5)]


# --- the write --------------------------------------------------------------

class _Client:
    def __init__(self, reads):
        self.writes = []
        self.reads = reads

    async def write(self, write, timeout=None):
        self.writes.append(write)
        return {"status": 0}

    async def read(self, node_id, endpoint, cluster, attribute, timeout=None):
        return self.reads


async def _no_sleep(_delay):
    return None


@pytest.mark.parametrize("key,attribute,value", [
    ("autoRelockTime", 0x0023, 45),
    ("wrongCodeEntryLimit", 0x0030, 5),
    ("userCodeTemporaryDisableTime", 0x0031, 90),
])
def test_a_change_writes_the_right_attribute_and_value_to_the_right_endpoint(key, attribute, value):
    client = _Client(reads=value)
    ok, message = asyncio.run(apply_setting(
        client, 77, 1, PlannedWrite(_setting(key), value, None), sleep=_no_sleep))
    assert ok is True
    write = client.writes[0]
    assert (write.node_id, write.endpoint, write.cluster, write.attribute) == (
        77, 1, CLUSTER_DOOR_LOCK, attribute)
    assert write.value == value and type(write.value) is int       # JSON integer, not a string
    assert str(value) in message


def test_the_write_frame_on_the_wire_is_an_integer_to_the_decimal_path():
    from protocol import MatterWrite, Protocol
    frame = Protocol().build_write(MatterWrite(77, 1, CLUSTER_DOOR_LOCK, 0x0023, 45))
    assert frame["args"]["attribute_path"] == "1/257/35"
    assert frame["args"]["value"] == 45 and type(frame["args"]["value"]) is int


def test_a_lock_that_acks_and_ignores_is_a_failure_naming_both_values():
    client = _Client(reads=10)                 # kept the old value
    ok, message = asyncio.run(apply_setting(
        client, 77, 1, PlannedWrite(_setting("autoRelockTime"), 45, 10), sleep=_no_sleep))
    assert ok is False
    assert "NOT applied" in message and "45" in message and "10" in message


# --- the handler: reading the current value ---------------------------------

class _Dev:
    def __init__(self, states):
        self.states = dict(states)


def test_the_handler_surfaces_each_setting_attribute_as_its_state():
    handler = DoorLockHandler()
    dev = _Dev({"autoRelockTime": 0, "wrongCodeEntryLimit": 0, "userCodeTemporaryDisableTime": 0})
    assert handler.on_attribute_update(dev, 0x0023, 30) == {"autoRelockTime": 30}
    assert handler.on_attribute_update(dev, 0x0030, 5) == {"wrongCodeEntryLimit": 5}
    assert handler.on_attribute_update(dev, 0x0031, "60") == {"userCodeTemporaryDisableTime": 60}


def test_a_device_without_the_state_yet_is_not_written_to():
    """A lock fielded before this release has no such state until Indigo rebuilds
    its state list; writing an undeclared key would be an Indigo error."""
    assert DoorLockHandler().on_attribute_update(_Dev({}), 0x0023, 30) == {}


def test_a_null_or_junk_value_never_becomes_a_state():
    handler = DoorLockHandler()
    dev = _Dev({"autoRelockTime": 7})
    assert handler.on_attribute_update(dev, 0x0023, None) == {}
    assert handler.on_attribute_update(dev, 0x0023, "n/a") == {}


def test_lock_state_handling_is_unchanged():
    handler = DoorLockHandler()
    assert handler.on_attribute_update(_Dev({}), 0x0000, 1) == {"onOffState": True, "lockState": "locked"}
    assert handler.on_attribute_update(_Dev({}), 0x0033, True) == {}      # RequirePin: ignored


def test_the_handler_subscribes_to_the_setting_attributes():
    wanted = DoorLockHandler().attributes_to_subscribe()
    assert {0x0000, 0x0023, 0x0030, 0x0031} <= set(wanted)
    assert 0x0033 not in wanted and 0x0025 not in wanted


# ===========================================================================
# Unread settings must not look like real values (review fix pass, PR #388)
#
# Indigo initialises a new Integer state to 0 (ADR-0003, #190), and the dialog
# seeds from state. Before a lock's values have been READ, "0" is Indigo's default,
# not the lock's value. `primed(key)` is the injected "has this device's reading
# of this setting landed this run?".
# ===========================================================================

def _primed(*keys):
    return lambda key: key in keys


ALL_KEYS = tuple(KEYS)
UNPRIMED = _primed()
#: Indigo's bare default for all three, i.e. what an unread lock's states hold.
DEFAULTS = {k: 0 for k in KEYS}


def _seed(states, primed):
    return config_ui_values(LOCK, states, _no_limits, attribute_list=_lists(FULL_LIST), primed=primed)


def test_an_unprimed_lock_shows_all_three_fields_blank():
    values = _seed(DEFAULTS, UNPRIMED)
    assert [values[k] for k in KEYS] == ["", "", ""]
    assert all(values["has" + k[0].upper() + k[1:]] == "yes" for k in KEYS)   # still offered


def test_an_untouched_save_of_an_unprimed_lock_validates_and_plans_nothing():
    values = _seed(DEFAULTS, UNPRIMED)
    assert validate_settings(LOCK, values, _no_limits, _lists(FULL_LIST),
                             states=DEFAULTS, primed=UNPRIMED) == {}
    assert planned_writes(LOCK, values, DEFAULTS, _no_limits, _lists(FULL_LIST),
                          primed=UNPRIMED) == []


def test_entering_zero_for_auto_relock_on_an_unprimed_lock_is_written_and_verified_on_the_wire():
    """0 == the unread default 0, so the old `previous == value` shortcut skipped
    it silently: the user asked to turn auto-relock off and nothing happened."""
    values = {"autoRelockTime": "0"}
    plans = planned_writes(LOCK, values, DEFAULTS, _no_limits, _lists(FULL_LIST), primed=UNPRIMED)
    assert [(p.setting.key, p.value, p.previous) for p in plans] == [("autoRelockTime", 0, None)]
    client = _Client(reads=0)
    ok, _message = asyncio.run(apply_setting(client, 77, 1, plans[0], sleep=_no_sleep))
    assert ok is True
    write = client.writes[0]
    assert (write.endpoint, write.cluster, write.attribute, write.value) == (1, 0x0101, 0x0023, 0)


def test_a_primed_zero_is_shown_as_zero_and_not_rewritten():
    primed = _primed("autoRelockTime")
    values = _seed({"autoRelockTime": 0}, primed)
    assert values["autoRelockTime"] == "0"
    assert planned_writes(LOCK, values, {"autoRelockTime": 0}, _no_limits, _lists(FULL_LIST),
                          primed=primed) == []


def test_a_primed_lock_holding_zero_attempts_saves_untouched_and_only_the_change_is_written():
    """The spec says writing 0 to WrongCodeEntryLimit is a CONSTRAINT_ERROR, but a
    lock may HOLD one: a value we did not change must never block the dialog."""
    states = {"autoRelockTime": 60, "wrongCodeEntryLimit": 0, "userCodeTemporaryDisableTime": 60}
    primed = _primed(*ALL_KEYS)
    values = _seed(states, primed)
    assert values["wrongCodeEntryLimit"] == "0"
    assert validate_settings(LOCK, values, _no_limits, _lists(FULL_LIST),
                             states=states, primed=primed) == {}
    values["autoRelockTime"] = "90"
    assert validate_settings(LOCK, values, _no_limits, _lists(FULL_LIST),
                             states=states, primed=primed) == {}
    plans = planned_writes(LOCK, values, states, _no_limits, _lists(FULL_LIST), primed=primed)
    assert [(p.setting.key, p.value) for p in plans] == [("autoRelockTime", 90)]


def test_a_lock_holding_more_than_our_one_day_cap_does_not_block_the_dialog():
    states = {"autoRelockTime": 100000}
    primed = _primed("autoRelockTime")
    values = _seed(states, primed)
    assert values["autoRelockTime"] == "100000"
    assert validate_settings(LOCK, values, _no_limits, _lists(FULL_LIST),
                             states=states, primed=primed) == {}
    # ...but TYPING an out-of-range value is still refused.
    errors = validate_settings(LOCK, {"autoRelockTime": "100001"}, _no_limits, _lists(FULL_LIST),
                               states=states, primed=primed)
    assert "autoRelockTime" in errors
    assert "0 and 86400" in errors["autoRelockTime"]


def test_the_auto_relock_range_hint_states_the_cap_and_unit():
    values = _seed(DEFAULTS, UNPRIMED)
    assert values["autoRelockTimeRange"] == "0-86400 seconds"


def test_an_unread_picker_is_still_offered_and_seeded_to_its_leave_alone_row():
    """A menu cannot be blank, and hiding it took the section away with no
    explanation. It leads with a "not read yet" row instead (see test_unread_settings)."""
    relay_list = lambda cluster: [0, 0x4003, 65531] if cluster == 0x0006 else None   # noqa: E731
    values = config_ui_values("matterRelay", {"startUpOnOff": 0}, _no_limits,
                              attribute_list=relay_list, primed=UNPRIMED)
    assert values["hasStartUpOnOff"] == "yes" and values["hasAnySetting"] == "yes"
    assert values["startUpOnOff"] == "unread"
    assert planned_writes("matterRelay", {"startUpOnOff": "unread"}, {"startUpOnOff": 0},
                          _no_limits, relay_list, primed=UNPRIMED) == []
    shown = config_ui_values("matterRelay", {"startUpOnOff": 1}, _no_limits,
                             attribute_list=relay_list, primed=_primed("startUpOnOff"))
    assert shown["hasStartUpOnOff"] == "yes" and shown["startUpOnOff"] == "1"


def test_callers_that_pass_no_primed_lookup_keep_the_old_behaviour():
    values = config_ui_values(LOCK, {"autoRelockTime": 30}, _no_limits,
                              attribute_list=_lists(FULL_LIST))
    assert values["autoRelockTime"] == "30"
