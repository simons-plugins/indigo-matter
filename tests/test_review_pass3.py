"""Third fix pass on PR #388 - converted from the reviewers' probes (388c).

Each test asserts the behaviour the PR promises:
* an unanswered/null read-back is not re-vouched for by the next deviceStartComm;
* a write that must not clear the error uses the documented single-key call;
* an untouched Save never overwrites another app's change (seed comparison);
* node replace drops primed entries; an int32-overflowing reading un-reads;
* an unavailable node's snapshot never reverts a confirmed write;
* start-up re-checks 'read?' against a FRESH device just before it writes.
"""
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

from test_device_sync import (  # noqa: F401  (fixtures and node fixtures)
    indigo_env, ds, plugin_cls, LOCK_SETTINGS_NODE, OCCUPANCY_SENSITIVITY_NODE,
    LIGHTING_PLUG_NODE,
)
from test_plugin_module import _lock_dev, _lock_stub, _LOCK_PROPS, _seeded_call  # noqa: F401
from test_unread_settings import _stub, _start, _open, _rows, _lock, _sensor

ARL = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
WCL = next(s for s in settings_for_type("matterLock") if s.key == "wrongCodeEntryLimit")


def _run_apply(plugin_cls, stub, dev, plan, node=0x39):
    original = device_settings.apply_setting

    async def quick(*a, **k):
        async def no_sleep(_d):
            return None
        return await original(*a, sleep=no_sleep, **k)

    device_settings.apply_setting = quick
    try:
        asyncio.run(plugin_cls._apply_setting(stub, node, 1, dev.id, plan))
    finally:
        device_settings.apply_setting = original


class _Matter:
    def __init__(self, write=None, read=None):
        self._write, self._read = write, read
        self.writes = []

    async def write(self, write, timeout=None):
        self.writes.append(write)
        if isinstance(self._write, Exception):
            raise self._write

    async def read(self, *a, **k):
        if isinstance(self._read, Exception):
            raise self._read
        return self._read


def _live(ds, value, attribute=0x23, node=0x39, cluster=0x0101):
    ds.handle_event(MatterEvent(kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=node, endpoint=1,
                                cluster=cluster, attribute=attribute, value=value))


# --- 1. an unanswered / null read-back is not re-vouched for ---------------

def test_D_a_later_device_start_does_not_re_vouch_for_an_unanswered_write(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=TimeoutError("no answer")))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 45, 30))
    assert not ds.setting_primed(dev.id, "autoRelockTime")
    assert (1, 0x0101, 0x0023) not in ds._setting_readings[0x39]       # the cache forgot it
    _start(plugin_cls, ds, mock_logger, dev)
    assert not ds.setting_primed(dev.id, "autoRelockTime")
    assert dev.states["autoRelockTime.ui"] == "unknown"
    assert _open(plugin_cls, stub, dev, "matterLock")["autoRelockTime"] == ""


def test_a_null_read_back_is_not_re_vouched_for_either(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=None))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 45, 30))
    _start(plugin_cls, ds, mock_logger, dev)
    assert not ds.setting_primed(dev.id, "autoRelockTime")


# --- 2. the error state ----------------------------------------------------

def test_A_a_refused_write_leaves_the_device_flagged(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger,
                 matter=_Matter(write=RuntimeError("Write failed: CONSTRAINT_ERROR (0x87)"), read=5))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(WCL, 3, 5))
    assert dev.errorState == ""
    assert dev.states["wrongCodeEntryLimit"] == 5


def test_a_refused_write_says_so_instead_of_blaming_the_firmware(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger,
                 matter=_Matter(write=RuntimeError("Write failed: CONSTRAINT_ERROR (0x87)"), read=5))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(WCL, 3, 5))
    message = mock_logger.error.call_args[0][2]
    assert "the device refused the value" in message
    assert "CONSTRAINT_ERROR" in message
    assert "acknowledges a write and ignores it" not in message


def test_a_silent_ack_is_still_described_as_a_firmware_that_ignores_writes():
    client = _Matter(read=5)
    ok, message = asyncio.run(device_settings.apply_setting(
        client, 57, 1, device_settings.PlannedWrite(WCL, 3, 5), sleep=lambda _d: asyncio.sleep(0)))
    assert ok is False and "acknowledges a write and ignores it" in message
    assert "refused" not in message


def test_a_write_that_must_not_clear_the_error_uses_the_documented_single_key_call(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(OCCUPANCY_SENSITIVITY_NODE, "FP300")
    dev = devices[ds.lookup(0x2D, 1)]
    dev.setErrorStateOnServer("unreachable")
    dev.updateStatesOnServer = lambda kv: pytest.fail("the batch call cannot opt out of clearing")
    before = len(dev.single_key_calls)
    ds.apply_states(dev.id, [{"key": "sensitivityLevel", "value": 2, "uiValue": "High"}],
                    clear_error=False)
    assert dev.single_key_calls[before:] == [{
        "key": "sensitivityLevel", "value": 2, "clearErrorState": False, "uiValue": "High",
        "decimalPlaces": None, "triggerEvents": True}]
    assert dev.errorState == "unreachable"


def test_the_unknown_flag_is_display_only_and_does_not_fire_triggers(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds.mark_setting_unread(dev.id, "autoRelockTime")
    call = dev.single_key_calls[-1]
    assert call["triggerEvents"] is False and call["clearErrorState"] is False


def test_a_node_device_stays_flagged_when_its_reachable_state_is_written(ds, indigo_env):
    """Pre-existing: _write_node_reachable used the batch call after marking the
    device unreachable; a state write clears the error unless told not to."""
    from test_device_sync import _with_node_evidence
    from test_handlers import RELAY_NODE
    from matter_model import parse_node
    _i, devices = indigo_env
    evidenced = _with_node_evidence(RELAY_NODE)
    node_dev_id = ds.create_from_raw(evidenced, "Office Plug")["nodeDeviceId"]
    ds._apply_reachability(parse_node({**evidenced, "available": False}, "Office Plug"))
    assert devices[node_dev_id].errorState == "unreachable"
    assert devices[node_dev_id].states["reachable"] is False


# --- 3. an untouched Save never overwrites another app's change ------------

def _seed_dialog(plugin_cls, stub, dev):
    return plugin_cls.getDeviceConfigUiValues(stub, dict(dev.pluginProps), "matterLock", dev.id)[0]


def _lock_dialog_stub(plugin_cls, ds, mock_logger):
    runtime = MagicMock()
    stub = _stub(plugin_cls, ds, mock_logger, runtime=runtime, matter=MagicMock())
    stub._apply_setting = MagicMock(return_value="coro")
    return stub, runtime


def test_D_a_seed_gone_stale_while_the_dialog_is_open_is_not_an_edit(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, runtime = _lock_dialog_stub(plugin_cls, ds, mock_logger)
    values = _seed_dialog(plugin_cls, stub, dev)                      # seeded 30
    _live(ds, 120)                                                    # another app changes it
    assert dev.states["autoRelockTime"] == 120
    assert plugin_cls.validateDeviceConfigUi(stub, dict(values), "matterLock", dev.id)[0] is True
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    runtime.submit.assert_not_called()


def test_the_user_typing_a_value_after_a_stale_seed_is_written(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, runtime = _lock_dialog_stub(plugin_cls, ds, mock_logger)
    values = _seed_dialog(plugin_cls, stub, dev)
    _live(ds, 120)
    values["autoRelockTime"] = "60"
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    assert runtime.submit.call_count == 1
    plan = stub._apply_setting.call_args.args[3]
    assert (plan.setting.key, plan.value, plan.previous) == ("autoRelockTime", 60, 120)


def test_the_remembered_seed_is_spent_on_close_and_on_cancel(plugin_cls, ds, indigo_env, mock_logger):
    """Behavioural: after a Save or a Cancel the seed is gone, so a later Save with no
    new open plans nothing (and says why) rather than reusing it."""
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, runtime = _lock_dialog_stub(plugin_cls, ds, mock_logger)
    values = _seed_dialog(plugin_cls, stub, dev)
    plugin_cls.closedDeviceConfigUi(stub, dict(values), True, "matterLock", dev.id)       # cancel
    values["autoRelockTime"] = "90"
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)      # no seed now
    runtime.submit.assert_not_called()
    assert any("starting values" in str(c) for c in mock_logger.warning.call_args_list)
    values = _seed_dialog(plugin_cls, stub, dev)
    values["autoRelockTime"] = "90"
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)      # Save
    assert runtime.submit.call_count == 1
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)      # spent
    assert runtime.submit.call_count == 1


def test_without_a_remembered_seed_nothing_is_planned_even_for_a_changed_value(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, runtime = _lock_dialog_stub(plugin_cls, ds, mock_logger)
    values = {**_LOCK_PROPS, "autoRelockTime": "99", "wrongCodeEntryLimit": "5",
              "userCodeTemporaryDisableTime": "60"}                   # plugin restarted mid-dialog
    plugin_cls.closedDeviceConfigUi(stub, values, False, "matterLock", dev.id)
    runtime.submit.assert_not_called()
    assert any("starting values" in str(c) for c in mock_logger.warning.call_args_list)


# --- 5. an int32-overflowing reading un-reads an already-read setting ------

def test_E_an_oversized_reading_after_a_read_makes_the_setting_unread(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    _live(ds, 2**32 - 1)
    stub = _stub(plugin_cls, ds, mock_logger)
    assert not ds.setting_primed(dev.id, "autoRelockTime")
    assert dev.states["autoRelockTime.ui"] == "unknown"
    assert _open(plugin_cls, stub, dev, "matterLock")["autoRelockTime"] == ""
    assert any("not read" in str(c) or "unread" in str(c) for c in mock_logger.info.call_args_list)


def test_a_large_non_setting_integer_state_is_still_written(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    dev.states["someCounter"] = 0
    ds.apply_states(dev.id, [{"key": "someCounter", "value": 2**32 - 1}])
    assert dev.states["someCounter"] == 2**32 - 1


# --- 6. an unavailable node never reverts a read setting; stale heading ----

def test_L_an_unavailable_snapshot_does_not_revert_a_confirmed_write(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=45))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 45, 30))
    ds.reconcile_all([dict(LOCK_SETTINGS_NODE, available=False)])      # snapshot still says 30
    assert dev.states["autoRelockTime"] == 45


# --- 7. start-up re-checks against a FRESH device --------------------------

def test_the_unknown_flag_uses_the_fresh_device_so_newly_added_states_are_seen(ds, indigo_env):
    """deviceStartComm's `dev` was fetched BEFORE the state-list refresh; the states
    the upgrade added exist only on a fresh fetch."""
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    fresh = devices[ds.lookup(0x39, 1)]
    ds._settings_primed.clear()
    stale = SimpleNamespace(id=fresh.id, deviceTypeId="matterLock", pluginProps=fresh.pluginProps,
                            states={"onOffState": True, "lockState": "locked"}, name="Side Gate")
    ds.flag_unread_settings(stale)
    assert fresh.states["autoRelockTime.ui"] == "unknown"


def test_a_flag_issued_after_a_reading_does_not_undo_it(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds._settings_primed.discard((dev.id, "autoRelockTime"))
    ds._flag_unknown(dev, ["autoRelockTime"])             # "unknown" written...
    ds.apply_states(dev.id, [{"key": "autoRelockTime", "value": 77}])   # ...then the reading lands
    assert dev.states["autoRelockTime.ui"] == "77 seconds"
    ds._flag_unknown(dev, ["autoRelockTime"])             # a late flag must not undo it
    assert dev.states["autoRelockTime.ui"] == "77 seconds"


def test_a_re_prime_never_writes_a_setting_read_in_the_meantime(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds._settings_primed.clear()
    real = ds._prime_states

    def racing(node, dev_id, ep, type_id, **kw):
        ds.apply_states(dev_id, [{"key": "autoRelockTime", "value": 77}])   # a live update wins
        return real(node, dev_id, ep, type_id, **kw)

    ds._prime_states = racing
    ds.reprime_settings(dev)
    assert dev.states["autoRelockTime"] == 77


# --- 8. the picker row follows the dialog's own value ----------------------

def test_the_unread_row_is_listed_when_the_dialog_was_seeded_with_it_even_if_read_since(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Plug")
    dev = devices[ds.lookup(0x34, 1)]
    stub = _stub(plugin_cls, ds, mock_logger)
    rows = plugin_cls.getSettingOptions(
        stub, filter="startUpOnOff", valuesDict={**dev.pluginProps, "startUpOnOff": "unread"},
        typeId="matterRelay", targetId=dev.id)           # the setting IS read now
    assert rows[0][0] == "unread"
    rows = plugin_cls.getSettingOptions(
        stub, filter="startUpOnOff", valuesDict={**dev.pluginProps, "startUpOnOff": "1"},
        typeId="matterRelay", targetId=dev.id)
    assert all(row_id != "unread" for row_id, _l in rows)


# --- 9. not connected / nothing cached yet ---------------------------------

def test_before_any_reconcile_one_line_explains_instead_of_blaming_the_device(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds._attribute_lists.clear()
    stub = _stub(plugin_cls, ds, mock_logger)
    _open(plugin_cls, stub, dev, "matterLock")
    lines = [str(c.args) for c in mock_logger.info.call_args_list]
    assert not any("has not reported an AttributeList" in line for line in lines)
    assert sum("once the plugin has connected" in line for line in lines) == 1


# --- 10. gaps the mutation run found ---------------------------------------

def test_A_closing_with_an_unread_lock_submits_a_write_for_zero(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {9: _lock_dev({"autoRelockTime": 0, "wrongCodeEntryLimit": 0,
                                              "userCodeTemporaryDisableTime": 0})}
    runtime = MagicMock()
    stub = _lock_stub(plugin_cls, mock_logger, runtime=runtime, matter=MagicMock())
    stub.device_sync.setting_primed.side_effect = lambda dev_id, key: False
    _seeded_call("closedDeviceConfigUi", plugin_cls,
        stub, {**_LOCK_PROPS, "autoRelockTime": "0", "wrongCodeEntryLimit": "",
               "userCodeTemporaryDisableTime": ""}, False, "matterLock", 9)
    assert runtime.submit.call_count == 1
    for call in runtime.submit.call_args_list:
        call.args[0].close()


def test_B_validating_an_unread_lock_still_refuses_an_out_of_range_zero(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {9: _lock_dev({"autoRelockTime": 0, "wrongCodeEntryLimit": 0,
                                              "userCodeTemporaryDisableTime": 0})}
    stub = _lock_stub(plugin_cls, mock_logger)
    stub.device_sync.setting_primed.side_effect = lambda dev_id, key: False
    result = _seeded_call("validateDeviceConfigUi", plugin_cls,
        stub, {**_LOCK_PROPS, "autoRelockTime": "", "wrongCodeEntryLimit": "0",
               "userCodeTemporaryDisableTime": ""}, "matterLock", 9)
    assert result[0] is False and "wrongCodeEntryLimit" in result[2]


def test_C_a_failing_primed_lookup_is_treated_as_unread(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {9: _lock_dev()}
    stub = _lock_stub(plugin_cls, mock_logger)
    stub.device_sync.setting_primed.side_effect = RuntimeError("lock poisoned")
    values, _ = plugin_cls.getDeviceConfigUiValues(stub, dict(_LOCK_PROPS), "matterLock", 9)
    assert values["autoRelockTime"] == ""


def test_F_the_hint_follows_management_switched_off_after_start(mock_logger):
    from protocol import ProtocolError
    from commission_jobs import CommissionJobs
    from test_commission_jobs import (FakeMatter, _good_create, _await_terminal, _DISCOVERY_DETAILS,
                                      _RESTART_MENU_ITEM)
    state = {"managed": True}

    async def scenario():
        class FailingMatter(FakeMatter):
            async def commission_with_code(self, code, **_kwargs):
                state["managed"] = False
                raise ProtocolError(1, _DISCOVERY_DETAILS)

        jobs = CommissionJobs(FailingMatter(), _good_create, mock_logger,
                              schedule=asyncio.ensure_future, managed=lambda: state["managed"])
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Side Gate"})
        return await _await_terminal(jobs, body["jobId"])()
    assert _RESTART_MENU_ITEM not in asyncio.run(scenario())["error"]["message"]


def test_G_the_plugins_real_wiring_gives_the_remote_hint_when_it_does_not_run_matter_server(
        plugin_cls, mock_logger):
    """Behavioural: the plugin's own builder of its CommissionJobs, with and without
    a managed matter-server (server_process None = LaunchAgent management off)."""
    from protocol import ProtocolError
    from test_commission_jobs import FakeMatter, _await_terminal, _DISCOVERY_DETAILS, _RESTART_MENU_ITEM

    def hint_for(server_process):
        async def scenario():
            class FailingMatter(FakeMatter):
                async def commission_with_code(self, code, **_kwargs):
                    raise ProtocolError(1, _DISCOVERY_DETAILS)

            plugin = SimpleNamespace(
                matter=FailingMatter(), logger=mock_logger, server_process=server_process,
                device_sync=SimpleNamespace(create_from_raw=None, knows_node=lambda n: False),
                runtime=SimpleNamespace(submit=asyncio.ensure_future))
            plugin._server_is_managed = lambda: plugin_cls._server_is_managed(plugin)
            jobs = plugin_cls._build_commission_jobs(plugin)
            _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Side Gate"})
            return (await _await_terminal(jobs, body["jobId"])())["error"]["message"]
        return asyncio.run(scenario())

    assert _RESTART_MENU_ITEM not in hint_for(None)
    assert "on the machine that runs it" in hint_for(None)
    assert _RESTART_MENU_ITEM in hint_for(object())


def test_re_priming_with_nothing_cached_writes_no_state_at_all(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds._setting_readings.clear()
    ds._settings_primed.clear()
    dev.updateStatesOnServer = lambda kv: pytest.fail("state write")
    dev.updateStateOnServer = lambda *a, **k: pytest.fail("state write")
    ds.reprime_settings(dev)


def test_a_live_update_keeps_the_cached_reading_current(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    _live(ds, 90)
    assert ds._setting_readings[0x39][(1, 0x0101, 0x0023)] == 90


