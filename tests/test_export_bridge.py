"""E3: the outbound export engine (`export_bridge`).

What is pinned here, and why each earns its place:

* **The endpoint provider re-classifies** — the allow-list is a user declaration
  made in the past, so a device that has since been deleted, disabled, taken
  over by this plugin, or re-typed must be skipped with a warning rather than
  sent as a stale spec (the E2 handover's standing requirement);
* **an E4 role is skipped, not sent** — an unknown role fails the WHOLE attach
  on the node (E3a), so one un-bridgeable export would silently un-export every
  working one;
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
        self.bridge = module.ExportBridge(
            self.store, self.runtime, mock_logger, lambda: self.prefs,
            plugin_version="2026.7.28", plugin_id=OURS,
            device_getter=self._device,
            client_factory=self._client,
        )

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

    def start(self) -> FakeBridgeClient:
        self.bridge.start()
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
        assert bridge_client.attach_timeout_for(0) == bridge_client.ATTACH_TIMEOUT
        assert bridge_client.attach_timeout_for(20) == bridge_client.ATTACH_TIMEOUT

    def test_a_large_set_gets_more_time_than_its_pacing_costs(self):
        # ~100ms per removal (§3.3) means 80 endpoints can spend 8s in pacing
        # alone — precisely the flat deadline it would otherwise be given.
        assert bridge_client.attach_timeout_for(80) > 80 * 0.1
        assert bridge_client.attach_timeout_for(80) == pytest.approx(14.0)

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

    def test_an_e4_role_is_skipped_rather_than_failing_the_whole_attach(
            self, bridge_mod, mock_logger, devices):
        """E3a: an unknown role fails the ENTIRE attach with ``internal``."""
        h = Harness(bridge_mod, mock_logger, devices,
                    [ExportEntry(101, "doorLock"), ExportEntry(102, "dimmableLight")])
        specs = h.bridge.endpoint_specs()
        assert [s.indigo_device_id for s in specs] == [102], "the good export must survive"
        assert "cannot be bridged yet" in warnings_of(mock_logger)

    def test_the_same_skip_is_warned_once_not_once_per_reconnect(self, bridge_mod, mock_logger,
                                                                 devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "doorLock")])
        for _ in range(5):
            h.bridge.endpoint_specs()
        assert mock_logger.warning.call_count == 1

    def test_a_changed_skip_reason_is_warned_again(self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "doorLock")])
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

    def test_the_client_is_dropped_even_if_the_final_attach_fails(
            self, bridge_mod, mock_logger, devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        client = h.start()
        client.fail["attach"] = ConnectionError("node is gone")
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.bridge.active is False
        assert "may linger" in warnings_of(mock_logger)

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
        h = self._harness(bridge_mod, mock_logger, devices, ExportEntry(101, "onOffLight"))
        same = RelayDevice(101, "Study Plug", onState=True)
        h.bridge.device_updated(same, RelayDevice(101, "Study Plug", onState=True))
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
                                                            devices):
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(101, "doorLock")])
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

    def test_a_command_for_an_e4_role_is_refused_not_attempted(self, bridge_mod, mock_logger,
                                                               devices, mock_indigo_base):
        """The lock seam: E4 owns it, and nothing here may auto-confirm it."""
        lock_id = FRAMES["command_lock"]["data"]["indigoDeviceId"]
        devices.add(RelayDevice(lock_id, "Front Door"))
        h = Harness(bridge_mod, mock_logger, devices, [ExportEntry(lock_id, "doorLock")])
        h.bridge.on_command(bridge_protocol.parse_command(FRAMES["command_lock"]["data"]))
        assert "cannot bridge" in warnings_of(mock_logger)
        mock_indigo_base.device.lock.assert_not_called()

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

    def test_a_halted_client_says_so_every_tick(self, bridge_mod, mock_logger, devices):
        """Halted is not transient: nothing is coming to fix it on its own."""
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.halted = True
        h.client.halted_reason = "version_skew"
        h.bridge.health_tick()
        assert "HALTED" in warnings_of(mock_logger)

    def test_the_recovery_state_says_nothing_is_exported(self, bridge_mod, mock_logger, devices):
        h = self._bridge(bridge_mod, mock_logger, devices)
        h.client.attached = False
        h.client.recovery = True
        h.bridge.health_tick()
        assert "rebuild" in warnings_of(mock_logger)
