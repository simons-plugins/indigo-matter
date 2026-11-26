"""A failed setting write is recorded on the setting (the String state ``settingsError``),
never on the device error flag, which is for reachability only (owner decision, PR #388).
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import device_settings
import protocol
from matter_handlers.settings import settings_for_type
from protocol import MatterEvent

from test_device_sync import (  # noqa: F401
    indigo_env, ds, plugin_cls, LOCK_SETTINGS_NODE,
)
from test_review_pass3 import _Matter, _run_apply, _live
from test_unread_settings import _stub, _open, _lock
from test_device_sync_replace import coffee  # noqa: F401

ARL = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
WCL = next(s for s in settings_for_type("matterLock") if s.key == "wrongCodeEntryLimit")
CONNECTED = SimpleNamespace(connected=True)


class _FatalFlag:
    """Makes ``setErrorStateOnServer`` fatal: a failed setting write must never touch it."""

    def __init__(self, dev):
        self.calls = []
        real = dev.setErrorStateOnServer

        def guard(value):
            self.calls.append(value)
            raise AssertionError("a setting failure must not set the device error flag")
        dev.setErrorStateOnServer = guard
        self.real = real


def _refuse(read=30):
    return _Matter(write=RuntimeError("Write failed: CONSTRAINT_ERROR (0x87)"), read=read)


def _fail_arl(plugin_cls, ds, dev, mock_logger, value=99999, matter=None):
    stub = _stub(plugin_cls, ds, mock_logger, matter=matter or _refuse(30))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, value, 30))
    return stub


# --- the state exists on every settings-bearing type ----------------------------

def test_every_type_with_settings_declares_the_string_state_and_the_dialog_fields():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    root = ET.parse(Path(__file__).resolve().parent.parent / "indigo-matter.indigoPlugin" / "Contents"
                    / "Server Plugin" / "Devices.xml").getroot()
    seen = 0
    for device in root.findall("Device"):
        if not settings_for_type(device.get("id")):
            continue
        seen += 1
        states = {st.get("id"): st for st in device.find("States").findall("State")}
        assert "settingsError" in states, device.get("id")
        assert states["settingsError"].find("ValueType").text == "String"
        fields = {f.get("id"): f for f in device.find("ConfigUI").findall("Field")}
        assert fields["settingsErrorShown"].get("hidden") == "true"
        text = fields["settingsErrorText"]
        assert text.get("readonly") == "yes" and text.get("visibleBindingId") == "settingsErrorShown"
    assert seen == 6


# --- a failure never touches the device error flag ------------------------------

def test_a_failed_write_on_a_healthy_lock_leaves_the_flag_and_the_display_alone(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    before = (dev.states["onOffState"], dev.states["lockState"])
    fatal = _FatalFlag(dev)
    _fail_arl(plugin_cls, ds, dev, mock_logger)
    assert fatal.calls == [] and dev.errorState == ""
    assert (dev.states["onOffState"], dev.states["lockState"]) == before
    assert mock_logger.error.called                        # the ERROR line stays


def test_a_failure_while_the_device_is_unreachable_keeps_that_flag_and_still_records(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    dev.setErrorStateOnServer("unreachable")
    _fail_arl(plugin_cls, ds, dev, mock_logger,
              matter=_Matter(write=TimeoutError("timed out"), read=TimeoutError("timed out")))
    assert dev.errorState == "unreachable"
    assert dev.states["settingsError"].startswith("Auto-relock time:")


# --- what is recorded ----------------------------------------------------------

def test_the_failure_names_the_setting_and_the_cause_and_the_state_keeps_the_real_value(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail_arl(plugin_cls, ds, dev, mock_logger)
    text = dev.states["settingsError"]
    assert text.startswith("Auto-relock time: the device rejected 99999")
    assert dev.states["autoRelockTime"] == 30              # the read-back value, not the request


def test_with_no_read_back_the_previous_value_is_kept_and_the_cause_is_unconfirmed(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail_arl(plugin_cls, ds, dev, mock_logger, value=45,
              matter=_Matter(read=TimeoutError("no answer")))
    assert dev.states["autoRelockTime"] == 30
    assert "did not confirm" in dev.states["settingsError"]


def test_a_firmware_that_acks_and_ignores_says_so(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail_arl(plugin_cls, ds, dev, mock_logger, value=45, matter=_Matter(read=30))
    assert "ignored" in dev.states["settingsError"] or "still 30" in dev.states["settingsError"]


def test_the_write_of_the_text_is_no_clear_and_carries_an_explicit_ui_value(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    dev.setErrorStateOnServer("unreachable")
    _fail_arl(plugin_cls, ds, dev, mock_logger)
    calls = [c for c in dev.single_key_calls if c["key"] == "settingsError"]
    assert calls and calls[-1]["clearErrorState"] is False
    assert calls[-1]["uiValue"] == calls[-1]["value"]
    assert dev.errorState == "unreachable"


# --- per setting: several failures, partial success, clearing ------------------------

def _fail(plugin_cls, ds, dev, mock_logger, setting, value, previous, matter):
    stub = _stub(plugin_cls, ds, mock_logger, matter=matter)
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(setting, value, previous))
    return stub


def test_two_failures_are_joined_and_one_settings_success_clears_only_its_own(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail(plugin_cls, ds, dev, mock_logger, ARL, 99999, 30, _refuse(30))
    _fail(plugin_cls, ds, dev, mock_logger, WCL, 200, 5, _refuse(5))
    text = dev.states["settingsError"]
    assert "Auto-relock time:" in text and "Wrong-code attempt limit:" in text and "; " in text
    ok = _Matter(read=40)
    _fail(plugin_cls, ds, dev, mock_logger, ARL, 40, 30, ok)       # succeeds
    text = dev.states["settingsError"]
    assert "Auto-relock time:" not in text and "Wrong-code attempt limit:" in text


def test_one_success_and_one_failure_in_one_save_record_only_the_failure(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail(plugin_cls, ds, dev, mock_logger, ARL, 40, 30, _Matter(read=40))
    assert dev.states["settingsError"] == ""
    _fail(plugin_cls, ds, dev, mock_logger, WCL, 200, 5, _refuse(5))
    assert dev.states["settingsError"].startswith("Wrong-code attempt limit:")
    assert dev.states["autoRelockTime"] == 40


def test_an_unrelated_state_report_does_not_clear_it(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail_arl(plugin_cls, ds, dev, mock_logger)
    _live(ds, 2, attribute=0x0000)                          # a lockState report
    _live(ds, 7, attribute=0x0030)                          # another setting's reading
    assert dev.states["settingsError"].startswith("Auto-relock time:")


def test_the_device_later_reporting_the_requested_value_clears_it(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail_arl(plugin_cls, ds, dev, mock_logger, value=99999)
    _live(ds, 99999)
    assert dev.states["settingsError"] == ""
    assert dev.states["settingsError.ui"] == "none"


def test_after_a_restart_a_read_of_the_old_value_keeps_the_text_and_a_good_write_clears_it(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _fail_arl(plugin_cls, ds, dev, mock_logger)
    text = dev.states["settingsError"]
    ds._failed_requests.clear()                              # a new run: memory gone, the state stays
    ds._settings_primed.clear()
    _live(ds, 30)                                            # still the old value
    assert dev.states["settingsError"] == text
    _fail(plugin_cls, ds, dev, mock_logger, ARL, 40, 30, _Matter(read=40))
    assert dev.states["settingsError"] == ""


# --- degradation ---------------------------------------------------------------

def test_a_failing_write_of_the_text_still_logs_the_original_failure_and_loses_nothing_else(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    real = dev.updateStateOnServer

    def flaky(key, *a, **kw):
        if key == "settingsError":
            raise RuntimeError("Indigo refused the text")
        return real(key, *a, **kw)

    dev.updateStateOnServer = flaky
    _fail_arl(plugin_cls, ds, dev, mock_logger)
    assert "CONSTRAINT_ERROR" in str(mock_logger.error.call_args)   # the original, still logged
    assert dev.states["autoRelockTime"] == 30                         # the real value still recorded
    assert dev.errorState == ""


def test_a_lock_fielded_without_the_state_yet_is_not_an_error(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    del dev.states["settingsError"]
    _fail_arl(plugin_cls, ds, dev, mock_logger)                     # must not raise
    assert "settingsError" not in dev.states


# --- the dialog ----------------------------------------------------------------

def test_the_dialog_shows_the_outstanding_failure_and_validation_strips_the_dialog_only_fields(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _fail_arl(plugin_cls, ds, dev, mock_logger)
    stub.matter = CONNECTED
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert values["settingsErrorShown"] == "yes"
    assert values["settingsErrorText"] == dev.states["settingsError"]
    result = plugin_cls.validateDeviceConfigUi(stub, dict(values), "matterLock", dev.id)
    assert "settingsErrorText" not in result[1] and "settingsErrorShown" not in result[1]


def test_a_dialog_with_nothing_failed_hides_the_line(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert values["settingsErrorShown"] == "no" and values["settingsErrorText"] == ""


# --- follow-ups on fe2ddd3 -----------------------------------------------------------

def test_apply_setting_itself_raising_still_records_the_failure_and_marks_the_setting_unread(
        plugin_cls, ds, indigo_env, mock_logger, monkeypatch):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    fatal = _FatalFlag(dev)

    async def boom(*a, **k):
        raise RuntimeError("something unexpected")

    monkeypatch.setattr(device_settings, "apply_setting", boom)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_refuse(30))
    import asyncio
    asyncio.run(plugin_cls._apply_setting(stub, 0x39, 1, dev.id, device_settings.PlannedWrite(ARL, 99999, 30)))
    assert dev.states["settingsError"] == "Auto-relock time: the write failed unexpectedly, see the event log"
    assert fatal.calls == [] and dev.errorState == ""
    assert not ds.setting_primed(dev.id, "autoRelockTime")          # we do not know what it holds
    assert mock_logger.exception.called                              # logged, loop not killed


def test_a_failing_failure_record_cannot_break_an_ordinary_report(ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds._failed_requests[(dev.id, "autoRelockTime")] = 45
    dev.setErrorStateOnServer("unreachable")
    ds._edit_settings_error = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lookup failed"))
    _live(ds, 45)                                                    # the very value that was asked for
    assert dev.states["autoRelockTime"] == 45                         # the report's own state landed
    assert dev.errorState == ""                                       # and the stale flag was cleared
    assert any("settings error" in str(c).lower() for c in mock_logger.warning.call_args_list)


def test_the_normal_report_path_does_no_failure_bookkeeping_when_nothing_failed(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    assert not ds._failed_requests
    worked = []
    ds._clear_satisfied_failures_unguarded = lambda *a, **k: worked.append(a)
    ds._clear_satisfied_failures(dev, [{"key": "autoRelockTime", "value": 3}])   # returns at once
    _live(ds, 90)                                                                  # and so does a report
    assert worked == []


def test_node_replace_drops_the_rebound_devices_failed_requests(ds, coffee):
    ds._failed_requests[(coffee.old_relay.id, "startUpOnOff")] = 1
    ds._failed_requests[(999, "startUpOnOff")] = 1                     # another device: untouched
    ds.replace_node(coffee.plan)
    assert (coffee.old_relay.id, "startUpOnOff") not in ds._failed_requests
    assert (999, "startUpOnOff") in ds._failed_requests
