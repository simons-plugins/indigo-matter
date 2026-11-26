"""Silent-failure review findings A-F on PR #388."""
from __future__ import annotations

import copy
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import device_settings
from matter_handlers.settings import settings_for_type

from test_device_sync import (  # noqa: F401
    indigo_env, ds, plugin_cls, LOCK_SETTINGS_NODE,
)
from test_review_pass3 import _Matter, _run_apply, _live
from test_unread_settings import _stub, _open, _lock

ARL = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
WCL = next(s for s in settings_for_type("matterLock") if s.key == "wrongCodeEntryLimit")
CONNECTED = SimpleNamespace(connected=True)


def _text(call):
    args = call.args
    return args[0] % args[1:] if len(args) > 1 else str(args[0])


# --- A: nothing in _apply_setting may vanish -------------------------------------------

def test_a_success_whose_state_write_raises_is_an_error_naming_device_and_setting_and_still_clears_the_failure(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(write=RuntimeError("CONSTRAINT"), read=30))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 99999, 30))     # an old failure
    assert dev.states["settingsError"]
    real = ds.apply_states

    def flaky(dev_id, kv, **kw):
        if any(item.get("key") == "autoRelockTime" for item in kv):
            raise RuntimeError("Indigo refused the state")
        return real(dev_id, kv, **kw)

    ds.apply_states = flaky
    stub.matter = _Matter(read=40)
    mock_logger.error.reset_mock()
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 40, 30))
    errors = [_text(c) for c in mock_logger.error.call_args_list]
    assert any(dev.name in e and "Auto-relock time" in e and "took the value" in e for e in errors)
    assert dev.states["settingsError"] == ""                  # the device DID take it


def _closing_stub(plugin_cls, ds, mock_logger, dev):
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "60"
    fut = Future()
    stub.runtime = SimpleNamespace(submit=lambda coro: (coro.close(), fut)[1])
    return stub, values, fut


def test_an_exception_raised_outside_every_guard_is_logged_by_the_done_callback(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, values, fut = _closing_stub(plugin_cls, ds, mock_logger, dev)
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    fut.set_exception(RuntimeError("an unguarded line blew up"))
    errors = [_text(c) for c in mock_logger.error.call_args_list]
    assert any(dev.name in e and "Auto-relock time" in e and "unguarded line" in e for e in errors)


# --- B: a stray KeyError must not skip recording the failure ---------------------------

def test_a_keyerror_on_the_failure_path_still_records_settingsError(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    real = ds.apply_states

    def bug(dev_id, kv, **kw):
        if any(item.get("key") == "autoRelockTime" for item in kv):
            raise KeyError("a bug, not a deleted device")
        return real(dev_id, kv, **kw)

    ds.apply_states = bug
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(write=RuntimeError("CONSTRAINT"), read=30))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 99999, 30))
    assert dev.states["settingsError"].startswith("Auto-relock time:")


# --- C: ambiguous / missing seeds are visible in the dialog ------------------------------

def test_validation_refuses_a_candidate_edit_while_the_device_has_two_dialogs_open(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    first = _open(plugin_cls, stub, dev, "matterLock")
    _open(plugin_cls, stub, dev, "matterLock")
    first["autoRelockTime"] = "60"
    result = plugin_cls.validateDeviceConfigUi(stub, dict(first), "matterLock", dev.id)
    assert result[0] is False
    assert "another window" in result[2]["autoRelockTime"]


def test_an_untouched_ambiguous_dialog_still_validates(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    first = _open(plugin_cls, stub, dev, "matterLock")
    _open(plugin_cls, stub, dev, "matterLock")
    assert plugin_cls.validateDeviceConfigUi(stub, dict(first), "matterLock", dev.id)[0] is True


def test_an_untouched_save_after_a_restart_logs_no_warning(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    vars(stub).pop("_dialog_seeds")                           # the plugin restarted mid-dialog
    stub.runtime = MagicMock()
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    assert not mock_logger.warning.called


def test_a_changed_field_after_a_restart_still_warns(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    vars(stub).pop("_dialog_seeds")
    values["autoRelockTime"] = "99"
    stub.runtime = MagicMock()
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    assert any("starting values" in _text(c) for c in mock_logger.warning.call_args_list)


# --- D: a failure text with no way out ---------------------------------------------------

def test_replacing_a_node_clears_the_failure_text_of_the_rebound_devices(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    ds.record_setting_failure(dev.id, ARL, 99999, "the device rejected 99999 seconds")
    assert dev.states["settingsError"]
    ds._forget_settings_read(dev)
    assert dev.states["settingsError"] == ""


def test_opening_the_dialog_drops_sentences_for_settings_the_device_no_longer_offers(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds.record_setting_failure(dev.id, ARL, 99999, "the device rejected 99999 seconds")
    ds.record_setting_failure(dev.id, WCL, 200, "the device rejected 200 attempts")
    # firmware update: AutoRelockTime disappears from the AttributeList
    for key, value in list(ds._attribute_lists.items()):
        if key[2] == 0x0101:
            ds._attribute_lists[key] = [a for a in value if a != 0x23]
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert "Auto-relock time" not in values["settingsErrorText"]
    assert "Wrong-code attempt limit" in values["settingsErrorText"]


def test_an_unknown_attribute_list_does_not_drop_a_failure_text(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    ds.record_setting_failure(dev.id, ARL, 99999, "the device rejected 99999 seconds")
    ds._attribute_lists.clear()                                # None is not False (ADR-0003)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    assert "Auto-relock time" in _open(plugin_cls, stub, dev, "matterLock")["settingsErrorText"]


# --- E: the read-edit-write is serialised ------------------------------------------------

def test_two_threads_editing_settingsError_cannot_lose_each_others_sentence(ds, indigo_env):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    inside, release = threading.Event(), threading.Event()
    real = ds.apply_states

    def slow(dev_id, kv, **kw):
        if any(item.get("key") == "settingsError" for item in kv) and not slow.done:
            slow.done = True
            inside.set()
            release.wait(5)                                   # thread 1 is mid read-modify-write
        return real(dev_id, kv, **kw)

    slow.done = False
    ds.apply_states = slow
    t1 = threading.Thread(target=lambda: ds.record_setting_failure(dev.id, ARL, 1, "cause one"))
    t2 = threading.Thread(target=lambda: ds.record_setting_failure(dev.id, WCL, 2, "cause two"))
    t1.start()
    assert inside.wait(5)
    t2.start()
    release.set()
    t1.join(5)
    t2.join(5)
    text = dev.states["settingsError"]
    assert "Auto-relock time: cause one" in text and "Wrong-code attempt limit: cause two" in text


# --- F: small ones -----------------------------------------------------------------------

def test_a_device_without_the_state_warns_once_per_device(ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LOCK_SETTINGS_NODE, "Side Gate")
    dev = devices[ds.lookup(0x39, 1)]
    del dev.states["settingsError"]
    ds.record_setting_failure(dev.id, ARL, 1, "cause")
    ds.record_setting_failure(dev.id, WCL, 2, "cause")
    warnings = [_text(c) for c in mock_logger.warning.call_args_list if "settingsError" in _text(c)]
    assert len(warnings) == 1 and dev.name in warnings[0]


def test_a_bounds_lookup_failure_leaves_a_debug_trace_with_the_traceback(
        plugin_cls, ds, indigo_env, mock_logger):
    from test_unread_settings import _sensor
    _i, devices = indigo_env
    dev = _sensor(ds, devices)                                # sensitivity: FromAttribute bounds
    sens = next(s for s in settings_for_type("matterMotionSensor") if s.key == "sensitivityLevel")
    ds.setting_limits = MagicMock(side_effect=RuntimeError("cache poisoned"))
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=2))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(sens, 2, 1), node=0x2D)
    assert any(c.kwargs.get("exc_info") for c in mock_logger.debug.call_args_list)


def test_could_not_tell_reachability_has_its_own_wording(plugin_cls, ds, indigo_env, mock_logger, mock_indigo_base):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    values = _open(plugin_cls, stub, dev, "matterLock")
    values["autoRelockTime"] = "60"

    class _Proxy:
        """The real device, except that reading its error state fails."""
        def __getattr__(self, name):
            if name == "errorState":
                raise RuntimeError("Indigo unhappy")
            return getattr(dev, name)

    class _Devices:
        def __getitem__(self, key):
            return _Proxy()
    mock_indigo_base.devices = _Devices()
    result = plugin_cls.validateDeviceConfigUi(stub, dict(values), "matterLock", dev.id)
    assert result[0] is False
    message = result[2]["autoRelockTime"]
    assert "could not tell" in message.lower() and "is unreachable" not in message


def test_the_remaining_log_lines_name_the_device_not_the_bare_id(plugin_cls, ds, indigo_env, mock_logger):
    from test_seed_expiry import _Clock
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    clock = _Clock()
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED, _seed_clock=clock)
    _open(plugin_cls, stub, dev, "matterLock")
    clock.now += 700
    _open(plugin_cls, stub, dev, "matterLock")
    assert any(dev.name in _text(c) for c in mock_logger.info.call_args_list if "unclosed" in _text(c))
    # a refused state write names the device
    real = dev.updateStateOnServer
    dev.updateStateOnServer = MagicMock(side_effect=RuntimeError("no"))
    ds.apply_states(dev.id, [{"key": "autoRelockTime", "value": 3}], clear_error=False)
    assert any(dev.name in _text(c) for c in mock_logger.warning.call_args_list if "could not write state" in _text(c))
    dev.updateStateOnServer = real
    # the skip lines
    ds._attribute_lists.clear()
    stub2 = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    ds._setting_readings[0x39] = {}                          # reconciled, AttributeList never reported
    mock_logger.info.reset_mock()
    _open(plugin_cls, stub2, dev, "matterLock")
    assert any(dev.name in _text(c) for c in mock_logger.info.call_args_list if "not offered" in _text(c))


# --- final pass: items 1, 3, 4 ----------------------------------------------------------

def test_a_failure_recorded_while_a_clear_is_in_flight_keeps_its_remembered_request(ds, indigo_env):
    import time
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    key = (int(dev.id), ARL.key)
    ds.record_setting_failure(dev.id, ARL, 30, "old cause")
    real = ds._edit_settings_error_locked
    fired = {"done": False}
    holder = {}

    def wrapped(dev_id, setting, sentence):
        ok = real(dev_id, setting, sentence)
        if sentence is None and not fired["done"]:
            fired["done"] = True
            t = threading.Thread(target=ds.record_setting_failure, args=(dev.id, ARL, 99, "new cause"))
            holder["t"] = t
            t.start()
            deadline = time.time() + 0.5
            while ds._failed_requests.get(key) != 99 and time.time() < deadline:
                time.sleep(0.01)
        return ok

    ds._edit_settings_error_locked = wrapped
    ds.clear_setting_failure(dev.id, ARL)
    holder["t"].join(2)
    assert "new cause" in devices[dev.id].states["settingsError"]
    assert ds._failed_requests.get(key) == 99


def test_a_clean_future_logs_nothing(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, values, fut = _closing_stub(plugin_cls, ds, mock_logger, dev)
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    fut.set_result(None)
    assert not mock_logger.error.called and not mock_logger.warning.called


def test_a_write_cancelled_by_shutdown_warns_naming_device_and_setting(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub, values, fut = _closing_stub(plugin_cls, ds, mock_logger, dev)
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, "matterLock", dev.id)
    assert fut.cancel()
    warnings = [_text(c) for c in mock_logger.warning.call_args_list]
    assert any(dev.name in w and "Auto-relock time" in w and "interrupted" in w for w in warnings)
    assert not mock_logger.error.called


def test_an_unexpected_error_pruning_failures_warns_and_the_dialog_still_opens(
        plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)

    real = stub._setting_attribute_list_lookup
    calls = []

    def bug(props):
        calls.append(1)
        if len(calls) == 1:                                    # the prune is the first caller
            raise TypeError("a programming error")
        return real(props)

    stub._setting_attribute_list_lookup = bug
    mock_logger.warning.reset_mock()
    values = _open(plugin_cls, stub, dev, "matterLock")
    assert values["autoRelockTime"] == "30"                    # the dialog opened and seeded
    warnings = [_text(c) for c in mock_logger.warning.call_args_list]
    assert any(dev.name in w and "prune" in w for w in warnings)
