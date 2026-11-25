"""Issue #367: Toggle sends an explicit On/Off from the state Indigo SHOWS.

Matter's own ``Toggle`` flips whatever the device believes, which for a bulb
whose OnOff stuck at True after a power cut is the opposite of what the Indigo
screen promised. These tests drive the real action path — ``DeviceSync.build_command``
into ``MatterClient.send_command`` over the in-process fake matter-server
socket — and assert on the frame that actually goes over the wire.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
from types import SimpleNamespace

import pytest

import protocol
from protocol import Protocol
from matter_client import MatterClient

from fakes import FakeWebSocket, returns
from indigo_fakes import FakeDev, FakeDevices

ON_OFF_CLUSTER = 0x0006

# Every Indigo device type whose Toggle used to be (or is) a bare Matter Toggle.
TOGGLE_TYPES = ["matterRelay", "matterDimmer", "matterColorDimmer"]


@pytest.fixture
def env(mock_indigo_base, mock_logger):
    indigo = mock_indigo_base
    indigo.devices = FakeDevices()
    import device_sync
    importlib.reload(device_sync)
    from matter_handlers.registry import HandlerRegistry
    ds = device_sync.DeviceSync(HandlerRegistry(), mock_logger)
    return SimpleNamespace(indigo=indigo, ds=ds, logger=mock_logger)


def _dev(type_id, on_state="__absent__"):
    dev = FakeDev(1, "Lamp", type_id, {"nodeId": "7", "endpointId": "1"})
    if on_state != "__absent__":
        dev.onState = on_state
    return dev


def _toggle(env, dev, times=1):
    """Press Toggle ``times`` times with the device state left as it is, and
    return the command names that reached the fake matter-server."""
    async def scenario():
        sent = []

        def responder(frame):
            if frame.get(protocol.KEY_COMMAND) == protocol.CMD_DEVICE:
                sent.append((frame["args"][protocol.ARG_CLUSTER], frame["args"][protocol.ARG_COMMAND]))
            return [{protocol.KEY_MESSAGE_ID: frame["message_id"], protocol.KEY_RESULT: None}]

        fake = FakeWebSocket(responder=responder)
        matter = MatterClient(Protocol(), env.logger, {}, connect=lambda uri: returns(fake))
        task = asyncio.create_task(matter.run())
        await matter.wait_connected(timeout=2)
        action = SimpleNamespace(deviceAction=env.indigo.kDeviceAction.Toggle)
        for _ in range(times):
            command = env.ds.build_command(dev, action)
            assert command is not None
            await matter.send_command(command)
        await matter.close()
        task.cancel()
        return sent

    return asyncio.run(scenario())


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_toggle_when_shown_on_sends_off(env, type_id):
    assert _toggle(env, _dev(type_id, True)) == [(ON_OFF_CLUSTER, "Off")]


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_toggle_when_shown_off_sends_on(env, type_id):
    assert _toggle(env, _dev(type_id, False)) == [(ON_OFF_CLUSTER, "On")]


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_toggle_with_no_state_falls_back_to_matter_toggle(env, type_id):
    assert _toggle(env, _dev(type_id)) == [(ON_OFF_CLUSTER, "Toggle")]  # no onState attr at all
    assert _toggle(env, _dev(type_id, None)) == [(ON_OFF_CLUSTER, "Toggle")]


def test_dimmer_decides_from_on_off_not_brightness(env):
    # Shown on with brightness 0, and shown off with brightness 80: brightness is irrelevant.
    on_dark = _dev("matterDimmer", True)
    on_dark.brightness = 0
    assert _toggle(env, on_dark) == [(ON_OFF_CLUSTER, "Off")]
    off_bright = _dev("matterColorDimmer", False)
    off_bright.brightness = 80
    assert _toggle(env, off_bright) == [(ON_OFF_CLUSTER, "On")]


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_unreachable_device_still_toggles_from_its_shown_state(env, type_id):
    # Today an unreachable device's Toggle is still sent; that is unchanged.
    dev = _dev(type_id, True)
    dev.errorState = "unreachable"
    assert _toggle(env, dev) == [(ON_OFF_CLUSTER, "Off")]


# --- degradation paths -------------------------------------------------------
class _RaisingState:
    deviceTypeId = "matterRelay"
    pluginProps = {"nodeId": "7", "endpointId": "1"}

    @property
    def onState(self):  # noqa: N802
        raise RuntimeError("state read blew up")


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_state_read_raising_falls_back_to_toggle_not_a_wrong_command(env, type_id, caplog):
    dev = _RaisingState()
    dev.deviceTypeId = type_id
    with caplog.at_level(logging.DEBUG, logger="Plugin"):
        assert _toggle(env, dev) == [(ON_OFF_CLUSTER, "Toggle")]
    assert any("state read blew up" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", ["on", "false", 1, 0, 0.5, [], {}])
def test_unexpected_state_type_falls_back_to_toggle(env, bad):
    # Only a real bool is a usable on/off state; "false" must not read as truthy-on.
    assert _toggle(env, _dev("matterRelay", bad)) == [(ON_OFF_CLUSTER, "Toggle")]


def test_second_toggle_before_report_resends_same_command(env):
    # Nothing here updates the shown state after a send (no optimistic echo for
    # on/off), so a second press before the device's report arrives reads the
    # same old state and sends the same command again.
    assert _toggle(env, _dev("matterRelay", False), times=2) == [
        (ON_OFF_CLUSTER, "On"), (ON_OFF_CLUSTER, "On")]


def test_explicit_on_off_actions_unchanged(env):
    import device_sync  # noqa: F401
    handler_dev = _dev("matterRelay", True)
    for action, name in ((env.indigo.kDeviceAction.TurnOn, "On"), (env.indigo.kDeviceAction.TurnOff, "Off")):
        cmd = env.ds.build_command(handler_dev, SimpleNamespace(deviceAction=action))
        assert cmd.command == name
