"""Fourth fix pass on PR #388 (probes 388d): refusals in validation, the stale note by
cause, the INFO line only when true, seeds that cannot be misattributed, per-key write
isolation, and the tests the mutation run found missing."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import device_settings
import protocol
from matter_handlers.settings import settings_for_type
from protocol import MatterEvent

from test_device_sync import (  # noqa: F401
    indigo_env, ds, plugin_cls, LOCK_SETTINGS_NODE, LIGHTING_PLUG_NODE, OCCUPANCY_SENSITIVITY_NODE,
    _null_variant,
)
from test_review_pass3 import _Matter, _run_apply, _live
from test_unread_settings import _stub, _start, _open, _lock

ARL = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
SUS = next(s for s in settings_for_type("matterRelay") if s.key == "startUpOnOff")
CONNECTED = SimpleNamespace(connected=True)


def _vals(dev, **fields):
    return {**dev.pluginProps, **fields}


def _validate(plugin_cls, stub, dev, values, type_id="matterLock"):
    return plugin_cls.validateDeviceConfigUi(stub, values, type_id, dev.id)


def _close(plugin_cls, stub, dev, values, type_id="matterLock"):
    stub.runtime = MagicMock()
    plugin_cls.closedDeviceConfigUi(stub, values, False, type_id, dev.id)
    for call in stub.runtime.submit.call_args_list:
        call.args[0].close()                    # never leave a coroutine un-awaited
    return stub.runtime


# --- 1. unsendable edits are refused in VALIDATION -----------------------------

def test_an_edit_while_not_connected_is_refused_in_validation(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=SimpleNamespace(connected=False))
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "60"
    result = _validate(plugin_cls, stub, dev, values)
    assert result[0] is False
    assert "Not connected to matter-server" in result[2]["autoRelockTime"]
    assert "Clear the field" in result[2]["autoRelockTime"]


def test_a_client_without_a_connected_attribute_counts_as_not_connected(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=SimpleNamespace())
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "60"
    assert _validate(plugin_cls, stub, dev, values)[0] is False


def test_an_edit_on_an_unreachable_device_is_refused_in_validation(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    dev.setErrorStateOnServer("unreachable")
    values["autoRelockTime"] = "60"
    result = _validate(plugin_cls, stub, dev, values)
    assert result[0] is False and "unreachable" in result[2]["autoRelockTime"]


def test_a_dialog_with_no_setting_edit_validates_as_before_even_when_disconnected(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=SimpleNamespace(connected=False))
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert _validate(plugin_cls, stub, dev, values)[0] is True


def test_clearing_the_field_lets_the_rest_save_while_disconnected(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=SimpleNamespace(connected=False))
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = ""
    assert _validate(plugin_cls, stub, dev, values)[0] is True


def test_the_close_backstop_names_the_device_and_the_setting(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "60"
    stub.matter = SimpleNamespace(connected=False)             # dropped between Validate and Close
    runtime = _close(plugin_cls, stub, dev, values)
    runtime.submit.assert_not_called()
    message = " ".join(str(a) for a in mock_logger.error.call_args.args)
    assert dev.name in message and ARL.label in message and "nothing was written" in message


def test_a_failed_write_never_replaces_an_existing_unreachable_flag(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    dev.setErrorStateOnServer("unreachable")
    stub = _stub(plugin_cls, ds, mock_logger,
                 matter=_Matter(write=TimeoutError("timed out"), read=TimeoutError("timed out")))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 45, 30))
    assert dev.errorState == "unreachable"
    assert dev.states["settingsError"].startswith("Auto-relock time:")     # recorded on the setting
    assert _open(plugin_cls, stub, dev, "matterLock")["settingsStale"] != "no"


# --- 2. the stale note --------------------------------------------------------

def test_stale_is_only_set_when_settings_are_shown_and_says_why(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    assert _open(plugin_cls, stub, dev, "matterLock")["settingsStale"] == "no"
    dev.setErrorStateOnServer("unreachable")
    assert _open(plugin_cls, stub, dev, "matterLock")["settingsStale"] == "device"
    stub.matter = SimpleNamespace(connected=False)
    assert _open(plugin_cls, stub, dev, "matterLock")["settingsStale"] == "server"
    ds._attribute_lists.clear()                               # nothing offered
    assert _open(plugin_cls, stub, dev, "matterLock")["hasAnySetting"] == "no"
    assert _open(plugin_cls, stub, dev, "matterLock")["settingsStale"] == "no"


def test_every_dialog_has_a_note_per_cause():
    import xml.etree.ElementTree as ET
    from pathlib import Path
    root = ET.parse(Path(__file__).resolve().parent.parent / "indigo-matter.indigoPlugin" / "Contents"
                    / "Server Plugin" / "Devices.xml").getroot()
    for device in root.findall("Device"):
        fields = {f.get("id"): f for f in device.find("ConfigUI").findall("Field")} \
            if device.find("ConfigUI") is not None else {}
        if "settingsHeading" not in fields:
            continue
        by_value = {f.get("visibleBindingValue"): f for f in fields.values()
                    if f.get("visibleBindingId") == "settingsStale"}
        assert set(by_value) == {"device", "server"}, device.get("id")
        assert "not connected to matter-server" in by_value["server"].find("Label").text.lower()
        assert "not reachable" in by_value["device"].find("Label").text.lower()


def test_validation_strips_the_dialog_only_stale_marker(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert "settingsStale" in values
    result = _validate(plugin_cls, stub, dev, values)
    assert "settingsStale" not in result[1]


def test_the_unreachable_check_reads_a_gone_device_as_fine_and_any_other_failure_as_stale(
        plugin_cls, mock_indigo_base, mock_logger):
    import plugin as plugin_module
    mock_indigo_base.devices = {}
    assert plugin_module._is_flagged_unreachable(5, mock_logger) is False       # KeyError: gone

    class _Broken:
        def __getitem__(self, _k):
            raise RuntimeError("Indigo is unhappy")
    mock_indigo_base.devices = _Broken()
    assert plugin_module._is_flagged_unreachable(5, mock_logger) is True
    assert mock_logger.warning.called


# --- 3. the "once connected" INFO line is only true sometimes -----------------

def test_connected_and_reconciled_logs_the_per_setting_reason_every_time(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    raw = copy.deepcopy(LOCK_SETTINGS_NODE)
    del raw["attributes"]["1/257/65531"]              # reconciled, AttributeList never reported
    ds.create_from_raw(raw, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    _open(plugin_cls, stub, dev, "matterLock")
    _open(plugin_cls, stub, dev, "matterLock")
    lines = [str(c.args) for c in mock_logger.info.call_args_list]
    assert not any("once the plugin has connected" in line for line in lines)
    assert sum("has not reported an AttributeList" in line for line in lines) >= 6    # 3 settings x 2 opens


def test_not_connected_logs_the_once_line_once(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds._attribute_lists.clear()
    stub = _stub(plugin_cls, ds, mock_logger, matter=SimpleNamespace(connected=False))
    _open(plugin_cls, stub, dev, "matterLock")
    _open(plugin_cls, stub, dev, "matterLock")
    lines = [str(c.args) for c in mock_logger.info.call_args_list]
    assert sum("once the plugin has connected" in line for line in lines) == 1


# --- 4. seeds ---------------------------------------------------------------

def test_a_dialog_build_failure_blanks_everything_and_an_untouched_save_writes_nothing(
        plugin_cls, ds, indigo_env, mock_logger, monkeypatch):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    dev.pluginProps.update({"autoRelockTime": "60", "hasAutoRelockTime": "yes"})   # an earlier Save
    original = device_settings.config_ui_values
    monkeypatch.setattr(device_settings, "config_ui_values",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert values["autoRelockTime"] == "" and values["hasAutoRelockTime"] == "no"
    assert values["hasAnySetting"] == "no"
    monkeypatch.setattr(device_settings, "config_ui_values", original)
    runtime = _close(plugin_cls, stub, dev, dict(values))
    runtime.submit.assert_not_called()
    # and even a SAVED prop riding in with no remembered seed is not written
    runtime = _close(plugin_cls, stub, dev, _vals(dev, autoRelockTime="60"))
    runtime.submit.assert_not_called()
    assert any("starting values" in str(c) for c in mock_logger.warning.call_args_list)


def test_two_dialogs_on_one_device_make_the_seed_ambiguous_and_neither_save_writes(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    first = _open(plugin_cls, stub, dev, "matterLock")          # seeds 30
    _live(ds, 45)
    second = _open(plugin_cls, stub, dev, "matterLock")         # seeds 45
    assert (first["autoRelockTime"], second["autoRelockTime"]) == ("30", "45")
    assert _close(plugin_cls, stub, dev, dict(first)).submit.call_count == 0
    assert _close(plugin_cls, stub, dev, dict(second)).submit.call_count == 0


def test_one_devices_seed_never_decides_another_devices_save(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    a = _lock(ds, devices)                                      # node 0x39, holds 30
    other = copy.deepcopy(LOCK_SETTINGS_NODE)
    other["node_id"] = 0x3A
    other["attributes"]["1/257/35"] = 60
    ds.create_from_raw(other, "Back Gate")
    b = devices[ds.lookup(0x3A, 1)]
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    stub._apply_setting = MagicMock(return_value="coro")
    _open(plugin_cls, stub, a, "matterLock")                    # A seeded 30
    seed_b = _open(plugin_cls, stub, b, "matterLock")           # B seeded 60
    assert seed_b["autoRelockTime"] == "60"
    values = dict(seed_b)
    values["autoRelockTime"] = "30"                              # the user sets B to 30
    stub.runtime = MagicMock()
    plugin_cls.closedDeviceConfigUi(stub, values, False, "matterLock", b.id)
    assert stub.runtime.submit.call_count == 1
    plan = stub._apply_setting.call_args.args[3]
    assert (plan.value, plan.previous) == (30, 60)


def test_a_numeric_seed_compares_numerically(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "030"                             # == seed "30"
    assert _validate(plugin_cls, stub, dev, values)[0] is True
    assert _close(plugin_cls, stub, dev, values).submit.call_count == 0


# --- 5. per-key no-clear writes ------------------------------------------------

def test_one_failing_key_does_not_abort_the_rest_and_is_not_marked_read(ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds._settings_primed.clear()
    real = dev.updateStateOnServer

    def flaky(key, *a, **kw):
        if key == "wrongCodeEntryLimit":
            raise RuntimeError("boom")
        return real(key, *a, **kw)

    dev.updateStateOnServer = flaky
    ds.apply_states(dev.id, [{"key": "autoRelockTime", "value": 31},
                             {"key": "wrongCodeEntryLimit", "value": 4},
                             {"key": "userCodeTemporaryDisableTime", "value": 61}], clear_error=False)
    assert dev.states["autoRelockTime"] == 31 and dev.states["userCodeTemporaryDisableTime"] == 61
    assert ds.setting_primed(dev.id, "autoRelockTime")
    assert not ds.setting_primed(dev.id, "wrongCodeEntryLimit")
    assert "wrongCodeEntryLimit" in str(ds.logger.warning.call_args)


def test_node_added_cannot_let_a_reachability_failure_escape_the_event_handler(ds, indigo_env):
    ds._apply_reachability = MagicMock(side_effect=RuntimeError("boom"))
    ds.handle_event(MatterEvent(kind=protocol.EVT_NODE_ADDED, node_id=0x39,
                                raw={"data": copy.deepcopy(LOCK_SETTINGS_NODE)}))      # must not raise
    assert ds.logger.warning.called


# --- 6. wording ---------------------------------------------------------------

def test_a_null_read_back_for_a_setting_with_a_null_state_says_previous_state():
    class _Client:
        async def write(self, write, timeout=None):
            return None

        async def read(self, *a, **k):
            return None
    ok, message = asyncio.run(device_settings.apply_setting(
        _Client(), 6, 1, device_settings.PlannedWrite(SUS, 1, 0),
        sleep=lambda _d: asyncio.sleep(0)))
    assert ok is False
    assert "the device still reports Previous state" in message or "still reports previous state" in message.lower()
    assert "no value (null)" not in message


# --- 7. tests the mutation run found missing -----------------------------------

def test_a_reading_that_lands_inside_the_flag_call_is_not_overwritten(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds._settings_primed.discard((dev.id, "autoRelockTime"))
    real_apply = ds.apply_states

    def racing(dev_id, kv, **kw):
        # the flag has decided to write "unknown"; a real reading lands right now
        if kw.get("count_as_read") is False and not racing.done:
            racing.done = True
            real_apply(dev_id, [{"key": "autoRelockTime", "value": 77}])
        return real_apply(dev_id, kv, **kw)

    racing.done = False
    ds.apply_states = racing
    ds._flag_unknown(dev, ["autoRelockTime"])
    assert dev.states["autoRelockTime"] == 77
    assert dev.states["autoRelockTime.ui"] == "77 seconds"


def test_a_recovered_node_with_nothing_new_to_prime_is_still_unflagged(ds, indigo_env):
    from test_device_sync import _with_node_evidence
    from test_handlers import RELAY_NODE
    from matter_model import parse_node
    _i, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    result = ds.create_from_raw(evidenced, "Office Plug")
    relay = devices[ds.lookup(42, 1, "matterRelay")]
    node_dev = devices[result["nodeDeviceId"]]
    ds._apply_reachability(parse_node({**evidenced, "available": False}, "Office Plug"))
    assert relay.errorState == "unreachable" and node_dev.errorState == "unreachable"
    ds._prime_states = lambda *a, **k: None                  # nothing new to prime
    ds._apply_reachability(parse_node({**evidenced, "available": True}, "Office Plug"))
    assert relay.errorState == "" and node_dev.errorState == ""


def test_deviceStartComm_sees_states_the_refresh_adds_on_a_pre_refresh_snapshot(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    fresh = devices[ds.lookup(0x39, 1)]
    fresh.pluginProps["createdTypeId"] = "matterLock"
    for key in ("autoRelockTime", "wrongCodeEntryLimit", "userCodeTemporaryDisableTime"):
        del fresh.states[key]
    fresh.declared_on_refresh = ("autoRelockTime", "wrongCodeEntryLimit", "userCodeTemporaryDisableTime")
    ds._settings_primed.clear()
    ds._setting_readings.clear()                               # nothing to re-prime from
    snapshot = SimpleNamespace(id=fresh.id, name=fresh.name, deviceTypeId="matterLock",
                               pluginProps=fresh.pluginProps, states=dict(fresh.states),
                               stateListOrDisplayStateIdChanged=fresh.stateListOrDisplayStateIdChanged)
    assert "autoRelockTime" not in snapshot.states
    stub = SimpleNamespace(logger=mock_logger, device_sync=ds, _prime_retired_on_time_state=lambda d: None)
    plugin_cls.deviceStartComm(stub, snapshot)
    assert fresh.states["autoRelockTime.ui"] == "unknown"      # judged on the FRESH device


def test_the_flag_write_uses_the_documented_call_with_recorded_keywords(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds.mark_setting_unread(dev.id, "autoRelockTime")
    call = dev.single_key_calls[-1]
    assert (call["key"], call["uiValue"], call["clearErrorState"], call["triggerEvents"]) == (
        "autoRelockTime", "unknown", False, False)


def test_a_null_read_back_for_a_null_state_setting_is_recorded_on_the_setting_not_the_device_flag(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    raw = LIGHTING_PLUG_NODE
    ds.create_from_raw(raw, "Lamp")
    dev = devices[ds.lookup(raw["node_id"], 1, "matterRelay")]
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=None))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(SUS, 0, 1), node=raw["node_id"])
    assert dev.errorState == ""
    assert dev.states["startUpOnOff"] == 255


def test_an_ambiguous_slot_refuses_even_a_real_edit_and_says_to_reopen(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    first = _open(plugin_cls, stub, dev, "matterLock")
    _open(plugin_cls, stub, dev, "matterLock")                 # a second dialog on the same device
    first["autoRelockTime"] = "60"
    result = _validate(plugin_cls, stub, dev, first)           # refused where the user can see it
    assert result[0] is False and "another window" in result[2]["autoRelockTime"]
    assert _close(plugin_cls, stub, dev, first).submit.call_count == 0
    assert any("starting values" in str(c) for c in mock_logger.warning.call_args_list)
