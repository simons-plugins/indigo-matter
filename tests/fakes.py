"""In-process fake matter-server WebSocket for tests.

Mocks matter-server at the WebSocket layer (IMPLEMENTATION §7), so the WS client,
HTTP handlers, and reconciliation can all be tested without a Node process.
"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import Future
from typing import Any, Callable, Optional

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


#: A real ``open_commissioning_window`` result (issue #210), snake_case —
#: VERIFIED against matter-server v1.2.2's WebSocketControllerHandler.ts (see
#: protocol.py's module header). Plays the role
#: tests/fixtures/bridge_protocol/frames.json's shapes play for the OUTBOUND
#: bridge contract: a golden fixture any test wanting a realistic result reaches
#: for, rather than hand-rolling one.
COMMISSIONING_WINDOW_RESULT = {
    "setup_pin_code": 12345678,
    "setup_manual_code": "34970112332",
    "setup_qr_code": "MT:-24J0AFN00KA0648G00",
}


def error_responder(command: str, code: Any, details: str = "") -> Callable[[dict], list]:
    """Build a responder that answers ONE command with a matter-server error
    frame (``{message_id, error_code, details}``) and everything else with
    ``result: None`` — ``scripted_responder``'s error-shaped counterpart, for
    tests that need to prove a ``ProtocolError`` reaches the caller."""
    def _respond(frame: dict) -> list:
        mid = frame.get(protocol.KEY_MESSAGE_ID)
        if frame.get(protocol.KEY_COMMAND) == command:
            return [{protocol.KEY_MESSAGE_ID: mid, protocol.KEY_ERROR_CODE: code,
                    protocol.KEY_ERROR_DETAILS: details}]
        return [{protocol.KEY_MESSAGE_ID: mid, protocol.KEY_RESULT: None}]
    return _respond


async def returns(value):
    """Await-able that yields ``value`` (for the client's connect factory)."""
    return value


# ---------------------------------------------------------------------------
# Export (outbound) doubles — the E3 bridge wiring
# ---------------------------------------------------------------------------
class RecordingRuntime:
    """Stands in for ``AsyncRuntime``, running each coroutine to completion NOW.

    ``ExportBridge`` deliberately never awaits what it submits (§3.4), so a
    runtime that merely queued coroutines would let every assertion pass against
    work that never happened. Running them synchronously on the calling thread
    keeps the tests deterministic *and* exercises ``_fire``'s done-callback, which
    is the only thing that stops a failed state push being silent.
    """

    def __init__(self, running: bool = True):
        self.is_running = running
        self.submitted: list = []

    def submit(self, coro):
        self.submitted.append(coro)
        if not self.is_running:
            raise RuntimeError("asyncio runtime is not running")
        future: Future = Future()
        try:
            future.set_result(_run_to_completion(coro))
        except Exception as exc:  # the client's own errors reach the callback
            future.set_exception(exc)
        return future


def _run_to_completion(coro):
    """Run ``coro`` now, whether or not we are already inside a loop.

    ``asyncio.run`` refuses to nest, and since E5 the export engine genuinely
    nests: a §5 command is dispatched onto the loop, its blocking half runs in
    an executor, and a failure there fires a corrective ``set_state`` back at
    the runtime. In the plugin those are two different threads and
    ``run_coroutine_threadsafe`` handles it as a matter of course. Here they are
    one thread, so the inner coroutine is given a loop of its own on a worker we
    join — which keeps the tests synchronous and deterministic without pretending
    the nesting does not happen.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    outcome: dict = {}

    def _worker():
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # pylint: disable=broad-except
            outcome["error"] = exc

    thread = threading.Thread(target=_worker, name="fake-runtime-nested")
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class InlineExecutor:
    """A ``concurrent.futures.Executor`` that runs work on the calling thread.

    ``ExportBridge`` dispatches §5 commands through ``run_in_executor`` (E5), and
    a real thread pool would make every command assertion a race. This satisfies
    the slice ``run_in_executor`` uses — ``submit`` returning a future — while
    keeping the tests deterministic, exactly as :class:`RecordingRuntime` does
    for the loop.
    """

    def __init__(self, hang: bool = False):
        self.shutdown_calls: list = []
        #: How many pieces of work were handed over — the observable that says
        #: the loop was left, and that a command nobody exports never even
        #: reached the hop.
        self.submitted = 0
        #: Stand in for a wedged ``indigo.*`` call: the work is accepted and the
        #: future never resolves, which is exactly what a Z-Wave device that has
        #: stopped answering looks like from the executor's side. Deterministic
        #: — no sleeping — so the timeout test does not race a clock.
        self.hang = hang

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        future: Future = Future()
        if self.hang:
            return future
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # pylint: disable=broad-except
            future.set_exception(exc)
        return future

    def shutdown(self, wait: bool = True, cancel_futures: bool = False):
        self.shutdown_calls.append((wait, cancel_futures))


class FakeBridgeClient:
    """Records what ``ExportBridge`` sends, without a socket or a node.

    Mirrors the slice of :class:`bridge_client.BridgeClient` the export engine
    actually touches. ``calls`` is ordered, because frame order is part of the
    contract (§1: frames are applied in receipt order).
    """

    def __init__(self, logger=None, prefs=None, **kwargs):
        self.logger = logger
        self.prefs = dict(prefs or {})
        self.kwargs = kwargs
        self.calls: list[tuple] = []
        self.attached = True
        self.connected = True
        self.halted = False
        self.halted_reason = None
        self.recovery = False
        self.closed = False
        self.ran = False
        #: The ``timeout`` of each attach, in order. Kept OUT of ``calls`` so the
        #: existing tuple assertions stay readable — but recorded, because the
        #: deadline an un-export is given is the whole of X1: the empty endpoint
        #: list says nothing about how many endpoints the node has to remove.
        self.attach_timeouts: list = []
        #: command name → exception to raise instead of succeeding.
        self.fail: dict = {}
        #: What ``get_status`` answers. The §4.3 warnings channel has exactly
        #: one reader — the watchdog's poll — so a fake without this cannot
        #: exercise the only path a node's persistence failure has to a user.
        self.status = None
        #: What ``get_pairing`` answers (§3.7). Read on every attach since the
        #: §5.5 window readout stopped trusting its own cache.
        self.pairing = None

    # -- the recorded surface -------------------------------------------
    async def run(self):
        self.ran = True

    async def attach(self, endpoints=None, *, replace_all=False, timeout=None):
        self.attach_timeouts.append(timeout)
        self._record("attach", endpoints, replace_all)
        # Mirrors the real client: `attach()` returns whatever `self.status`
        # was just set to (issue #246's `reattach` reads this return value
        # directly). Tests that only assert on `calls`/`attach_timeouts` are
        # unaffected — `self.status` defaults to `None`, same as before.
        return self.status

    async def upsert_endpoint(self, spec, timeout=None):
        return self._record("upsert_endpoint", spec)

    async def remove_endpoint(self, device_id, timeout=None):
        return self._record("remove_endpoint", device_id)

    async def set_state(self, device_id, states):
        return self._record("set_state", device_id, dict(states))

    async def set_reachable(self, device_id, reachable, timeout=None):
        return self._record("set_reachable", device_id, reachable)

    async def get_status(self, timeout=None):
        self._record("get_status")
        return self.status

    async def get_pairing(self, timeout=None):
        self._record("get_pairing")
        if self.pairing is None:
            raise ConnectionError("no pairing report configured")
        return self.pairing

    async def close(self):
        self.closed = True
        self._record("close")

    def retry_now(self):
        self._record("retry_now")
        return True  # mirrors a real WsJsonClient accepting the poke

    # -- introspection ---------------------------------------------------
    def _record(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]
        return None

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def only(self, name: str) -> tuple:
        matches = [call for call in self.calls if call[0] == name]
        assert len(matches) == 1, f"expected exactly one {name}, got {self.names()}"
        return matches[0]


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
        # Every real Indigo device carries both, and §4.1 `reachable` is derived
        # from them (XAC8) — so they are base attributes, not per-class extras.
        self.enabled = attrs.pop("enabled", True)
        self.configured = attrs.pop("configured", True)
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
        self.onState = kwargs.pop("onState", False)
        super().__init__(*args, **kwargs)


class DimmerDevice(RelayDevice):
    """indigo.DimmerDevice — a relay that also dims, and maybe colours.

    The colour attributes default to ``None``, which is what real Indigo reports
    on a device with no colour channel — the export handlers have to omit those
    §4.2 keys rather than push a fabricated 0.
    """

    def __init__(self, *args, **kwargs):
        self.supportsColor = kwargs.pop("supportsColor", False)
        self.supportsRGB = kwargs.pop("supportsRGB", False)
        self.supportsWhiteTemperature = kwargs.pop("supportsWhiteTemperature", False)
        self.brightness = kwargs.pop("brightness", 0)
        self.redLevel = kwargs.pop("redLevel", None)
        self.greenLevel = kwargs.pop("greenLevel", None)
        self.blueLevel = kwargs.pop("blueLevel", None)
        self.whiteLevel = kwargs.pop("whiteLevel", None)
        self.whiteTemperature = kwargs.pop("whiteTemperature", None)
        # z2m (and any driver that publishes one) reports its colour mode here
        # — real Indigo always has a `.states` dict, empty by default, so a
        # device with no opinion on colour mode looks exactly like one that
        # never mentioned it (issue #282's fallback heuristic path).
        self.states = kwargs.pop("states", None) or {}
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
    """A plugin-defined `custom` device — `indigo.Device`, no IOM subclass.

    The measured shape (issue #252): Texecom's `<Device type="custom">` alarm
    zones instantiate as a plain `indigo.Device`, so `supportsOnState` is False
    and the real reading sits in a plugin-named state. `states` therefore
    matters here in a way it does not for the typed doubles, and defaults to
    empty so the existing "nothing to publish" rows keep meaning that.
    """

    def __init__(self, *args, **kwargs):
        self.states = dict(kwargs.pop("states", None) or {})
        super().__init__(*args, **kwargs)


def texecom_zone(dev_id=743389524, name="Study PIR", status=False, **kwargs):
    """A Texecom alarm zone, as measured on the reference database 2026-08-18.

    The `statusText.*` booleans are Indigo's own enum expansion of the
    `statusText` state, and they are here on purpose: they are what makes the
    naive "list every boolean state" mapping picker offer five candidates where
    only `status` is right.
    """
    states = {
        "lastTripped": "24-09-25 17:33:09",
        "status": status,
        "statusText": "Healthy",
        "statusText.Healthy": not status,
        "statusText.Shorted": False,
        "statusText.Tamper": False,
        "statusText.Tripped": status,
    }
    return CustomDevice(dev_id, name, states=states,
                        plugin_id=kwargs.pop("plugin_id", "com.racarter.indigoplugin.texecom"),
                        deviceTypeId=kwargs.pop("deviceTypeId", "alarmZone"), **kwargs)


class HostileDevice:
    """A device proxy where EVERY attribute access raises.

    Models an Indigo device object being deleted underneath us: the proxy is
    still in the list, but reading anything off it blows up. The catalog must
    turn this into an ``Excluded`` verdict, never an exception — the picker
    walks the whole database, so one of these would otherwise empty the dialog.
    ``id``/``name`` raise too, deliberately: the picker cannot even label it.
    """

    def __getattr__(self, name):
        raise RuntimeError(f"device proxy is gone (reading {name!r})")


class HostilePluginIdDevice(RelayDevice):
    """Readable enough to be *listed*, but classification detonates.

    The other half of the class: ``id``/``name`` work, so the picker can build
    a row, and then reading ``pluginId`` — the loop guard's only input —
    raises. The verdict must be excluded, never eligible: an unreadable
    ``pluginId`` is precisely the case where we cannot prove the guard passed.
    """

    @property
    def pluginId(self):
        raise RuntimeError("pluginId blew up")

    @pluginId.setter
    def pluginId(self, value):
        pass  # the base __init__ assigns it; only the READ has to raise


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


class FakeHealthDevice:
    """A stand-in for the `matterBridgeHealth` device (issue #286).

    Just enough surface for ``ExportBridge._apply_subscription_churn``: an
    id (so the ``device_getter`` cache-hit path can look it up again) and a
    RECORDING ``updateStatesOnServer``, so a test can assert both the final
    states and how many times a write actually happened — the standing-churn
    "write once, not every tick" discipline has no other observable.
    """

    def __init__(self, dev_id):
        self.id = dev_id
        self.deviceTypeId = HEALTH_DEVICE_TYPE_ID
        self.states: dict = {}
        #: One entry per ``updateStatesOnServer`` call, each the kv-list it
        #: was given — a rewrite of identical states is a bug this exists to
        #: catch, so recording every call (not just the latest) matters.
        self.writes: list = []
        #: Raise instead of writing — issue #286 review finding 3's
        #: write-failure latch has no other way to be exercised.
        self.fail_writes = False

    def updateStatesOnServer(self, kvlist):
        if self.fail_writes:
            raise RuntimeError("updateStatesOnServer failed (fail_writes)")
        self.writes.append(list(kvlist))
        for kv in kvlist:
            self.states[kv["key"]] = kv["value"]


#: Mirrors ``export_bridge.HEALTH_DEVICE_TYPE_ID`` without importing that
#: module here — ``fakes.py`` is imported by suites that mock ``indigo``
#: differently, and a bare string keeps this fixture-only class import-free.
HEALTH_DEVICE_TYPE_ID = "matterBridgeHealth"


class FakeIndigoDevices:
    """Stands in for ``indigo.devices``: iterable, and subscriptable by id."""

    def __init__(self, devices=()):
        self._devices = list(devices)

    def add(self, device):
        self._devices.append(device)
        return device

    def drop(self, device_id):
        """Delete a device, for the paths that must survive one vanishing."""
        self._devices = [dev for dev in self._devices if dev.id != device_id]

    def __iter__(self):
        return iter(self._devices)

    def __len__(self):
        return len(self._devices)

    def __getitem__(self, device_id):
        for device in self._devices:
            if device.id == device_id:
                return device
        raise KeyError(device_id)

    def iter(self, _filter=None):
        """``indigo.devices.iter("self")`` — this suite has one plugin, so
        every device here already IS "self"; the filter is accepted and
        ignored, mirroring ``test_device_sync.py``'s own fake. Real Indigo
        EXCLUDES unconfigured devices from ``iter("self")`` (issue #62) —
        modelled the same way, via ``getattr(..., "configured", True)`` so a
        double without the attribute at all still iterates (most of this
        module's doubles predate ``configured`` and default to ``True``).
        """
        return [dev for dev in self._devices if getattr(dev, "configured", True)]
