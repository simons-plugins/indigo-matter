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
from test_dimmer_color import COLOR_NODE, DIMMER_NODE
from test_handlers import RELAY_NODE
from indigo_fakes import FakeDev, FakeDeviceFactory, FakeDevices

ON_OFF_CLUSTER = 0x0006

# Every Indigo device type whose Toggle used to be (or is) a bare Matter Toggle.
TOGGLE_TYPES = ["matterRelay", "matterDimmer", "matterColorDimmer"]


@pytest.fixture(autouse=True)
def _fresh_warning_memory():
    from matter_handlers import base
    base.TOGGLE_FALLBACK_WARNED.clear()
    yield
    base.TOGGLE_FALLBACK_WARNED.clear()


@pytest.fixture
def env(mock_indigo_base, mock_logger):
    indigo = mock_indigo_base
    indigo.devices = FakeDevices()
    indigo.device = FakeDeviceFactory(indigo.devices)
    indigo.kProtocol = SimpleNamespace(Plugin="plugin")
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
def test_toggle_with_missing_state_falls_back_to_matter_toggle(env, type_id):
    assert _toggle(env, _dev(type_id)) == [(ON_OFF_CLUSTER, "Toggle")]  # no onOffState key at all


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_toggle_with_none_state_falls_back_to_matter_toggle(env, type_id):
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


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_second_toggle_before_report_resends_same_command(env, type_id):
    # Nothing here updates the shown state after a send (no optimistic echo for
    # on/off), so a second press before the device's report arrives reads the
    # same old state and sends the same command again.
    assert _toggle(env, _dev(type_id, False), times=2) == [
        (ON_OFF_CLUSTER, "On"), (ON_OFF_CLUSTER, "On")]


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_explicit_on_off_actions_unchanged(env, type_id):
    for action, name in ((env.indigo.kDeviceAction.TurnOn, "On"), (env.indigo.kDeviceAction.TurnOff, "Off")):
        cmd = env.ds.build_command(_dev(type_id, True), SimpleNamespace(deviceAction=action))
        assert cmd.command == name


# --- the actual #367 sequence: a device report arrives, THEN Toggle -----------
NODES = {"matterRelay": RELAY_NODE, "matterDimmer": DIMMER_NODE, "matterColorDimmer": COLOR_NODE}


def _created(env, type_id):
    node = NODES[type_id]
    env.ds.create_from_raw(node, "Lamp")
    return env.indigo.devices[env.ds.lookup(node["node_id"], 1)]


def _report(env, type_id, value):
    env.ds.handle_event(protocol.MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=NODES[type_id]["node_id"], endpoint=1,
        cluster=ON_OFF_CLUSTER, attribute=0x0000, value=value))


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_report_says_on_then_toggle_sends_off(env, type_id):
    dev = _created(env, type_id)
    assert dev.deviceTypeId == type_id
    _report(env, type_id, True)
    assert _toggle(env, dev) == [(ON_OFF_CLUSTER, "Off")]


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_report_says_off_then_toggle_sends_on(env, type_id):
    dev = _created(env, type_id)
    _report(env, type_id, True)
    _report(env, type_id, False)
    assert _toggle(env, dev) == [(ON_OFF_CLUSTER, "On")]


@pytest.mark.parametrize("type_id", TOGGLE_TYPES)
def test_freshly_created_never_reported_toggle_sends_on(env, type_id):
    dev = _created(env, type_id)
    assert dev.states["onOffState"] is False
    assert _toggle(env, dev) == [(ON_OFF_CLUSTER, "On")]


# --- visibility: the fallback warns once per device; the normal path never ----
def _toggle_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "Toggle" in r.getMessage()]


class _PropertyAttributeError:
    deviceTypeId = "matterRelay"
    pluginProps = {"nodeId": "7", "endpointId": "1"}
    id = 5
    name = "Broken"

    @property
    def onState(self):  # noqa: N802
        raise AttributeError("inside the property")


@pytest.mark.parametrize("make", [
    lambda: _dev("matterRelay"),                  # missing
    lambda: _dev("matterRelay", None),            # None
    lambda: _dev("matterRelay", "on"),            # non-bool
    lambda: _RaisingState(),                      # raises
    lambda: _PropertyAttributeError(),            # AttributeError inside the property
], ids=["missing", "none", "non-bool", "raises", "property-attribute-error"])
def test_every_fallback_warns(env, caplog, make):
    dev = make()
    with caplog.at_level(logging.WARNING, logger="Plugin"):
        assert _toggle(env, dev) == [(ON_OFF_CLUSTER, "Toggle")]
    (record,) = _toggle_warnings(caplog)
    message = record.getMessage()
    assert "Matter Toggle" in message and "device's own state" in message


def test_fallback_warning_names_device_and_value(env, caplog):
    dev = _dev("matterRelay", "on")
    with caplog.at_level(logging.WARNING, logger="Plugin"):
        _toggle(env, dev)
    message = _toggle_warnings(caplog)[0].getMessage()
    assert "Lamp" in message and "'on'" in message


def test_fallback_warning_once_per_device(env, caplog):
    first, other = _dev("matterRelay", None), _dev("matterRelay", None)
    other.id = 2
    with caplog.at_level(logging.WARNING, logger="Plugin"):
        _toggle(env, first, times=2)
        assert len(_toggle_warnings(caplog)) == 1      # second press, same device: quiet
        _toggle(env, other)
        assert len(_toggle_warnings(caplog)) == 2      # a different device warns


def test_fallback_warning_cannot_raise_when_device_id_unreadable(env, caplog):
    class NoId(_RaisingState):
        @property
        def id(self):
            raise RuntimeError("no id")
    with caplog.at_level(logging.WARNING, logger="Plugin"):
        assert _toggle(env, NoId()) == [(ON_OFF_CLUSTER, "Toggle")]
    assert len(_toggle_warnings(caplog)) == 1


@pytest.mark.parametrize("shown", [True, False])
def test_explicit_path_never_warns_and_logs_resolution_at_debug(env, caplog, shown):
    with caplog.at_level(logging.DEBUG, logger="Plugin"):
        _toggle(env, _dev("matterRelay", shown))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    expected = f'Toggle "Lamp": shown {"on" if shown else "off"}, sending {"Off" if shown else "On"}'
    assert expected in [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
