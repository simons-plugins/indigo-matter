"""Every log line for a setting write names the Indigo device, in the plugin's own style
(``"Name": message``); success messages use the option label, not a raw number."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import device_settings
from matter_handlers.settings import settings_for_type

from test_device_sync import (  # noqa: F401
    indigo_env, ds, plugin_cls, LOCK_SETTINGS_NODE, LIGHTING_PLUG_NODE, OCCUPANCY_SENSITIVITY_NODE,
)
from test_review_pass3 import _Matter, _run_apply
from test_unread_settings import _stub, _open, _lock, _sensor

ARL = next(s for s in settings_for_type("matterLock") if s.key == "autoRelockTime")
SENS = next(s for s in settings_for_type("matterMotionSensor") if s.key == "sensitivityLevel")
SUS = next(s for s in settings_for_type("matterRelay") if s.key == "startUpOnOff")
CONNECTED = SimpleNamespace(connected=True)


def _lines(method):
    out = []
    for call in method.call_args_list:
        args = call.args
        out.append(args[0] % args[1:] if len(args) > 1 else str(args[0]))
    return out


def test_the_success_line_names_the_device_and_uses_the_option_label(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _sensor(ds, devices)
    dev.pluginProps.update({"nodeId": "45", "endpointId": "1"})
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=2))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(SENS, 2, 1), node=0x2D)
    assert f'"{dev.name}": Sensitivity is now High (most sensitive).' in _lines(mock_logger.info)


def test_the_power_on_success_line_names_the_device(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    ds.create_from_raw(LIGHTING_PLUG_NODE, "Lamp")
    dev = devices[ds.lookup(LIGHTING_PLUG_NODE["node_id"], 1, "matterRelay")]
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=0))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(SUS, 0, 1), node=LIGHTING_PLUG_NODE["node_id"])
    assert f'"{dev.name}": After a power cut is now Off.' in _lines(mock_logger.info)


def test_the_option_label_comes_from_the_settings_own_options_not_just_enum_values():
    """The cause: value_text only knew EnumValues, so a setting whose labels come from its
    `options` callable (sensitivity) fell through to the raw number."""
    from matter_handlers.settings import Bounds
    assert SENS.value_text(2, Bounds(0, 2)) == "High (most sensitive)"
    assert SENS.value_text(0, Bounds(0, 2)) == "Low (least sensitive)"
    assert SENS.value_text(2) == "2"                     # no bounds known: the number, as before
    assert ARL.value_text(30) == "30 seconds"


def test_the_failure_lines_name_the_device_but_settingsError_does_not(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger,
                 matter=_Matter(write=RuntimeError("Write failed: CONSTRAINT_ERROR (0x87)"), read=30))
    _run_apply(plugin_cls, stub, dev, device_settings.PlannedWrite(ARL, 99999, 30))
    assert all(line.startswith(f'"{dev.name}": ') for line in _lines(mock_logger.error))
    assert dev.name not in dev.states["settingsError"]


def test_the_crash_path_names_the_device(plugin_cls, ds, indigo_env, mock_logger, monkeypatch):
    _i, devices = indigo_env
    dev = _lock(ds, devices)

    async def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(device_settings, "apply_setting", boom)
    import asyncio
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter())
    asyncio.run(plugin_cls._apply_setting(stub, 0x39, 1, dev.id, device_settings.PlannedWrite(ARL, 5, 30)))
    call = mock_logger.exception.call_args
    assert dev.name in (call.args[0] % call.args[1:])


def test_a_failing_name_lookup_still_logs_with_the_device_id(plugin_cls, ds, indigo_env, mock_logger, mock_indigo_base):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=_Matter(read=45))
    import plugin as plugin_module
    real = plugin_module._device_log_name

    class _Broken:
        def __getitem__(self, _k):
            raise RuntimeError("Indigo unhappy")
    mock_indigo_base.devices = _Broken()
    assert plugin_module._device_log_name(dev.id) == f"device {dev.id}"
    mock_indigo_base.devices = devices
    assert plugin_module._device_log_name(dev.id) == f'"{dev.name}"'


def test_the_close_time_lines_name_the_device(plugin_cls, ds, indigo_env, mock_logger):
    _i, devices = indigo_env
    dev = _lock(ds, devices)
    stub = _stub(plugin_cls, ds, mock_logger, matter=CONNECTED)
    stub.runtime = MagicMock()
    plugin_cls.closedDeviceConfigUi(                  # no seed: the "reopen the dialog" warning
        stub, {**dev.pluginProps, "autoRelockTime": "99"}, False, "matterLock", dev.id)
    warning = _lines(mock_logger.warning)[-1]
    assert dev.name in warning and "starting values" in warning
