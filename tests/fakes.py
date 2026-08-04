"""In-process fake matter-server WebSocket for tests.

Mocks matter-server at the WebSocket layer (IMPLEMENTATION §7), so the WS client,
HTTP handlers, and reconciliation can all be tested without a Node process.
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional

import protocol

_CLOSE = object()

#: Pass as ``handshake`` for a peer that opens the socket and then says nothing —
#: the "something else is listening on this port" case the hello timeout covers.
NO_HANDSHAKE = object()


class FakeWebSocket:
    """A controllable stand-in for a ``websockets`` connection.

    On connect it emits a ``server_info`` event (matching matter-server's
    announce-on-connect behaviour). Client commands are recorded in ``sent`` and
    answered by an optional ``responder`` callback. Tests can also ``push_event``
    server-initiated events.
    """

    def __init__(self, responder: Optional[Callable[[dict], list]] = None,
                 server_info: Optional[dict] = None,
                 handshake=None):
        self._outbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self._closed = False
        self._responder = responder
        # Both peers push a BARE object as their first frame (no event/message_id
        # wrapper): matter-server's server_info, the bridge node's hello (§2).
        # ``handshake`` seeds whichever one the test needs; matter-server's is the
        # default so the controller tests read unchanged. NO_HANDSHAKE seeds
        # nothing at all — the socket opens and then stays mute.
        if handshake is not NO_HANDSHAKE:
            self._outbox.put_nowait(json.dumps(
                handshake if handshake is not None
                else (server_info or {"sdk_version": "matter-server/0.6.2", "fabric_id": "0x0"})
            ))

    @property
    def closed(self) -> bool:
        """True once anyone has closed this socket — the client included."""
        return self._closed

    async def send(self, raw: str) -> None:
        if self._closed:
            raise ConnectionError("send on closed socket")
        frame = json.loads(raw)
        self.sent.append(frame)
        if self._responder is not None:
            for response in (self._responder(frame) or []):
                await self._outbox.put(json.dumps(response))

    async def recv(self) -> str:
        if self._closed and self._outbox.empty():
            raise ConnectionError("recv on closed socket")
        item = await self._outbox.get()
        if item is _CLOSE:
            raise ConnectionError("socket closed")
        return item

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        try:
            return await self.recv()
        except ConnectionError as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        self._closed = True
        await self._outbox.put(_CLOSE)

    async def push_event(self, event: str, data: dict) -> None:
        await self._outbox.put(json.dumps({protocol.KEY_EVENT: event, protocol.KEY_DATA: data}))

    async def push_raw(self, raw: str) -> None:
        """Deliver an arbitrary text frame — including one that is not valid JSON."""
        await self._outbox.put(raw)

    async def push_frame(self, frame) -> None:
        """Deliver an arbitrary JSON-encodable frame (a list, a bare string, …)."""
        await self._outbox.put(json.dumps(frame))

    def sent_commands(self) -> list[str]:
        return [f.get(protocol.KEY_COMMAND) for f in self.sent]


def scripted_responder(results: dict) -> Callable[[dict], list]:
    """Build a responder that answers each command with a canned result.

    ``results`` maps command name -> result payload (or a callable(frame)->payload).
    Unknown commands get a ``result: None`` reply so requests never hang.
    """
    def _respond(frame: dict) -> list:
        command = frame.get(protocol.KEY_COMMAND)
        mid = frame.get(protocol.KEY_MESSAGE_ID)
        if command in results:
            value = results[command]
            if callable(value):
                value = value(frame)
            return [{protocol.KEY_MESSAGE_ID: mid, protocol.KEY_RESULT: value}]
        return [{protocol.KEY_MESSAGE_ID: mid, protocol.KEY_RESULT: None}]
    return _respond


async def returns(value):
    """Await-able that yields ``value`` (for the client's connect factory)."""
    return value


# ---------------------------------------------------------------------------
# Fake Indigo devices for the export catalog / picker (PRD §5.1-§5.2)
# ---------------------------------------------------------------------------
# ``export_catalog`` dispatches on the IOM class-name chain, not isinstance
# (the indigo module is a MagicMock in tests — see conftest). So these classes
# are named exactly as Indigo's are and mirror its inheritance: DimmerDevice
# and SpeedControlDevice really are RelayDevice subclasses, which is precisely
# the ambiguity the catalog's most-specific-first ordering has to survive.
#
# Pessimistic discipline (docs/TESTING.md): each class declares only the
# capability flags its real counterpart carries, and ``minimal_device`` builds
# a device with none at all, so the catalog's getattr defaults are exercised
# rather than assumed.

#: pluginId for a device that is NOT ours — the loop guard must let it through.
OTHER_PLUGIN_ID = "com.example.someone-else"


class FakeIndigoDevice:
    """Base: the device attributes every Indigo device really has."""

    def __init__(self, dev_id=1, name="Device", plugin_id=OTHER_PLUGIN_ID, **attrs):
        self.id = dev_id
        self.name = name
        self.pluginId = plugin_id
        self.pluginProps = dict(attrs.pop("pluginProps", None) or {})
        self.deviceTypeId = attrs.pop("deviceTypeId", "")
        self.displayStateValUi = attrs.pop("displayStateValUi", "")
        self.model = attrs.pop("model", "")
        self.subModel = attrs.pop("subModel", "")
        for key, value in attrs.items():
            setattr(self, key, value)


class RelayDevice(FakeIndigoDevice):
    """indigo.RelayDevice — on/off only."""

    def __init__(self, *args, **kwargs):
        self.supportsOnState = kwargs.pop("supportsOnState", True)
        super().__init__(*args, **kwargs)


class DimmerDevice(RelayDevice):
    """indigo.DimmerDevice — a relay that also dims, and maybe colours."""

    def __init__(self, *args, **kwargs):
        self.supportsColor = kwargs.pop("supportsColor", False)
        self.supportsRGB = kwargs.pop("supportsRGB", False)
        self.supportsWhiteTemperature = kwargs.pop("supportsWhiteTemperature", False)
        super().__init__(*args, **kwargs)


class SpeedControlDevice(RelayDevice):
    """indigo.SpeedControlDevice — fans; excluded in v1."""


class SensorDevice(FakeIndigoDevice):
    """indigo.SensorDevice — binary (supportsOnState) or numeric (supportsSensorValue)."""

    def __init__(self, *args, **kwargs):
        self.supportsOnState = kwargs.pop("supportsOnState", False)
        self.supportsSensorValue = kwargs.pop("supportsSensorValue", False)
        super().__init__(*args, **kwargs)


class ThermostatDevice(FakeIndigoDevice):
    """indigo.ThermostatDevice."""


class SprinklerDevice(FakeIndigoDevice):
    """indigo.SprinklerDevice — no Matter irrigation type; excluded in v1."""


class MultiIODevice(FakeIndigoDevice):
    """indigo.MultiIODevice — no single-accessory representation; excluded."""


class CustomDevice(FakeIndigoDevice):
    """A plugin-defined `custom` device — no resolvable Matter role."""


def minimal_device(class_name, dev_id=1, name="Device", plugin_id=OTHER_PLUGIN_ID):
    """A device carrying ONLY id/name/pluginId, in a class of ``class_name``.

    Models the pessimistic case: an Indigo object whose capability flags we
    must not assume exist. Everything the catalog reads beyond those three
    attributes has to fall back cleanly.
    """
    klass = type(class_name, (object,), {})
    dev = klass()
    dev.id = dev_id
    dev.name = name
    dev.pluginId = plugin_id
    return dev


class FakeIndigoDevices:
    """Stands in for ``indigo.devices``: iterable, and subscriptable by id."""

    def __init__(self, devices=()):
        self._devices = list(devices)

    def add(self, device):
        self._devices.append(device)
        return device

    def __iter__(self):
        return iter(self._devices)

    def __len__(self):
        return len(self._devices)

    def __getitem__(self, device_id):
        for device in self._devices:
            if device.id == device_id:
                return device
        raise KeyError(device_id)
