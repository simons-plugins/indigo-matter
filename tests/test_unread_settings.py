"""'Read this run' mechanism - second fix pass on PR #388.

Converted from the reviewer's probes P1-P9: each asserts the CORRECT behaviour.

* a cached reading must never overwrite newer state (P3, P9) or the error flag (P8);
* an unread picker is shown with a "not read yet" row, never hidden (P1, P2, P5);
* an unread/withheld field never carries an old saved prop into a write (P4);
* a node that is unavailable still shows the last values it reported (P2);
* every setting state write carries an explicit uiValue.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest

import device_settings
import protocol
from matter_handlers.settings import settings_for_type
from protocol import MatterEvent

from test_device_sync import (  # noqa: F401  (fixtures and node fixtures)
    indigo_env, ds, plugin_cls, LIGHTING_PLUG_NODE, OCCUPANCY_SENSITIVITY_NODE,
    LOCK_SETTINGS_NODE, _LOCK_SETTING_STATES,
)

UNREAD_LABEL = "Not read from the device yet (leave unchanged)"


def _stub(plugin_cls, ds, mock_logger, **extra):
    stub = SimpleNamespace(logger=mock_logger, device_sync=ds, **extra)
    stub._setting_limits_lookup = lambda p: plugin_cls._setting_limits_lookup(stub, p)
    stub._setting_attribute_list_lookup = lambda p: plugin_cls._setting_attribute_list_lookup(stub, p)
    stub._setting_primed_lookup = lambda d: plugin_cls._setting_primed_lookup(stub, d)
    stub._device_states = lambda d: plugin_cls._device_states(stub, d)
    stub._has_matter_node = plugin_cls._has_matter_node
    stub._log_setting_skips = lambda d: plugin_cls._log_setting_skips(stub, d)
    stub._apply_setting = lambda *a: plugin_cls._apply_setting(stub, *a)
    return stub


def _start(plugin_cls, ds, mock_logger, dev):
    stub = SimpleNamespace(logger=mock_logger, device_sync=ds,
                           _prime_retired_on_time_state=lambda d: None)
    plugin_cls.deviceStartComm(stub, dev)


def _open(plugin_cls, stub, dev, type_id):
    values, _ = plugin_cls.getDeviceConfigUiValues(stub, dict(dev.pluginProps), type_id, dev.id)
    return values


def _rows(plugin_cls, stub, dev, type_id, key):
    return plugin_cls.getSettingOptions(
        stub, filter=key, valuesDict=dict(dev.pluginProps), typeId=type_id, targetId=dev.id)


def _lock(ds, devices):
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    dev.pluginProps.update({"createdTypeId": "matterLock"})
    return dev


def _sensor(ds, devices):
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "FP300")
    dev = devices[ds.lookup(0x2D, 1)]
    dev.pluginProps["createdTypeId"] = "matterMotionSensor"
    return dev


# --- 1. a cached reading never overwrites newer state ----------------------

def test_P3_a_live_change_survives_a_device_restart(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    ds.handle_event(MatterEvent(kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=0x2D, endpoint=1,
                                cluster=0x0080, attribute=0, value=2))
    assert dev.states["sensitivityLevel"] == 2
    _start(plugin_cls, ds, mock_logger, dev)
    assert dev.states["sensitivityLevel"] == 2


def test_P9_a_confirmed_dialog_write_survives_a_device_restart(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    dev.pluginProps.update({"nodeId": "57", "endpointId": "1"})

    class _Matter:
        async def write(self, write, timeout=None):
            return None

        async def read(self, *a, **k):
            return 45

    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter())
    plan = device_settings.PlannedWrite(
        next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime"), 45, 30)
    original = device_settings.apply_setting

    async def quick(*a, **k):
        async def no_sleep(_d):
            return None
        return await original(*a, sleep=no_sleep, **k)

    device_settings.apply_setting = quick
    try:
        asyncio.run(plugin_cls._apply_setting(stub, 57, 1, dev.id, plan))
    finally:
        device_settings.apply_setting = original
    assert dev.states["autoRelockTime"] == 45
    _start(plugin_cls, ds, mock_logger, dev)
    assert dev.states["autoRelockTime"] == 45
    assert ds._setting_readings[0x39][(1, 0x0101, 0x0023)] == 45      # the cache is current too


def test_a_device_start_makes_no_state_write_at_all_for_settings_already_read(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)

    def fatal(_kv):
        raise AssertionError("deviceStartComm wrote a state for a setting already read")

    dev.updateStatesOnServer = fatal
    _start(plugin_cls, ds, mock_logger, dev)          # must not raise
    assert not mock_logger.warning.called


def test_a_device_start_still_primes_a_setting_that_is_not_yet_read(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds._settings_primed.discard((dev.id, "wrongCodeEntryLimit"))
    dev.states["wrongCodeEntryLimit"] = 0
    dev.states["autoRelockTime"] = 99                 # a newer live value for a READ key
    _start(plugin_cls, ds, mock_logger, dev)
    assert dev.states["wrongCodeEntryLimit"] == 5
    assert dev.states["autoRelockTime"] == 99


# --- 2. an unread picker is shown with a "not read yet" row ----------------

def test_P5_a_setting_marked_unread_gets_its_unread_row_back(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    stub = _stub(plugin_cls, ds, mock_logger)
    assert _rows(plugin_cls, stub, dev, "matterRelay", "startUpOnOff")[0][0] == "0"   # read: no row
    ds.mark_setting_unread(dev.id, "startUpOnOff")
    values = _open(plugin_cls, stub, dev, "matterRelay")
    assert values["hasStartUpOnOff"] == "yes"
    assert _rows(plugin_cls, stub, dev, "matterRelay", "startUpOnOff")[0] == (
        device_settings.UNREAD_OPTION_ID, UNREAD_LABEL)


def test_the_unread_row_is_not_offered_once_the_setting_is_read(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    rows = _rows(plugin_cls, _stub(plugin_cls, ds, mock_logger), dev, "matterRelay", "startUpOnOff")
    assert all(row_id != device_settings.UNREAD_OPTION_ID for row_id, _l in rows)
    assert _open(plugin_cls, _stub(plugin_cls, ds, mock_logger), dev, "matterRelay")["startUpOnOff"] == "1"


def test_choosing_the_unread_row_changes_nothing(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    ds.mark_setting_unread(dev.id, "startUpOnOff")
    stub = _stub(plugin_cls, ds, mock_logger)
    values = {**_open(plugin_cls, stub, dev, "matterRelay"),
              "startUpOnOff": device_settings.UNREAD_OPTION_ID}
    assert device_settings.validate_settings(
        "matterRelay", values, stub._setting_limits_lookup(values),
        stub._setting_attribute_list_lookup(values), True,
        states=stub._device_states(dev.id), primed=stub._setting_primed_lookup(dev.id)) == {}
    assert device_settings.planned_writes(
        "matterRelay", values, stub._device_states(dev.id), stub._setting_limits_lookup(values),
        stub._setting_attribute_list_lookup(values), True,
        primed=stub._setting_primed_lookup(dev.id)) == []


def test_choosing_a_real_option_on_an_unread_picker_is_written(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    ds.mark_setting_unread(dev.id, "startUpOnOff")
    stub = _stub(plugin_cls, ds, mock_logger)
    values = {**_open(plugin_cls, stub, dev, "matterRelay"), "startUpOnOff": "0"}
    plans = device_settings.planned_writes(
        "matterRelay", values, stub._device_states(dev.id), stub._setting_limits_lookup(values),
        stub._setting_attribute_list_lookup(values), True,
        primed=stub._setting_primed_lookup(dev.id))
    assert [(p.setting.key, p.value, p.previous) for p in plans] == [("startUpOnOff", 0, None)]


# --- 3. an unread/withheld field never carries an old saved prop -----------

def test_P4_a_stale_saved_prop_is_never_written_when_the_reading_lands_mid_dialog(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    dev.pluginProps["startUpOnOff"] = "0"            # saved by an earlier dialog session
    ds._settings_primed.clear()                      # restart, not yet reconciled
    stub = _stub(plugin_cls, ds, mock_logger)
    values = _open(plugin_cls, stub, dev, "matterRelay")
    assert values["startUpOnOff"] == device_settings.UNREAD_OPTION_ID      # not the stale "0"
    ds.reconcile_all([LIGHTING_PLUG_NODE])           # the reading arrives while the dialog is open
    plans = device_settings.planned_writes(
        "matterRelay", values, stub._device_states(dev.id), stub._setting_limits_lookup(values),
        stub._setting_attribute_list_lookup(values), True,
        primed=stub._setting_primed_lookup(dev.id))
    assert plans == []


def test_a_stale_numeric_prop_is_blanked_when_the_setting_is_unread(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    dev.pluginProps["autoRelockTime"] = "0"
    ds._settings_primed.clear()
    values = _open(plugin_cls, _stub(plugin_cls, ds, mock_logger), dev, "matterLock")
    assert values["autoRelockTime"] == ""


def test_a_stale_prop_is_blanked_for_a_setting_the_device_does_not_offer(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    dev.pluginProps["startUpOnOff"] = "2"
    values = device_settings.config_ui_values(
        "matterRelay", {}, lambda c, a: None, values=dict(dev.pluginProps),
        attribute_list=lambda cluster: [0, 65531], primed=lambda key: True)
    assert values["startUpOnOff"] == "" and values["hasStartUpOnOff"] == "no"


# --- 4. re-priming never clears the error state ----------------------------

def test_P8_a_device_start_keeps_an_unreachable_device_flagged_unreachable(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    ds._settings_primed.clear()
    node = copy.deepcopy(OCCUPANCY_SENSITIVITY_NODE)
    node["available"] = False
    ds.reconcile_all([node])
    flagged = dev.errorState
    assert flagged                                        # sanity: it IS flagged
    _start(plugin_cls, ds, mock_logger, dev)
    assert dev.errorState == flagged


def test_P8_a_re_prime_that_really_writes_still_keeps_the_device_flagged_unreachable(
        plugin_cls, ds, indigo_env, mock_logger):
    """The setting is unread, so deviceStartComm DOES write from the cache - and a
    cached reading is not evidence the device is reachable."""
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    dev.setErrorStateOnServer("unreachable")
    ds._settings_primed.clear()
    dev.states["sensitivityLevel"] = 0
    _start(plugin_cls, ds, mock_logger, dev)
    assert dev.states["sensitivityLevel"] == 1          # it did write
    assert dev.errorState == "unreachable"


def test_a_live_reading_still_clears_a_stale_error_flag(ds, indigo_env):
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    dev.setErrorStateOnServer("unreachable")
    ds.handle_event(MatterEvent(kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=0x2D, endpoint=1,
                                cluster=0x0080, attribute=0, value=2))
    assert dev.errorState == ""


# --- 5. an unavailable node's settings show the last values it reported ----

def test_P2_an_unavailable_nodes_settings_are_primed_from_its_snapshot(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    ds._settings_primed.clear()                           # plugin restart
    dev.states["sensitivityLevel"] = 0                    # Indigo's default
    dev.states["onOffState"] = False
    node = copy.deepcopy(OCCUPANCY_SENSITIVITY_NODE)
    node["available"] = False
    ds.reconcile_all([node])
    assert dev.states["sensitivityLevel"] == 1
    assert ds.setting_primed(dev.id, "sensitivityLevel")
    assert dev.states["onOffState"] is False              # live states are NOT primed
    assert dev.errorState                                 # and it stays unreachable
    values = _open(plugin_cls, _stub(plugin_cls, ds, mock_logger), dev, "matterMotionSensor")
    assert values["hasSensitivity"] == "yes" and values["sensitivityLevel"] == "1"


def test_an_unavailable_node_with_an_empty_snapshot_primes_nothing(ds, indigo_env):
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    ds._settings_primed.clear()
    dev.states["sensitivityLevel"] = 0
    ds.reconcile_all([{"node_id": 0x2D, "available": False, "attributes": {}}])
    assert dev.states["sensitivityLevel"] == 0
    assert not ds.setting_primed(dev.id, "sensitivityLevel")


# --- 6. every setting state write carries an explicit uiValue --------------

def test_a_setting_reading_carries_its_number_and_unit_as_the_display_value(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    assert dev.states["autoRelockTime.ui"] == "30 seconds"
    assert dev.states["wrongCodeEntryLimit.ui"] == "5 attempts"
    assert dev.states["userCodeTemporaryDisableTime.ui"] == "60 seconds"


def test_a_picker_setting_reading_carries_the_option_label_as_its_display_value(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    assert dev.states["startUpOnOff.ui"] == "On"
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "FP300")
    sensor = devices[ds.lookup(0x2D, 1)]
    assert sensor.states["sensitivityLevel.ui"] == "Standard"


# --- 7. small ---------------------------------------------------------------

def test_a_null_read_back_makes_the_setting_unread(plugin_cls, ds, indigo_env, mock_logger):
    """For a setting with NO declared null state (see test_stacked_388_387 for one with)."""
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    dev.setErrorStateOnServer = lambda v: None
    dev.pluginProps.update({"nodeId": "57", "endpointId": "1"})

    class _Matter:
        async def write(self, write, timeout=None):
            return None

        async def read(self, *a, **k):
            return None                                   # the device answers with a null

    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter())
    plan = device_settings.PlannedWrite(
        next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime"), 45, 30)
    asyncio.run(plugin_cls._apply_setting(stub, 57, 1, dev.id, plan))
    assert not ds.setting_primed(dev.id, "autoRelockTime")


def test_a_failing_reprime_and_a_failing_flag_are_reported_separately_with_a_traceback(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)

    def boom(_dev):
        raise RuntimeError("boom")

    ds.reprime_settings = boom
    flagged = []
    ds.flag_unread_settings = lambda d: flagged.append(d.id)
    _start(plugin_cls, ds, mock_logger, dev)
    assert flagged == [dev.id], "the second step must still run after the first fails"
    assert mock_logger.warning.call_args.kwargs.get("exc_info") is True


def test_marking_a_setting_unread_writes_through_the_sanctioned_seam(ds, indigo_env):
    """No bare dev.updateStatesOnServer outside apply_states (module docstring)."""
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    seen = []
    original = ds.apply_states
    ds.apply_states = lambda dev_id, kv, **kw: (seen.append((kv, kw)), original(dev_id, kv, **kw))[1]
    ds.mark_setting_unread(dev.id, "autoRelockTime")
    assert seen and seen[0][0][0]["uiValue"] == "unknown"
    assert seen[0][1].get("clear_error") is False


# --- numeric seeded-equality (reviewer's P6) -------------------------------

def test_P6_a_typed_value_numerically_equal_to_the_held_one_is_left_alone():
    lock_list = lambda cluster: [0, 0x30, 65531] if cluster == 0x0101 else None   # noqa: E731
    states = {"wrongCodeEntryLimit": 0}
    for typed in ("0", " 0 ", "00"):
        errors = device_settings.validate_settings(
            "matterLock", {"wrongCodeEntryLimit": typed}, lambda c, a: None, lock_list,
            states=states, primed=lambda key: True)
        assert errors == {}, typed
