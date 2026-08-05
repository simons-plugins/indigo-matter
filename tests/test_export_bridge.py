"""E3: the outbound export engine (`export_bridge`).

What is pinned here, and why each earns its place:

* **The endpoint provider re-classifies** — the allow-list is a user declaration
  made in the past, so a device that has since been deleted, disabled, taken
  over by this plugin, or re-typed must be skipped with a warning rather than
  sent as a stale spec (the E2 handover's standing requirement);
* **a role this version cannot bridge is skipped, not sent** — an unknown role
  fails the WHOLE attach on the node (E3a), so one un-bridgeable export would
  silently un-export every working one. E4 made the handler table total over the
  v1 enum, so the tests that exercise this path now *remove* a handler
  (:func:`unbridgeable_role`) rather than naming a role that has none: the
  behaviour still has to work, because it is what protects an old plugin from an
  allow-list written by a newer one;
* **the attach deadline scales with the endpoint count** — the node paces bulk
  removals ~100ms apart, so a fixed 8s deadline times out on exactly the large
  databases that most need export to work;
* **the client exists only while something is exported** (XG5), and emptying the
  allow-list goes out as the deliberate §3.1 ``replace_all`` attach, not as a
  disconnect;
* **nothing is awaited on Indigo's thread** — every push is fire-and-forget, and
  a failed one still reaches the log (§3.4).

References to ``§N`` are ``docs/BRIDGE_PROTOCOL.md``.
"""
from __future__ import annotations

import importlib

import pytest

import bridge_client
import bridge_protocol
import export_catalog
from export_store import ExportEntry, ExportStore

from conftest import load_bridge_frames
from fakes import (
    OTHER_PLUGIN_ID,
    DimmerDevice,
    FakeBridgeClient,
    FakeIndigoDevices,
    InlineExecutor,
    RecordingRuntime,
    RelayDevice,
    SprinklerDevice,
)

FRAMES = load_bridge_frames()
OURS = export_catalog.DEFAULT_PLUGIN_ID


@pytest.fixture
def bridge_mod(mock_indigo_base):
    """`export_bridge` (and the handlers it uses) bound to a mocked ``indigo``."""
    import export_handlers
    import export_bridge as module
    importlib.reload(export_handlers)
    importlib.reload(module)
    return module


class Harness:
    """An ExportBridge wired to fakes, plus the knobs the tests need."""

    def __init__(self, module, mock_logger, devices, entries=()):
        self.logger = mock_logger
        self.prefs: dict = {}
        self.devices = devices
        self.store = ExportStore(lambda: self.prefs, mock_logger)
        for entry in entries:
            self.store.upsert(entry)
        self.runtime = RecordingRuntime()
        self.clients: list[FakeBridgeClient] = []
        self.executor = InlineExecutor()
        self.saves = 0
        self.bridge = module.ExportBridge(
            self.store, self.runtime, mock_logger, lambda: self.prefs,
            plugin_version="2026.7.28", plugin_id=OURS,
            device_getter=self._device,
            client_factory=self._client,
            save_prefs=self._save_prefs,
            executor_factory=lambda: self.executor,
        )

    def _save_prefs(self) -> None:
        self.saves += 1

    def _device(self, device_id):
        try:
            return self.devices[device_id]
        except KeyError:
            return None

    def _client(self, logger, prefs, **kwargs):
        client = FakeBridgeClient(logger, prefs, **kwargs)
        self.clients.append(client)
        return client

    @property
    def client(self) -> FakeBridgeClient:
        assert self.clients, "no bridge client was created"
        return self.clients[-1]

    def start(self, seed: bool = True) -> FakeBridgeClient:
        """Start the client and, by default, simulate its attach.

        The real client's handshake calls the endpoint provider before anything
        else, and building a spec is what seeds the per-device "last pushed"
        snapshot every later diff is measured against (E5). A harness that
        started the client without that would leave every ``device_updated``
        test diffing against nothing — a state the running plugin cannot be in,
        because a push is gated on being attached and an attach always ran the
        provider first.
        """
        self.bridge.start()
        if seed:
            self.bridge.endpoint_specs()
        return self.client


def warnings_of(logger) -> str:
    return " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                    else str(call.args[0])
                    for call in logger.warning.call_args_list)


def errors_of(logger) -> str:
    return " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                    else str(call.args[0])
                    for call in logger.error.call_args_list)


@pytest.fixture
def unbridgeable_role(bridge_mod, monkeypatch):
    """Make ``doorLock`` a role this build has no handler for, and return it.

    E4 completed the §4.2 table, so there is no longer a real role without a
    handler — but the skip path is not dead code. It is what a plugin does with
    an allow-list entry written by a *newer* version of itself (the export blob
    lives in plugin prefs and survives a downgrade), and with any role a future
    protocol version adds. Removing a real handler is the only honest way to
    reach it.
    """
    import export_handlers
    monkeypatch.delitem(export_handlers.HANDLERS, "doorLock")
    return "doorLock"


@pytest.fixture
def devices():
    return FakeIndigoDevices([
        RelayDevice(101, "Study Plug", onState=False),
        DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40),
        SprinklerDevice(104, "Irrigation"),
        RelayDevice(105, "Matter Plug", plugin_id=OURS),
    ])


# ---------------------------------------------------------------------------
# The attach-timeout formula
# ---------------------------------------------------------------------------
class TestAttachTimeout:
    """E3a's pacing×count interaction, answered without a protocol change."""

    def test_small_sets_keep_the_flat_floor(self):
        """The floor is over the count SENT — which is not always the count paced.

        ``attach_timeout_for(0)`` returning the floor is correct arithmetic and
        wrong as a deadline for the ONE caller that sends zero endpoints on
        purpose: the §3.1 ``replace_all`` un-export sends nothing and makes the
        node remove everything. That path must size its own deadline over the
        removals — see
        ``TestLifecycle.test_the_un_export_deadline_is_sized_by_the_removals``.
        """
        assert bridge_client.attach_timeout_for(0) == bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(20) == bridge_client.ATTACH_TIMEOUT

    def test_a_large_set_gets_more_time_than_its_pacing_costs(self):
        # ~100ms per removal (§3.3) means 80 endpoints can spend 8s in pacing
        # alone — precisely the flat deadline it would otherwise be given.
        assert bridge_client.attach_timeout_for(80) > 80 * 0.1
        assert bridge_client.attach_timeout_for(80) == pytest.approx(14.0)

    def test_the_crossover_off_the_floor_is_where_the_arithmetic_says(self):
        """T3: 2.0 + 0.15n passes 8.0 between 40 and 41, and nowhere else.

        Pinned because the crossover is the only observable consequence of the
        two constants — tune either and this is the test that notices.
        """
        assert bridge_client.attach_timeout_for(40) == bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(41) > bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(41) == pytest.approx(8.15)

    def test_it_is_monotonic_and_never_negative(self):
        values = [bridge_client.attach_timeout_for(n) for n in range(0, 200, 10)]
        assert values == sorted(values)
        assert bridge_client.attach_timeout_for(-5) == bridge_client.ATTACH_TIMEOUT


# ---------------------------------------------------------------------------
# The endpoint provider (§3.1 reconcile source)
# ---------------------------------------------------------------------------
class TestEndpointProvider:
    def test_builds_a_spec_from_the_live_device(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(102, "dimmableLight")])
        (spec,) = h.bridge.endpoint_specs()
        assert spec.indigo_device_id == 102
        assert spec.role == "dimmableLight"
        assert spec.label == "Hall Dimmer"
        assert spec.reachable is True
        assert spec.states == {"onOff": True, "level": 40}

    def test_the_name_override_wins_over_the_indigo_name(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, "onOffLight", name_override="Desk Lamp")])
        assert h.bridge.endpoint_specs()[0].label == "Desk Lamp"

    def test_options_ride_along(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(102, "dimmableLight", options={"someKey": 1}))
        assert h.bridge.endpoint_specs()[0].options == {"someKey": 1}

    def test_a_disabled_device_is_unreachable_not_absent(self, bridge_mod, mock_logger, devices):
        """XAC8: greyed out in the ecosystem beats timing out."""
        devices[101].enabled = False
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        assert h.bridge.endpoint_specs()[0].reachable is False

    def test_an_unconfigured_device_is_unreachable(self, bridge_mod, mock_logger, devices):
        devices[101].configured = False
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        assert h.bridge.endpoint_specs()[0].reachable is False

    def test_a_deleted_device_is_skipped_with_a_warning(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(999, "onOffLight")])
        assert h.bridge.endpoint_specs() == []
        assert "no longer exists" in warnings_of(mock_logger)

    def test_a_now_excluded_device_is_skipped_with_its_reason(self, bridge_mod, mock_logger,
                                                              devices):
        """The store is NOT the guard — classify is, on every attach."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        # A sprinkler cannot be exported at all, so it can only get in by a
        # restored backup or a hand-edited pref — exactly what this covers.
        h.store.upsert(ExportEntry(104, "onOffLight"))
        assert h.bridge.endpoint_specs() == []
        assert export_catalog.REASON_SPRINKLER in warnings_of(mock_logger)

    def test_our_own_device_never_reaches_the_node(self, bridge_mod, mock_logger, devices):
        """XAC6/XNG3 — the loop guard, re-run at endpoint-build time."""
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(105, "onOffLight"))
        assert h.bridge.endpoint_specs() == []
        assert export_catalog.REASON_LOOP_GUARD in warnings_of(mock_logger)

    def test_a_role_the_device_no_longer_offers_is_skipped(self, bridge_mod, mock_logger,
                                                           devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        # A plain relay never offers dimmableLight; the user's dimmer was
        # replaced by a relay under the same device id.
        h.store.upsert(ExportEntry(101, "dimmableLight"))
        assert h.bridge.endpoint_specs() == []
        assert "no longer offers" in warnings_of(mock_logger)

    def test_an_unbridgeable_role_is_skipped_rather_than_failing_the_whole_attach(
            self, bridge_mod, mock_logger, devices, unbridgeable_role):
        """E3a: an unknown role fails the ENTIRE attach with ``internal``."""
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, unbridgeable_role), ExportEntry(102, "dimmableLight")])
        specs = h.bridge.endpoint_specs()
        assert [s.indigo_device_id for s in specs] == [102], "the good export must survive"
        assert "not one this plugin version can bridge" in warnings_of(mock_logger)

    def test_the_same_skip_is_warned_once_not_once_per_reconnect(self, bridge_mod, mock_logger,
                                                                 devices, unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        for _ in range(5):
            h.bridge.endpoint_specs()
        assert mock_logger.warning.call_count == 1

    def test_a_changed_skip_reason_is_warned_again(self, bridge_mod, mock_logger, devices,
                                                   unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        h.bridge.endpoint_specs()
        h.store.upsert(ExportEntry(101, "dimmableLight"))   # now a different failure
        h.bridge.endpoint_specs()
        assert mock_logger.warning.call_count == 2


# ---------------------------------------------------------------------------
# Client lifecycle (XG5)
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_an_empty_allow_list_starts_nothing(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge.exports_changed()
        assert h.bridge.active is False
        assert h.clients == []

    def test_the_first_export_starts_the_client(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.bridge.active is True
        assert h.client.ran is True

    def test_starting_twice_is_a_no_op(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.start()
        h.bridge.start()
        assert len(h.clients) == 1

    def test_emptying_the_allow_list_un_exports_deliberately_then_stops(
            self, bridge_mod, mock_logger, devices):
        """PRD §7: endpoints go, pairings stay — and it needs the §3.1 opt-in."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert client.only("attach") == ("attach", [], True), "must carry replace_all"
        assert client.closed is True
        assert h.bridge.active is False

    def test_the_un_export_deadline_is_sized_by_the_removals_not_the_empty_send(
            self, bridge_mod, mock_logger, devices):
        """X1: attach latency is dominated by REMOVALS, and this path removes all.

        The endpoint list sent is ``[]``, so letting the client derive the
        deadline from it hands the un-export of a 60-device database the flat 8s
        floor — while the node paces those 60 removals ~100ms apart (§3.3). It
        times out, warns that accessories "may linger", and then ``close()``
        yanks the socket out from under a reconcile that was going fine.
        """
        entries = [ExportEntry(200 + n, "onOffLight") for n in range(60)]
        h = Harness(bridge_mod, mock_logger, devices, entries)
        client = h.start()
        for entry in entries:
            h.store.remove(entry.indigo_device_id)
        h.bridge.exports_changed()

        assert client.attach_timeouts == [bridge_client.attach_timeout_for(60)]
        assert client.attach_timeouts[0] > 60 * 0.1, "must outlast the node's pacing"
        assert client.attach_timeouts[0] > bridge_client.ATTACH_TIMEOUT
        assert "may linger" not in warnings_of(mock_logger)

    def test_a_small_un_export_still_gets_the_floor(self, bridge_mod, mock_logger, devices):
        """Sizing by removals must not make the ordinary case slower to fail."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert client.attach_timeouts == [bridge_client.ATTACH_TIMEOUT]

    def test_the_client_is_dropped_even_if_the_final_attach_fails(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        client.fail["attach"] = ConnectionError("node is gone")
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.bridge.active is False
        assert "will linger" in warnings_of(mock_logger)
        # F4: the socket must still be released — the client is unreachable from
        # here on, so a skipped close() leaks it until the plugin reloads.
        assert client.closed is True

    def test_stop_is_idempotent(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.bridge.stop()
        h.bridge.stop()
        assert h.bridge.active is False

    def test_the_client_gets_the_ws_port_pref(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.prefs[bridge_protocol.PREF_WS_PORT] = "5999"
        assert h.start().prefs[bridge_protocol.PREF_WS_PORT] == "5999"


# ---------------------------------------------------------------------------
# Indigo → node
# ---------------------------------------------------------------------------
class TestDeviceUpdated:
    def _harness(self, bridge_mod, mock_logger, devices, entry):
        h = Harness(bridge_mod, mock_logger, devices, [entry])
        h.start()
        return h

    def test_a_state_change_becomes_a_set_state(self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        before = RelayDevice(101, "Study Plug", onState=False)
        after = RelayDevice(101, "Study Plug", onState=True)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_state") == ("set_state", 101, {"onOff": True})

    def test_a_level_change_becomes_a_set_state(self, bridge_mod, mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(102, "dimmableLight"))
        before = DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40)
        after = DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_state") == ("set_state", 102, {"level": 90})

    def test_an_unchanged_device_sends_nothing(self, bridge_mod, mock_logger, devices):
        """"Unchanged" means "equal to what we last PUSHED", since E5.

        ``devices[101]`` is ``onState=False``, so that is what the attach put on
        the wire and what this diff is measured against.
        """
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        same = RelayDevice(101, "Study Plug", onState=False)
        h.bridge.device_updated(same, RelayDevice(101, "Study Plug", onState=False))
        assert h.client.names() == []

    def test_a_rename_re_sends_the_spec_so_the_label_follows(self, bridge_mod, mock_logger,
                                                             devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        devices[101].name = "Desk Plug"
        h.bridge.device_updated(RelayDevice(101, "Study Plug"), devices[101])
        _name, spec = h.client.only("upsert_endpoint")
        assert spec.label == "Desk Plug"

    def test_a_rename_is_ignored_when_the_user_pinned_a_name(self, bridge_mod, mock_logger,
                                                             devices):
        h = self._harness(bridge_mod, mock_logger, devices,
                          ExportEntry(101, "onOffLight", name_override="Desk Lamp"))
        devices[101].name = "Something Else"
        h.bridge.device_updated(RelayDevice(101, "Study Plug"), devices[101])
        assert "upsert_endpoint" not in h.client.names()

    def test_disabling_a_device_sets_reachable_false(self, bridge_mod, mock_logger, devices):
        """XAC8 groundwork — the split §3.5 command, not a cluster state."""
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        before = RelayDevice(101, "Study Plug", enabled=True)
        after = RelayDevice(101, "Study Plug", enabled=False)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_reachable") == ("set_reachable", 101, False)

    def test_re_enabling_a_device_sets_reachable_true(self, bridge_mod, mock_logger, devices):
        """T3: the other direction of XAC8 — a device that comes back must say so.

        A one-way ``set_reachable`` leaves the accessory greyed out in every
        ecosystem forever, which looks exactly like a dead device.
        """
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        before = RelayDevice(101, "Study Plug", enabled=False)
        after = RelayDevice(101, "Study Plug", enabled=True)
        h.bridge.device_updated(before, after)
        assert h.client.only("set_reachable") == ("set_reachable", 101, True)

    def test_an_unbridgeable_role_update_is_skipped_silently(self, bridge_mod, mock_logger,
                                                             devices, unbridgeable_role):
        """T3: the provider already warned; repeating it per state change is noise."""
        h = self._harness(bridge_mod, mock_logger, devices,
                          ExportEntry(101, unbridgeable_role))
        mock_logger.reset_mock()
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert h.client.names() == []
        assert mock_logger.warning.call_count == 0

    def test_a_device_removed_between_the_guard_and_here_is_dropped(self, bridge_mod,
                                                                    mock_logger, devices):
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        h.store.remove(101)
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert h.client.names() == []

    def test_pushes_are_never_awaited(self, bridge_mod, mock_logger, devices):
        """§3.4 — a state push must not make Indigo's thread wait on Matter."""
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        h.client.fail["set_state"] = ConnectionError("socket died mid-write")
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        # It failed, it did not raise into Indigo, and it was NOT silent.
        assert "set_state dev 101 failed" in warnings_of(mock_logger)

    def test_nothing_is_sent_before_the_attach_completes(self, bridge_mod, mock_logger, devices):
        """An incremental frame sent un-attached is refused (§1.1) and pointless."""
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        h.client.attached = False
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        h.bridge.upsert(101)
        h.bridge.remove(101)
        assert h.client.names() == []


class TestIncrementalCrud:
    def test_upsert_sends_the_current_spec(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.bridge.upsert(102)
        _name, spec = h.client.only("upsert_endpoint")
        assert spec.indigo_device_id == 102 and spec.role == "dimmableLight"

    def test_upsert_of_an_unbridgeable_export_sends_nothing(self, bridge_mod, mock_logger,
                                                            devices, unbridgeable_role):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, unbridgeable_role)])
        h.start()
        h.bridge.upsert(101)
        assert h.client.names() == []

    def test_remove_drops_the_endpoint(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.bridge.remove(101)
        assert h.client.only("remove_endpoint") == ("remove_endpoint", 101)

    def test_a_role_change_is_a_remove_then_an_add(self, bridge_mod, mock_logger, devices):
        """§4.1 rejects a role change in place — ecosystems cache the type."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffPlugInUnit")])
        h.start()
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.replace(101)
        assert h.client.names() == ["remove_endpoint", "upsert_endpoint"]
        assert h.client.calls[1][1].role == "onOffLight"

    def test_a_role_change_for_a_vanished_entry_only_removes(self, bridge_mod, mock_logger,
                                                             devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.store.remove(102)
        h.bridge.replace(102)
        assert h.client.names() == ["remove_endpoint"]


# ---------------------------------------------------------------------------
# Node → Indigo (§5 command events)
# ---------------------------------------------------------------------------
class TestOnCommand:
    def _deliver(self, h, frame_name):
        data = FRAMES[frame_name]["data"]
        h.bridge.on_command(bridge_protocol.parse_command(data))

    def test_an_on_off_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                  mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "onOffLight")])
        self._deliver(h, "command_on_off")
        mock_indigo_base.device.turnOn.assert_called_once_with(devices[123456789])

    def test_a_set_level_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                    mock_indigo_base):
        devices.add(DimmerDevice(123456789, "Golden Lamp"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "dimmableLight")])
        self._deliver(h, "command_set_level")
        mock_indigo_base.dimmer.setBrightness.assert_called_once_with(
            devices[123456789], value=60)

    def test_a_set_color_temp_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                         mock_indigo_base):
        devices.add(DimmerDevice(900004, "Golden CT", whiteLevel=70,
                                 supportsWhiteTemperature=True))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(900004, "colorTemperatureLight")])
        self._deliver(h, "command_set_color_temp")
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert kwargs["whiteTemperature"] == 3125     # 1e6 / 320 mireds
        assert kwargs["whiteLevel"] == 70

    def test_a_set_color_command_reaches_the_device(self, bridge_mod, mock_logger, devices,
                                                    mock_indigo_base):
        devices.add(DimmerDevice(900005, "Golden RGB", supportsRGB=True))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(900005, "extendedColorLight")])
        self._deliver(h, "command_set_color")
        _args, kwargs = mock_indigo_base.dimmer.setColorLevels.call_args
        assert set(kwargs) == {"redLevel", "greenLevel", "blueLevel"}
        assert kwargs["blueLevel"] == 100             # hue 210, saturation 80

    def test_a_command_for_an_unexported_device_is_refused_with_a_warning(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PRD §7 race row, against the golden frame for exactly this case."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        self._deliver(h, "command_unexported_device")
        assert "not exported" in warnings_of(mock_logger)
        mock_indigo_base.device.turnOff.assert_not_called()

    def test_a_command_for_a_vanished_device_is_refused(self, bridge_mod, mock_logger, devices,
                                                        mock_indigo_base):
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "onOffLight")])   # no such Indigo device
        self._deliver(h, "command_on_off")
        assert "no longer exists" in warnings_of(mock_logger)
        mock_indigo_base.device.turnOn.assert_not_called()

    def test_a_command_the_role_does_not_define_is_refused(self, bridge_mod, mock_logger,
                                                           devices, mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(123456789, "onOffLight")])
        self._deliver(h, "command_set_level")            # a light that cannot dim
        assert "does not define" in warnings_of(mock_logger)
        mock_indigo_base.dimmer.setBrightness.assert_not_called()

    def test_a_command_for_an_unbridgeable_role_is_refused_not_attempted(
            self, bridge_mod, mock_logger, devices, mock_indigo_base, unbridgeable_role):
        """A command must never be attempted for a role this build cannot serve."""
        lock_id = FRAMES["command_lock"]["data"]["indigoDeviceId"]
        devices.add(RelayDevice(lock_id, "Front Door"))
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(lock_id, unbridgeable_role)])
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_lock"]["data"]))
        assert "cannot bridge" in warnings_of(mock_logger)
        mock_indigo_base.device.lock.assert_not_called()

    def test_a_lock_command_reaches_indigo_and_confirms_nothing(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PRD §7: dispatch the lock, then stop. No optimistic state write."""
        lock_id = FRAMES["command_lock"]["data"]["indigoDeviceId"]
        dev = RelayDevice(lock_id, "Front Door", onState=False)
        devices.add(dev)
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(lock_id, "doorLock")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_lock"]["data"]))
        mock_indigo_base.device.lock.assert_called_once_with(dev)
        # Nothing was pushed back: the ecosystem's `lockState` moves when the
        # real device does and `deviceUpdated` says so, not because we asked.
        assert h.client.names() == []

        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_unlock"]["data"]))
        mock_indigo_base.device.unlock.assert_called_once_with(dev)

    def test_a_failing_dispatch_is_logged_not_raised(self, bridge_mod, mock_logger, devices,
                                                     mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug"))
        mock_indigo_base.device.turnOn.side_effect = RuntimeError("server said no")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        self._deliver(h, "command_on_off")
        assert "server said no" in errors_of(mock_logger)


# ---------------------------------------------------------------------------
# Failure surfacing
# ---------------------------------------------------------------------------
class TestFailureSurfacing:
    def _bridge(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        return h

    def test_a_refused_attach_names_the_code(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_MASS_REMOVAL_REFUSED, "no intent")
        assert bridge_protocol.ERR_MASS_REMOVAL_REFUSED in errors_of(mock_logger)

    def test_an_invalid_endpoint_map_says_what_a_rebuild_costs(self, bridge_mod, mock_logger,
                                                               devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "unreadable")
        assert "duplicate accessories" in errors_of(mock_logger)

    def test_version_skew_says_restart_the_agent(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_version_skew(bridge_protocol.Hello(2, "9.9.9", "1.0"))
        assert "restart the bridge agent" in errors_of(mock_logger)

    def test_drift_is_reported_never_repaired(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_drift_detected(bridge_protocol.parse_drift(
            FRAMES["drift_detected"]["data"]["drift"]))
        assert "DRIFT" in errors_of(mock_logger)
        assert h.client.names() == [], "drift must not trigger a repair"

    def test_an_unreachable_node_is_reported_once_per_outage(self, bridge_mod, mock_logger,
                                                             devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        for attempt in range(1, 6):
            h.bridge._on_unreachable(attempt)
        assert mock_logger.warning.call_count == 1
        assert "started by hand" in warnings_of(mock_logger)


class TestHealthTick:
    def _bridge(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        return h

    def test_an_inactive_bridge_says_nothing(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge.health_tick()
        assert mock_logger.warning.call_count == 0

    def test_an_attached_client_says_nothing(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        for _ in range(10):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 0

    def test_a_disconnected_client_warns_once_after_about_a_minute(self, bridge_mod, mock_logger,
                                                                   devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        for _ in range(10):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 1
        assert "~1 min" in warnings_of(mock_logger)

    def test_a_halted_client_says_so_once_per_streak(self, bridge_mod, mock_logger, devices):
        """Halted is not transient: nothing is coming to fix it on its own.

        F9: which is exactly why it must not be said every 15s tick, forever —
        the state never changes, so the repeat carries no new information and
        buries everything else in the event log.
        """
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.halted = True
        h.client.halted_reason = "version_skew"
        for _ in range(20):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 1
        assert "HALTED" in warnings_of(mock_logger)
        assert "version_skew" in warnings_of(mock_logger)

    def test_a_recovered_client_can_warn_again(self, bridge_mod, mock_logger, devices):
        """Once per STREAK, not once per process: a second outage must be heard."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.halted = True
        h.bridge.health_tick()
        h.client.halted = False
        h.bridge._on_attached(bridge_protocol.StatusReport(
            commissioned=True, fabrics=[], endpoint_count=1, endpoints=[], drift=[]))
        h.client.halted = True
        h.bridge.health_tick()
        assert mock_logger.warning.call_count == 2

    def test_the_recovery_state_says_nothing_is_exported_once(self, bridge_mod, mock_logger,
                                                               devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.client.recovery = True
        for _ in range(20):
            h.bridge.health_tick()
        assert mock_logger.warning.call_count == 1
        assert "rebuild" in warnings_of(mock_logger)


# ---------------------------------------------------------------------------
# Nothing that stops export may be silent (the PR #124 silent-failure sweep)
# ---------------------------------------------------------------------------
class TestSilentFailures:
    """Every path here used to `return` with no trace of what was dropped."""

    def _bridge(self, bridge_mod, mock_logger, devices, role="onOffLight"):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, role)])
        h.start()
        return h

    def test_a_drop_while_merely_unattached_names_the_device_at_debug(
            self, bridge_mod, mock_logger, devices):
        """F1: attach WILL reconcile this, so it is debug — but not silence."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.bridge.upsert(101)
        debug = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                         else str(call.args[0])
                         for call in mock_logger.debug.call_args_list)
        assert "101" in debug
        assert mock_logger.warning.call_count == 0

    @pytest.mark.parametrize("state,expected", [
        ("halted", "HALTED (version_skew)"),
        ("recovery", "endpoint-map"),
    ])
    def test_a_drop_while_halted_or_in_recovery_is_a_warning_with_the_reason(
            self, bridge_mod, mock_logger, devices, state, expected):
        """F1: the client's own loud path is unreachable from here — replicate it.

        ``_live_client`` gates before ``set_state`` is ever called, so
        ``BridgeClient._log_dropped_state_push``'s warning is dead code on this
        route: a halted bridge dropped every push in total silence.
        """
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        setattr(h.client, state, True)
        h.client.halted_reason = "version_skew"
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert expected in warnings_of(mock_logger)
        assert "101" in warnings_of(mock_logger)

    def test_the_halted_drop_warning_is_once_per_streak(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.client.halted = True
        for _ in range(10):
            h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                    RelayDevice(101, "P", onState=True))
        assert mock_logger.warning.call_count == 1

    def test_a_failed_run_loop_schedule_is_a_warning_not_a_debug_line(
            self, bridge_mod, mock_logger, devices):
        """F2: if run() never got scheduled, NOTHING is exported, ever."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.runtime.is_running = False
        h.bridge.start()
        assert "bridge client run loop" in warnings_of(mock_logger)

    def test_a_failed_un_export_schedule_is_a_warning(self, bridge_mod, mock_logger, devices):
        """F2: the accessories stay in every paired ecosystem, unexplained."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.runtime.is_running = False
        h.store.remove(101)
        h.bridge.exports_changed()
        assert "un-exporting everything" in warnings_of(mock_logger)

    def test_an_ordinary_push_that_cannot_be_scheduled_stays_at_debug(
            self, bridge_mod, mock_logger, devices):
        """F2: a set_state is re-delivered by the next attach; it is not a loss."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.runtime.is_running = False
        h.bridge.device_updated(RelayDevice(101, "P", onState=False),
                                RelayDevice(101, "P", onState=True))
        assert mock_logger.warning.call_count == 0
        assert mock_logger.debug.called

    def test_a_failed_dispatch_corrects_the_ecosystem_back_to_the_truth(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """F5: otherwise Home shows the light the user asked for and Indigo does not.

        The ecosystem applied the command optimistically the moment it sent it.
        Logging and returning leaves those two beliefs permanently split until
        something else happens to that device.
        """
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        mock_indigo_base.device.turnOn.side_effect = RuntimeError("server said no")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        assert h.client.only("set_state") == ("set_state", 123456789, {"onOff": False})
        assert "still shows" in errors_of(mock_logger)

    def test_a_state_read_failure_dedupes_on_the_reason_not_the_message(
            self, bridge_mod, mock_logger, devices):
        """F8b: a varying ``str(exc)`` in the key defeats the dedupe entirely."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        calls = {"n": 0}

        def exploding_states_for(_dev, _options=None):
            calls["n"] += 1
            raise RuntimeError(f"transient read error #{calls['n']}")

        handler = bridge_mod.export_handlers.handler_for("onOffLight")
        original, handler.states_for = handler.states_for, exploding_states_for
        try:
            for _ in range(5):
                h.bridge.endpoint_specs()
        finally:
            handler.states_for = original
        assert mock_logger.warning.call_count == 1
        assert "transient read error #1" in warnings_of(mock_logger), \
            "the varying detail belongs in the LINE, just not in the key"

    def test_a_non_terminal_attach_refusal_is_reported_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        """F8c: a transient refusal reconnects on backoff — this fires every cycle."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        for _ in range(10):
            h.bridge._on_attach_refused(bridge_protocol.ERR_INTERNAL, "node is confused")
        assert mock_logger.error.call_count == 1

    def test_a_terminal_refusal_is_still_said_every_time(self, bridge_mod, mock_logger, devices):
        """Terminal refusals do not loop, and each one is a distinct decision."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "unreadable")
        h.bridge._on_attach_refused(bridge_protocol.ERR_ENDPOINT_MAP_INVALID, "unreadable")
        assert mock_logger.error.call_count == 2

    def test_a_reattach_lets_a_refusal_be_reported_again(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.bridge._on_attach_refused(bridge_protocol.ERR_INTERNAL, "node is confused")
        h.bridge._on_attached(bridge_protocol.StatusReport(
            commissioned=True, fabrics=[], endpoint_count=1, endpoints=[], drift=[]))
        h.bridge._on_attach_refused(bridge_protocol.ERR_INTERNAL, "node is confused again")
        assert mock_logger.error.call_count == 2

    def test_a_failing_diff_names_the_device_and_repeats_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        """F6/F7: a bare traceback per state change tells you nothing and never stops."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        handler = bridge_mod.export_handlers.handler_for("onOffLight")
        original = handler.diff_from
        handler.diff_from = lambda _p, _n, _opt=None: (
            _ for _ in ()).throw(RuntimeError("bad device"))
        try:
            for _ in range(10):
                h.bridge.device_updated(RelayDevice(101, "Study Plug", onState=False),
                                        RelayDevice(101, "Study Plug", onState=True))
        finally:
            handler.diff_from = original
        assert mock_logger.exception.call_count == 1
        assert "Study Plug" in errors_of(mock_logger)
        assert "101" in errors_of(mock_logger)

    def test_a_failed_dispatch_corrects_with_the_exports_own_options(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PR #125 C1: the corrective push used to drop the export's §4.1 options.

        A covering whose motor is wired backwards carries ``invert``. Read
        WITHOUT it, ``states_for`` answers ``100 - actual`` — so a failed
        ``goToPosition`` on such a blind "corrected" the ecosystem to the mirror
        image of where the blind really is. That is worse than the stale value it
        replaced: it is a wrong answer pushed with the authority of a fresh
        reading, and nothing later contradicts it.

        Brightness 30 with ``invert`` is §4.2 position 70. The bug pushed 30.
        """
        dev = DimmerDevice(700, "Back Blind", onState=True, brightness=30)
        devices.add(dev)
        mock_indigo_base.dimmer.setBrightness.side_effect = RuntimeError("motor jammed")
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(700, "windowCovering", options={"invert": True})])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=700, command="goToPosition", args={"position": 90}))
        assert h.client.only("set_state") == ("set_state", 700, {"position": 70})

    def test_a_correction_with_nothing_readable_says_so_rather_than_returning(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PR #125 H1: the error line one above promises "pushing the real one back".

        A lock whose ``onState`` has gone ``None`` has no truth to push, so
        ``states_for`` is empty and the push never happened — after the log had
        already said it would. Silence there is the worst possible outcome for
        the one role where the user is standing at the door.
        """
        dev = RelayDevice(701, "Front Door", onState=None)
        devices.add(dev)
        mock_indigo_base.device.lock.side_effect = RuntimeError("mesh unreachable")
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(701, "doorLock")])
        h.start()
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=701, command="lock", args={}))
        assert h.client.names() == []
        assert "cannot push truth" in warnings_of(mock_logger)

    def test_a_device_that_stops_reporting_a_key_is_named_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        """PR #125 C4: the ONLY trace of a published key vanishing.

        ``diff`` iterates the new snapshot, so a key that disappeared produced
        no push and no log — and a reconnect does not heal it either, because
        ``attach`` sends the same partial snapshot and §3.4 leaves absent keys
        untouched. The ecosystem shows the last value it was told, indefinitely.
        Keeping that value is the decision; being quiet about it was the bug.
        """
        devices.add(RelayDevice(702, "Front Door", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(702, "doorLock")])
        h.start()
        for _ in range(6):
            h.bridge.device_updated(RelayDevice(702, "Front Door", onState=True),
                                    RelayDevice(702, "Front Door", onState=None))
        assert mock_logger.warning.call_count == 1
        warnings = warnings_of(mock_logger)
        assert "702" in warnings and "locked" in warnings
        assert "last known value" in warnings
        assert h.client.names() == [], "an absent key must not be fabricated onto the wire"

    def test_a_key_that_starts_reporting_again_re_arms_the_latch(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(703, "doorLock")])
        devices.add(RelayDevice(703, "Side Door", onState=True))
        h.start()
        h.bridge.device_updated(RelayDevice(703, "Side Door", onState=True),
                                RelayDevice(703, "Side Door", onState=None))
        h.bridge.device_updated(RelayDevice(703, "Side Door", onState=None),
                                RelayDevice(703, "Side Door", onState=True))
        h.bridge.device_updated(RelayDevice(703, "Side Door", onState=True),
                                RelayDevice(703, "Side Door", onState=None))
        assert mock_logger.warning.call_count == 2

    def test_a_command_that_lawfully_did_nothing_is_named_once_per_streak(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """PR #125 M1: ``stopMotion`` on a dimmer-modelled blind.

        Neither an error (§4.2 declares the command for this role) nor a
        success worth being silent about: the user pressed stop and the blind
        kept going. It used to be a debug line from a stateless handler that
        could not name the device.
        """
        devices.add(DimmerDevice(704, "Landing Blind", onState=True, brightness=50))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(704, "windowCovering")])
        h.start()
        for _ in range(4):
            h.bridge.on_command(bridge_protocol.BridgeCommand(
                indigo_device_id=704, command="stopMotion", args={}))
        assert mock_logger.warning.call_count == 1
        assert "704" in warnings_of(mock_logger)
        assert "changed nothing" in warnings_of(mock_logger)
        mock_indigo_base.dimmer.setBrightness.assert_not_called()

    def test_a_command_that_did_something_clears_the_no_op_latch(
            self, bridge_mod, mock_logger, devices):
        devices.add(DimmerDevice(705, "Landing Blind", onState=True, brightness=50))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(705, "windowCovering")])
        h.start()
        stop = bridge_protocol.BridgeCommand(indigo_device_id=705, command="stopMotion", args={})
        h.bridge.on_command(stop)
        h.bridge.on_command(bridge_protocol.BridgeCommand(
            indigo_device_id=705, command="goToPosition", args={"position": 40}))
        h.bridge.on_command(stop)
        assert mock_logger.warning.call_count == 2


# ---------------------------------------------------------------------------
# E5 — the un-export that did not land (XAC7)
# ---------------------------------------------------------------------------
class TestPendingUnExport:
    """The orphaned-accessories-forever gap, closed.

    Emptying the allow-list is the ONE moment the plugin ever tells the node to
    remove everything (§3.1 ``intent: replace_all``). If that attach does not
    land — the node is down, launchd has not restarted it, the plugin is
    reloading — nothing ever says it again: the allow-list is empty, so XG5 says
    no client, so there is no connection, so there is no attach. Every exported
    accessory stays in every paired ecosystem for good, controlling nothing.
    """

    def _emptied_with_a_dead_node(self, bridge_mod, mock_logger, devices, count=1):
        entries = [ExportEntry(200 + n, "onOffLight") for n in range(count)]
        h = Harness(bridge_mod, mock_logger, devices, entries)
        client = h.start()
        client.fail["attach"] = ConnectionError("node is gone")
        for entry in entries:
            h.store.remove(entry.indigo_device_id)
        h.bridge.exports_changed()
        return h

    def test_a_failed_un_export_is_recorded_in_prefs(self, bridge_mod, mock_logger, devices):
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices, count=3)
        assert h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] == 3
        assert h.saves >= 1, "the debt has to reach disk to survive the reload it covers"

    def test_a_successful_un_export_leaves_no_debt(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs

    def test_the_debt_reconnects_even_though_nothing_is_exported(
            self, bridge_mod, mock_logger, devices):
        """XG5 says no client while nothing is exported. This is the exception."""
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        assert h.bridge.active is False

        h.bridge.exports_changed()          # e.g. the next startup, or a dialog close

        assert h.bridge.active is True, "an outstanding un-export must reconnect on its own"
        assert h.bridge._owes_replace_all() is True

    def test_the_reconnected_attach_carries_the_replace_all_intent(
            self, bridge_mod, mock_logger, devices):
        """Without the intent the node answers `mass_removal_refused`, forever."""
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.bridge.exports_changed()
        provider = h.client.kwargs["replace_all_provider"]
        assert provider() is True

    def test_a_re_populated_allow_list_does_NOT_cancel_the_debt(
            self, bridge_mod, mock_logger, devices):
        """⊗ The debt survives a disjoint re-add, or the client halts forever.

        The old rule ANDed the debt with an empty allow-list — "a re-populated
        list supersedes it" — and the two are not alternatives. Empty the list
        while the node is down (debt), then export a *different* device: the
        node still has to remove everything it holds, its §3.1 guard still sees
        zero survivors, and an attach without the intent is refused with
        `mass_removal_refused`, which HALTS the client permanently behind a
        message blaming the allow-list.
        """
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.bridge._owes_replace_all() == 1

    def test_a_successful_attach_discharges_the_debt_and_drops_the_client(
            self, bridge_mod, mock_logger, devices):
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.bridge.exports_changed()
        client = h.client

        h.bridge._on_attached(bridge_protocol.parse_status(
            FRAMES["attach_replace_all"]["response"]["result"]), True)

        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs
        assert client.closed is True, "XG5: nothing exported, so nothing needs a socket"
        assert h.bridge.active is False
        assert "outstanding un-export completed" in " ".join(
            str(call.args[0]) for call in mock_logger.info.call_args_list)

    def test_an_attach_that_did_NOT_carry_the_intent_discharges_nothing(
            self, bridge_mod, mock_logger, devices):
        """⊗ Discharge on what the attach SENT, never on live state.

        Reading "is a debt recorded now?" here wipes a debt written *while this
        attach was in flight*: the user empties the allow-list at the moment an
        ordinary reconnect lands, that attach carried no intent and removed
        nothing, and one step later the flag is cleared with a log line
        asserting an un-export that never happened. The accessories stay in
        every ecosystem and nothing anywhere knows they should not.
        """
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.bridge.exports_changed()

        h.bridge._on_attached(bridge_protocol.parse_status(
            FRAMES["attach_replace_all"]["response"]["result"]), False)

        assert h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] == 1
        assert "outstanding un-export completed" not in " ".join(
            str(call.args[0]) for call in mock_logger.info.call_args_list)

    def test_a_debt_discharged_beside_real_exports_keeps_the_client(
            self, bridge_mod, mock_logger, devices):
        """XG5 hangs up only when nothing is exported — not merely on discharge."""
        h = self._emptied_with_a_dead_node(bridge_mod, mock_logger, devices)
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        client = h.client

        h.bridge._on_attached(bridge_protocol.parse_status(
            FRAMES["attach_replace_all"]["response"]["result"]), True)

        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs
        assert client.closed is False, "there is something exported; it needs the socket"
        assert h.bridge.active is True

    def test_an_unreadable_flag_is_treated_as_no_debt(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.prefs[bridge_mod.PREF_PENDING_REPLACE_ALL] = "not a number"
        assert h.bridge._owes_replace_all() == 0
        h.bridge.exports_changed()
        assert h.bridge.active is False


class TestStartGating:
    """The destroy-then-recreate race around the un-export."""

    def test_start_during_an_un_export_is_deferred_not_dropped(
            self, bridge_mod, mock_logger, devices, monkeypatch):
        """A user who empties the list and immediately re-adds must end up connected.

        ``_replace_all_then_stop`` clears ``self.client`` the instant it fires,
        so an unguarded ``start()`` would build a second client on top of the
        socket the first is still using to say "remove everything" — and then
        race it to the same node, which supersedes one of them.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        first = h.start()
        started_during: list = []

        async def _slow_attach(endpoints=None, *, replace_all=False, timeout=None):
            # Stand in for a node that takes its time over the paced removals.
            h.store.upsert(ExportEntry(101, "onOffLight"))
            h.bridge.exports_changed()
            started_during.append(len(h.clients))
            return None

        monkeypatch.setattr(first, "attach", _slow_attach)
        h.store.remove(101)
        h.bridge.exports_changed()

        assert started_during == [1], "no second client while the un-export is in flight"
        assert len(h.clients) == 2, "and the deferred start still happened afterwards"
        assert h.bridge.active is True


# ---------------------------------------------------------------------------
# E5 — diffing against what was pushed, not against the last Indigo reading
# ---------------------------------------------------------------------------
class TestPushedSnapshot:
    def _colour(self, devices, green):
        return DimmerDevice(300, "Strip", supportsColor=True, supportsRGB=True,
                            onState=True, brightness=100,
                            redLevel=100, greenLevel=green, blueLevel=0)

    def test_a_sub_tolerance_ramp_still_reaches_the_ecosystem(
            self, bridge_mod, mock_logger, devices):
        """The bug this whole change exists for: unbounded, silent drift.

        Hue carries a ±1° tolerance (Matter's 0-254 hue round-trips ±1°). One
        unit of green against full red moves the recovered hue about 0.6°, so
        every single step of a ramp compares "unchanged" against the *previous
        Indigo reading* — and the accessory sits at the colour it had when the
        ramp started while the lamp walks arbitrarily far away from it. No
        error, no warning, nothing in the log at all.

        Against the last value actually pushed, the same tolerance bounds the
        TOTAL error at 1°, which is what a tolerance is supposed to mean.
        """
        devices.add(self._colour(devices, 0))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(300, "extendedColorLight")])
        h.start()

        for green in range(1, 11):
            h.bridge.device_updated(self._colour(devices, green - 1), self._colour(devices, green))

        hues = [call[2]["hue"] for call in h.client.calls
                if call[0] == "set_state" and "hue" in call[2]]
        assert hues, "a 6-degree ramp must reach the ecosystem"
        assert hues[-1] > 4, f"the last pushed hue must track the lamp, got {hues}"

    def test_a_state_equal_to_the_last_push_sends_nothing(
            self, bridge_mod, mock_logger, devices):
        """The other half: this must not become "push on every callback"."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        for _ in range(5):
            h.bridge.device_updated(DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40),
                                    DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40))
        assert h.client.names() == []

    def test_a_partial_push_is_merged_not_replaced(self, bridge_mod, mock_logger, devices):
        """§3.4 state maps are partial and the node leaves absent keys alone.

        A snapshot that replaced rather than merged would forget every key the
        last push happened not to carry, and re-send it on the next change.
        """
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(102, "dimmableLight")])
        h.start()
        h.bridge.device_updated(DimmerDevice(102, "Hall Dimmer", onState=True, brightness=40),
                                DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90))
        h.bridge.device_updated(DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90),
                                DimmerDevice(102, "Hall Dimmer", onState=True, brightness=90))
        assert [call for call in h.client.calls if call[0] == "set_state"] == [
            ("set_state", 102, {"level": 90}),
        ], "onOff was already pushed by the attach and must not be re-sent"

    def test_the_snapshot_dies_with_the_export(self, bridge_mod, mock_logger, devices):
        """Bounded memory: entries are removed with the export, never accumulated."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        assert 101 in h.bridge._pushed
        h.store.remove(101)
        h.bridge.remove(101)
        assert h.bridge._pushed == {}

    def test_a_skipped_device_drops_its_snapshot(self, bridge_mod, mock_logger, devices):
        """A device we are not sending to has a snapshot that is stale by definition."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        assert 101 in h.bridge._pushed
        devices.drop(101)
        h.bridge.endpoint_specs()
        assert 101 not in h.bridge._pushed


# ---------------------------------------------------------------------------
# E5 — §5 commands leave the loop
# ---------------------------------------------------------------------------
class TestCommandDispatchOffLoop:
    def test_the_indigo_call_runs_on_the_command_worker(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """The loop is SHARED with the inbound matter-server client.

        A blocking ``indigo.device.turnOn`` on it makes every live Matter device
        update wait behind somebody pressing a button in the Home app.
        """
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()

        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

        assert h.executor.submitted == 1
        mock_indigo_base.device.turnOn.assert_called_once()

    def test_a_command_for_an_unexported_device_costs_no_thread_hop(
            self, bridge_mod, mock_logger, devices):
        """The store and role lookups stay on the loop: they are dict reads."""
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        assert h.executor.submitted == 0
        assert "not exported" in warnings_of(mock_logger)

    def test_the_worker_is_shut_down_with_the_client(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))
        h.bridge.stop()
        # `wait=False`: a dispatch blocked on a wedged IndigoServer must not hold
        # plugin shutdown open. `cancel_futures=True`: work that has not STARTED
        # must not run against a plugin that is already tearing down.
        assert h.executor.shutdown_calls == [(False, True)]

    def test_a_queued_coroutine_cannot_rebuild_the_worker_after_stop(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        """A stopped bridge must not leave a live worker thread behind it.

        ``_command_worker`` builds on first use, and a §5 command already queued
        on the loop when ``stop()`` ran would otherwise construct a brand-new
        ThreadPoolExecutor underneath a bridge that has shut down — one nothing
        ever joins.
        """
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        h.bridge.stop()

        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

        assert h.executor.submitted == 0
        assert h.bridge._executor is None


# ---------------------------------------------------------------------------
# E5 hardening (PR #126 dual-review batch)
# ---------------------------------------------------------------------------
class TestCommandWorkerHealth:
    """One wedged `indigo.*` call must not be a silent house-wide outage.

    The worker is deliberately single-threaded (per-device FIFO), and the price
    is that whatever is at the front blocks every command for every device. Left
    unbounded and unwatched that is an outage with NO log output at all: the
    frames keep arriving, the queue keeps growing, and from Indigo's side
    nothing happened.
    """

    def _harness(self, bridge_mod, mock_logger, devices, hang=False):
        devices.add(RelayDevice(123456789, "Golden Plug", onState=False))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(123456789, "onOffLight")])
        h.start()
        # Set AFTER start(): the attach's own provider run must not hang.
        h.executor.hang = hang
        return h

    def test_the_worker_is_single_threaded_on_purpose(self, bridge_mod):
        """FIFO is a CORRECTNESS property, not a resource budget.

        Two commands for the same accessory running concurrently lets a
        `setLevel 20` overtake a `setLevel 80` — both "succeed" and the lamp is
        left at the wrong brightness with nothing to correct it. A pool would
        pass every other test in this file.
        """
        executor = bridge_mod._command_executor()
        try:
            assert executor._max_workers == 1
        finally:
            executor.shutdown(wait=False)

    def test_dispatch_order_is_preserved_by_a_real_executor(self, bridge_mod):
        """The property the single worker exists for, against a real pool."""
        executor = bridge_mod._command_executor()
        order: list = []
        try:
            futures = [executor.submit(order.append, n) for n in range(25)]
            for future in futures:
                future.result(timeout=5)
        finally:
            executor.shutdown(wait=True)
        assert order == list(range(25))

    def test_a_wedged_dispatch_is_named_with_its_command_and_device(
            self, bridge_mod, mock_logger, devices, mock_indigo_base, monkeypatch):
        monkeypatch.setattr(bridge_mod, "COMMAND_TIMEOUT", 0.01)
        h = self._harness(bridge_mod, mock_logger, devices, hang=True)
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_on_off"]["data"]))

        errors = " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                          else str(call.args[0]) for call in mock_logger.error.call_args_list)
        assert "onOff" in errors and "123456789" in errors
        assert "has not returned" in errors

    def test_the_queue_depth_warning_fires_once_per_streak(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        h = self._harness(bridge_mod, mock_logger, devices)
        # Stand in for work handed to a worker that is not coming back.
        h.bridge._submitted = bridge_mod.COMMAND_QUEUE_WARN

        h.bridge.health_tick()
        h.bridge.health_tick()

        queued = [call for call in mock_logger.warning.call_args_list
                  if "queued on the command worker" in str(call.args[0])]
        assert len(queued) == 1, "a standing queue must not repeat every 15s"

        # ...and it re-arms once the queue drains, so a NEW pile-up is news.
        h.bridge._completed = h.bridge._submitted
        h.bridge.health_tick()
        h.bridge._submitted += bridge_mod.COMMAND_QUEUE_WARN
        h.bridge.health_tick()
        queued = [call for call in mock_logger.warning.call_args_list
                  if "queued on the command worker" in str(call.args[0])]
        assert len(queued) == 2

    def test_stop_reports_how_many_commands_were_dropped(
            self, bridge_mod, mock_logger, devices, mock_indigo_base):
        h = self._harness(bridge_mod, mock_logger, devices)
        h.bridge._submitted = 4
        h.bridge._completed = 1
        h.bridge._executor = h.executor

        h.bridge.stop()

        assert "3 ecosystem command(s) still queued" in warnings_of(mock_logger)


class TestNodeWarnings:
    """§4.3 `warnings` — the node's only route to a log a human reads.

    The node writes to stdout and, in this milestone, is started BY HAND, so its
    stdout is a terminal that closed hours ago. A map it could not write is the
    exact fault E5 exists to make visible.
    """

    def _status(self, warnings):
        return bridge_protocol.parse_status({
            "commissioned": True, "fabrics": [], "endpointCount": 0,
            "endpoints": [], "drift": [], "driftChecked": True, "warnings": warnings,
        })

    def test_a_reported_warning_reaches_the_indigo_log(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge._on_attached(self._status(["Could not write the endpoint map"]), False)
        assert "Could not write the endpoint map" in warnings_of(mock_logger)

    def test_a_standing_warning_is_said_once_per_streak(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        status = self._status(["disk full"])
        h.bridge._on_attached(status, False)
        h.bridge._on_attached(status, False)
        said = [call for call in mock_logger.warning.call_args_list
                if "disk full" in str(call.args)]
        assert len(said) == 1

    def test_a_new_warning_beside_a_standing_one_is_still_news(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge._on_attached(self._status(["disk full"]), False)
        h.bridge._on_attached(self._status(["disk full", "identity unwritable"]), False)
        assert "identity unwritable" in warnings_of(mock_logger)

    def test_an_empty_warnings_list_says_nothing_and_re_arms(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge._on_attached(self._status(["disk full"]), False)
        h.bridge._on_attached(self._status([]), False)
        h.bridge._on_attached(self._status(["disk full"]), False)
        said = [call for call in mock_logger.warning.call_args_list
                if "disk full" in str(call.args)]
        assert len(said) == 2, "a fault that recurs after clearing is news again"


class TestDriftLatch:
    """`_on_drift_detected` had no latch, and drift is never repaired.

    After a `factory_reset preserveEndpointNumbers: true` that is EVERY exported
    device on every reconcile, with no way for the user to clear it short of
    §3.11 — so the one error that names the problem is buried under its own
    repetitions.
    """

    def _drift(self, *pairs):
        return bridge_protocol.parse_drift(
            [{"uniqueId": u, "expected": e, "actual": a} for u, e, a in pairs])

    def test_the_same_drift_set_is_reported_once(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        drift = self._drift(("indigo-1", 2, 5))
        h.bridge._on_drift_detected(drift)
        h.bridge._on_drift_detected(self._drift(("indigo-1", 2, 5)))
        assert mock_logger.error.call_count == 1

    def test_a_device_joining_the_drift_is_reported(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [])
        h.bridge._on_drift_detected(self._drift(("indigo-1", 2, 5)))
        h.bridge._on_drift_detected(self._drift(("indigo-1", 2, 5), ("indigo-2", 3, 6)))
        assert mock_logger.error.call_count == 2


class TestUnExportSnapshotAndGating:
    def test_the_pushed_snapshots_are_dropped_by_an_un_export(
            self, bridge_mod, mock_logger, devices):
        """Nothing is exported, so nothing has a last-pushed state.

        Left behind, the snapshots are a baseline for devices the node no longer
        holds — and the next diff against one would suppress a key the ecosystem
        has never actually been told.
        """
        devices.add(RelayDevice(101, "Lamp", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        assert h.bridge._pushed

        h.store.remove(101)
        h.bridge.exports_changed()

        assert h.bridge._pushed == {}

    def test_a_failed_submit_does_not_gate_start_forever(
            self, bridge_mod, mock_logger, devices):
        """`_un_exporting` gates `start()`, and its coroutine's `finally` is the
        only thing that clears it. If the coroutine was never scheduled, that
        gate stays shut for the life of the plugin and a user who re-adds a
        device gets a permanently inert bridge."""
        devices.add(RelayDevice(101, "Lamp", onState=True))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start()
        h.runtime.is_running = False

        h.store.remove(101)
        h.bridge.exports_changed()

        assert h.bridge._un_exporting is False
        h.runtime.is_running = True
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.bridge.active is True


class TestIndigoDeviceLookup:
    def test_a_missing_device_is_quiet(self, bridge_mod, mock_logger, mock_indigo_base):
        mock_indigo_base.devices = {}
        assert bridge_mod._indigo_device(999, mock_logger) is None
        assert "could not read Indigo device" not in warnings_of(mock_logger)

    def test_indigo_failing_to_answer_is_NOT_reported_as_a_deleted_device(
            self, bridge_mod, mock_logger, mock_indigo_base):
        """The bare `except Exception` gave both the same silent None.

        "Device %s no longer exists" sends the user to delete an export that was
        never the problem; a broken IPC connection needs the opposite response.
        """
        class _Broken:
            def __getitem__(self, _key):
                raise ConnectionError("IndigoServer went away")

        mock_indigo_base.devices = _Broken()
        assert bridge_mod._indigo_device(101, mock_logger) is None
        said = warnings_of(mock_logger)
        assert "could not read Indigo device" in said
        assert "NOT the device having been deleted" in said
