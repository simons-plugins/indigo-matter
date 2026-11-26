"""Test-review findings G-I on PR #388 (each pins a mutation that used to survive)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import device_settings
from matter_handlers.settings import settings_for_type

from test_device_sync import (  # noqa: F401
    indigo_env, ds, plugin_cls, LOCK_SETTINGS_NODE, LIGHTING_PLUG_NODE,
)
from test_review_pass3 import _Matter, _run_apply, _live
from test_unread_settings import _stub, _open, _lock
from test_device_sync_replace import coffee  # noqa: F401

ARL = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
WCL = next(s for s in settings_for_type("matterLock") if s.key == "wrongCodeEntryLimit")
SUS = next(s for s in settings_for_type("matterRelay") if s.key == "startUpOnOff")
CONNECTED = SimpleNamespace(connected=True)
DISCONNECTED = SimpleNamespace(connected=False)


def _refuse(read=30):
    return _Matter(write=RuntimeError("Write failed: CONSTRAINT_ERROR (0x87)"), read=read)


def _text(call):
    args = call.args
    return args[0] % args[1:] if len(args) > 1 else str(args[0])


# --- G: a failed clear is retried -------------------------------------------------------

def test_a_failed_clear_is_retried_by_the_next_matching_reading(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _run_apply(plugin_cls, _stub(plugin_cls, ds, mock_logger, matter=_refuse(30)), dev,
               device_settings.PlannedWrite(ARL, 99999, 30))
    real, state = dev.updateStateOnServer, {"fail": True}

    def flaky(key, *a, **kw):
        if key == "settingsError" and state["fail"]:
            state["fail"] = False
            raise RuntimeError("Indigo busy")
        return real(key, *a, **kw)

    dev.updateStateOnServer = flaky
    _live(ds, 99999)
    assert dev.states["settingsError"] != ""                     # the clear failed once
    _live(ds, 99999)                                             # the device still holds it
    assert dev.states["settingsError"] == ""


def test_a_picker_failure_is_cleared_by_a_later_reading_of_the_requested_value(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    raw = LIGHTING_PLUG_NODE
    ds.create_from_raw(raw, "Lamp")
    dev = devices[ds.lookup(raw["node_id"], 1, "matterRelay")]
    _run_apply(plugin_cls, _stub(plugin_cls, ds, mock_logger, matter=_refuse(1)), dev,
               device_settings.PlannedWrite(SUS, 0, 1), node=raw["node_id"])
    assert dev.states["settingsError"].startswith(SUS.label)
    _live(ds, 0, attribute=0x4003, node=raw["node_id"], cluster=0x0006)
    assert dev.states["startUpOnOff"] == 0
    assert dev.states["settingsError"] == ""


# --- H -----------------------------------------------------------------------------------

def test_every_edited_field_gets_the_refusal_not_just_the_first(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=DISCONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"], values["wrongCodeEntryLimit"] = "60", "4"
    result = plugin_cls.validateDeviceConfigUi(stub, values, "matterLock", dev.id)
    assert result[0] is False
    assert {"autoRelockTime", "wrongCodeEntryLimit"} <= set(result[2].keys())
    assert "Not connected" in result[2]["autoRelockTime"] and "Not connected" in result[2]["wrongCodeEntryLimit"]


def test_garbage_in_an_unread_number_field_is_an_edit_and_is_refused(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds._settings_primed.clear()
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert values["autoRelockTime"] == ""
    values["autoRelockTime"] = "abc"
    result = plugin_cls.validateDeviceConfigUi(stub, values, "matterLock", dev.id)
    assert result[0] is False and "whole number" in result[2]["autoRelockTime"]


def test_the_settingsError_write_fires_triggers(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _run_apply(plugin_cls, _stub(plugin_cls, ds, mock_logger, matter=_refuse(30)), dev,
               device_settings.PlannedWrite(ARL, 99999, 30))
    calls = [c for c in dev.single_key_calls if c["key"] == "settingsError"]
    assert calls and all(c["triggerEvents"] is True for c in calls)


def test_a_good_write_with_nothing_recorded_does_not_rewrite_settingsError(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _run_apply(plugin_cls, _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=40)), dev,
               device_settings.PlannedWrite(ARL, 40, 30))
    assert [c for c in dev.single_key_calls if c["key"] == "settingsError"] == []


def test_a_failure_with_no_cause_still_records_the_generic_sentence(plugin_cls, ds, indigo_env, mock_logger, monkeypatch):
    _i, devices = indigo_env
    dev = _lock(ds, devices)

    async def silent_fail(*a, **k):
        return False, "Auto-relock time was NOT applied"       # never calls on_cause

    monkeypatch.setattr(device_settings, "apply_setting", silent_fail)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_refuse(30))
    asyncio.run(plugin_cls._apply_setting(stub, 0x39, 1, dev.id, device_settings.PlannedWrite(ARL, 99999, 30)))
    assert dev.states["settingsError"] == "Auto-relock time: the write failed"


def test_a_build_failure_after_seeding_forgets_the_seed_and_resets_the_error_fields(
        plugin_cls, ds, indigo_env, mock_logger, monkeypatch):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds.record_setting_failure(dev.id, ARL, 1, "cause")
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    import plugin as plugin_module
    real = plugin_module._matter_connected

    def boom(_plugin):                    # raises AFTER _remember_seed has already run
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_module, "_matter_connected", boom)
    values = _open(plugin_cls, stub, dev, "matterLock")
    monkeypatch.setattr(plugin_module, "_matter_connected", real)
    assert dev.id not in vars(stub).get("_dialog_seeds", {})
    assert values["settingsErrorShown"] == "no" and values["settingsErrorText"] == ""


def test_no_seed_and_every_field_blank_logs_nothing(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    stub.runtime = SimpleNamespace(submit=lambda coro: coro.close())
    blank = {**dev.pluginProps, "autoRelockTime": "", "wrongCodeEntryLimit": "",
             "userCodeTemporaryDisableTime": ""}
    plugin_cls.closedDeviceConfigUi(stub, blank, False, "matterLock", dev.id)
    assert not mock_logger.warning.called and not mock_logger.error.called


def test_a_timed_out_send_with_the_old_value_read_back_says_ignored_not_rejected(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _run_apply(plugin_cls, _stub(plugin_cls, ds, mock_logger, matter=_Matter(write=TimeoutError("t"), read=30)),
               dev, device_settings.PlannedWrite(ARL, 99999, 30))
    text = dev.states["settingsError"]
    assert text == "Auto-relock time: the device ignored the change (still 30 seconds)"
    assert "rejected" not in text


def test_an_acked_and_ignored_write_says_exactly_that(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _run_apply(plugin_cls, _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=30)),
               dev, device_settings.PlannedWrite(ARL, 45, 30))
    assert dev.states["settingsError"] == "Auto-relock time: the device ignored the change (still 30 seconds)"


def test_the_short_cause_strips_semicolons_and_newlines():
    odd = SimpleNamespace(value_text=lambda value, bounds=None: "a; b\nc")
    cause = device_settings._short_cause(odd, 1, 2, RuntimeError("x"))
    assert ";" not in cause and "\n" not in cause


def test_the_prefix_filter_needs_the_colon_so_hold_and_hold_time_stay_separate(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    hold, hold_time = SimpleNamespace(label="Hold", key="hold"), SimpleNamespace(label="Hold time", key="holdTime")
    assert ds._edit_settings_error(dev.id, hold_time, "Hold time: a")
    assert ds._edit_settings_error(dev.id, hold, "Hold: b")
    assert ds._edit_settings_error(dev.id, hold, None)           # clearing "Hold" ...
    assert dev.states["settingsError"] == "Hold time: a"          # ... keeps "Hold time"


def test_connected_but_not_yet_reconciled_logs_the_one_time_line(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds._attribute_lists.clear()
    ds._setting_readings.clear()                                  # nothing read since the plugin started
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    _open(plugin_cls, stub, dev, "matterLock")
    lines = [_text(c) for c in mock_logger.info.call_args_list]
    assert sum("once the plugin has connected" in line for line in lines) == 1
    assert not any("has not reported an AttributeList" in line for line in lines)


# --- I -----------------------------------------------------------------------------------

def test_a_lock_fielded_without_the_state_yet_logs_the_error_and_warns_once(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    del dev.states["settingsError"]
    stub = _stub(plugin_cls, ds, mock_logger, matter=_refuse(30))
    for _ in range(2):
        _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 99999, 30))
    assert "settingsError" not in dev.states
    assert any("CONSTRAINT_ERROR" in _text(c) and dev.name in _text(c) for c in mock_logger.error.call_args_list)
    warnings = [_text(c) for c in mock_logger.warning.call_args_list if "settingsError" in _text(c)]
    assert len(warnings) == 1 and dev.name in warnings[0]


def test_node_replace_leaves_no_failure_memory_that_a_later_reading_could_act_on(ds, coffee):
    from test_device_sync_replace import OLD_NODE  # noqa: F401
    relay = coffee.old_relay
    ds.record_setting_failure(relay.id, SUS, 1, "cause")
    assert relay.states["settingsError"]
    ds.replace_node(coffee.plan)
    assert relay.states["settingsError"] == ""
    before = [c for c in relay.single_key_calls if c["key"] == "settingsError"]
    ds.apply_states(relay.id, [{"key": "startUpOnOff", "value": 1}])      # the value that was asked for
    assert [c for c in relay.single_key_calls if c["key"] == "settingsError"] == before
