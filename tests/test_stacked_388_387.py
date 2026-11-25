"""PR #388 stacked on PR #387: one coherent behaviour for "unread" and "null".

#387 records a null StartUpOnOff as the setting's declared null state (255, display
"previous state") and offers a "Previous state" row only while the state holds it.
#388 tracks whether a setting has been READ this run. A null IS a device reading, so:

* it marks the setting read, updates the cached reading, carries its uiValue;
* it is never flagged "unknown" or shown as "Not read from the device yet";
* picker rows: unread -> the unread row leads; read+null -> the Previous-state row
  leads; read+0/1/2 -> neither; never both;
* both rows mean "leave alone"; choosing Off/On/Toggle from either is written;
* a null read-back after a write is a real answer when the setting declares a null
  state, and "mark unread" when it does not;
* 255 is never written to the device and never swallowed by the int32 guard.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import device_settings
from matter_handlers.settings import settings_for_type

from test_device_sync import (  # noqa: F401  (fixtures and node fixtures)
    indigo_env, ds, plugin_cls, _LIGHTING_CASES, _null_variant, LOCK_SETTINGS_NODE,
)
from test_review_pass3 import _Matter, _run_apply, _seed_dialog  # noqa: F401
from test_unread_settings import _stub, _start, _open, _rows

NULL_ROW = ("255", "Previous state (device default; cannot be chosen from Indigo)")
UNREAD_ROW = (device_settings.UNREAD_OPTION_ID, device_settings.UNREAD_OPTION_LABEL)
SUS = {t: next(s for s in settings_for_type(t) if s.key == "startUpOnOff")
       for t in ("matterRelay", "matterDimmer", "matterColorDimmer")}


def _make(ds, devices, raw, type_id):
    ds.create_from_raw(raw, "Lamp")
    dev = devices[ds.lookup(raw["node_id"], 1, type_id)]
    dev.pluginProps["createdTypeId"] = type_id
    return dev


@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES)
def test_a_null_reporting_device_shows_the_previous_state_row_and_counts_as_read(
        plugin_cls, ds, indigo_env, mock_logger, raw, type_id):
    _i, devices = indigo_env
    null_raw = _null_variant(raw)
    dev = _make(ds, devices, null_raw, type_id)
    nid = null_raw["node_id"]
    assert dev.states["startUpOnOff"] == 255
    assert dev.states["startUpOnOff.ui"] == "previous state"
    assert ds.setting_primed(dev.id, "startUpOnOff")
    assert ds._setting_readings[nid][(1, 0x0006, 0x4003)] == 255
    stub = _stub(plugin_cls, ds, mock_logger)
    _start(plugin_cls, ds, mock_logger, dev)               # a restart must not flag it
    assert dev.states["startUpOnOff.ui"] == "previous state"
    values = _open(plugin_cls, stub, dev, type_id)
    assert values["hasStartUpOnOff"] == "yes" and values["startUpOnOff"] == "255"
    rows = _rows(plugin_cls, stub, dev, type_id, "startUpOnOff")
    assert rows[0] == NULL_ROW
    assert UNREAD_ROW not in rows


@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES)
def test_the_picker_rows_never_offer_both_special_rows(plugin_cls, ds, indigo_env, mock_logger, raw, type_id):
    _i, devices = indigo_env
    null_raw = _null_variant(raw)
    dev = _make(ds, devices, null_raw, type_id)
    stub = _stub(plugin_cls, ds, mock_logger)

    def special(rows):
        return [r for r in rows if r in (NULL_ROW, UNREAD_ROW)]

    assert special(_rows(plugin_cls, stub, dev, type_id, "startUpOnOff")) == [NULL_ROW]   # read + null
    ds.mark_setting_unread(dev.id, "startUpOnOff")
    assert special(_rows(plugin_cls, stub, dev, type_id, "startUpOnOff")) == [UNREAD_ROW]  # unread (state still 255)
    # a dialog seeded unread whose setting has since been read as null: still ONE row
    ds.apply_states(dev.id, [SUS[type_id].state_entry(None)])
    rows = plugin_cls.getSettingOptions(
        stub, filter="startUpOnOff", valuesDict={**dev.pluginProps, "startUpOnOff": "unread"},
        typeId=type_id, targetId=dev.id)
    assert len(special(rows)) == 1 and rows[0] == UNREAD_ROW
    ds.apply_states(dev.id, [{"key": "startUpOnOff", "value": 1}])
    assert special(_rows(plugin_cls, stub, dev, type_id, "startUpOnOff")) == []            # read + On


def test_the_picker_rows_for_the_real_option_ids_are_unchanged(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    raw, type_id = _LIGHTING_CASES[1]
    dev = _make(ds, devices, raw, type_id)
    rows = _rows(plugin_cls, _stub(plugin_cls, ds, mock_logger), dev, type_id, "startUpOnOff")
    assert [r[0] for r in rows] == ["0", "1", "2"]


@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES)
def test_an_untouched_save_of_a_null_holding_device_plans_nothing_even_if_it_moves(
        plugin_cls, ds, indigo_env, mock_logger, raw, type_id):
    _i, devices = indigo_env
    dev = _make(ds, devices, _null_variant(raw), type_id)
    runtime = MagicMock()
    stub = _stub(plugin_cls, ds, mock_logger, runtime=runtime, matter=MagicMock())
    stub._apply_setting = MagicMock(return_value="coro")
    values = plugin_cls.getDeviceConfigUiValues(stub, dict(dev.pluginProps), type_id, dev.id)[0]
    assert values["startUpOnOff"] == "255"
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, type_id, dev.id)
    runtime.submit.assert_not_called()


@pytest.mark.parametrize("seed_row", ["255", "unread"])
@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES)
def test_choosing_a_real_option_from_either_special_row_is_written(
        plugin_cls, ds, indigo_env, mock_logger, raw, type_id, seed_row):
    _i, devices = indigo_env
    dev = _make(ds, devices, _null_variant(raw), type_id)
    if seed_row == "unread":
        ds.mark_setting_unread(dev.id, "startUpOnOff")
    runtime = MagicMock()
    stub = _stub(plugin_cls, ds, mock_logger, runtime=runtime, matter=MagicMock())
    stub._apply_setting = MagicMock(return_value="coro")
    values = plugin_cls.getDeviceConfigUiValues(stub, dict(dev.pluginProps), type_id, dev.id)[0]
    assert values["startUpOnOff"] == seed_row
    values["startUpOnOff"] = "1"
    plugin_cls.closedDeviceConfigUi(stub, dict(values), False, type_id, dev.id)
    plan = stub._apply_setting.call_args.args[3]
    # from the unread row the previous value is UNKNOWN by design (#388)
    assert (plan.value, plan.previous) == (1, 255 if seed_row == "255" else None)


@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES)
def test_the_null_state_is_never_written_to_the_device(ds, indigo_env, raw, type_id):
    """Typing/choosing 255 is "leave alone" from either row; there is no write path."""
    _i, devices = indigo_env
    dev = _make(ds, devices, _null_variant(raw), type_id)
    nid = raw["node_id"]
    lists = lambda cluster: ds.attribute_list(nid, 1, cluster)      # noqa: E731
    for seed in ("unread", "255", ""):
        assert device_settings.planned_writes(
            type_id, {"startUpOnOff": "255"}, dict(dev.states), lambda c, a: None, lists,
            primed=lambda key: True, seeds={"startUpOnOff": seed}) == []
        assert device_settings.validate_settings(
            type_id, {"startUpOnOff": "255"}, lambda c, a: None, lists,
            states=dict(dev.states), primed=lambda key: True, seeds={"startUpOnOff": seed}) == {}


# --- a null read-back after a write ----------------------------------------

@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES)
def test_a_null_read_back_for_a_setting_with_a_null_state_is_a_real_answer(
        plugin_cls, ds, indigo_env, mock_logger, raw, type_id):
    _i, devices = indigo_env
    dev = _make(ds, devices, raw, type_id)                  # holds 1/2 -> read
    dev.setErrorStateOnServer = lambda v: None
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=None))
    dev.pluginProps.update({"nodeId": str(raw["node_id"]), "endpointId": "1"})
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(SUS[type_id], 0, 1), node=raw["node_id"])
    assert dev.states["startUpOnOff"] == 255
    assert dev.states["startUpOnOff.ui"] == "previous state"
    assert ds.setting_primed(dev.id, "startUpOnOff")


def test_a_null_read_back_for_a_setting_without_a_null_state_still_marks_it_unread(
        plugin_cls, ds, indigo_env, mock_logger):
    from test_unread_settings import _lock
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=None))
    arl = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(arl, 45, 30))
    assert not ds.setting_primed(dev.id, "autoRelockTime")


# --- 255 and the int32 guard -------------------------------------------------

def test_the_int32_guard_does_not_swallow_the_null_state(ds, indigo_env):
    _i, devices = indigo_env
    raw, type_id = _LIGHTING_CASES[0]
    dev = _make(ds, devices, raw, type_id)
    ds.apply_states(dev.id, [SUS[type_id].state_entry(None)])
    assert dev.states["startUpOnOff"] == 255 and ds.setting_primed(dev.id, "startUpOnOff")


# --- dimmers get the unread / flag / re-prime behaviour too -----------------

@pytest.mark.parametrize("raw,type_id", _LIGHTING_CASES[1:])
def test_a_dimmer_gets_the_unread_flag_and_the_re_prime(plugin_cls, ds, indigo_env, mock_logger, raw, type_id):
    _i, devices = indigo_env
    dev = _make(ds, devices, raw, type_id)
    ds._settings_primed.clear()
    dev.states["startUpOnOff"] = 0
    _start(plugin_cls, ds, mock_logger, dev)                # re-prime from the cache
    assert dev.states["startUpOnOff"] == raw["attributes"]["1/6/16387"]
    assert ds.setting_primed(dev.id, "startUpOnOff")
    ds.mark_setting_unread(dev.id, "startUpOnOff")
    assert dev.states["startUpOnOff.ui"] == "unknown"
