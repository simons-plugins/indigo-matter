"""E3: the plugin-side export wiring — subscription, callbacks, dialog nudges.

``export_bridge`` is tested on its own in ``test_export_bridge.py``; this file
pins the four things only ``plugin.py`` can get wrong:

* **the ``deviceUpdated`` fast path.** The subscription is server-wide, so this
  callback fires for every device change on the whole Indigo database. If it
  ever does more than a set lookup for a device nobody exported, every user who
  exports one lamp pays for it on every state change in their house;
* **when the subscription is issued at all.** Default posture is inert (XG5), so
  a plugin with an empty allow-list must not ask Indigo for the firehose;
* **the delete path** — a deleted device leaves the allow-list *and* the bridge;
* **the dialog nudges** — a role change re-creates the accessory (§4.1), which
  costs the user their Home-app name and room, so it is a different call and a
  different message from an ordinary update.
"""
from __future__ import annotations

import importlib
import json
from unittest.mock import Mock

import pytest

import bridge_protocol
import export_catalog
from export_store import ExportEntry, ExportStore
from fakes import FakeIndigoDevices, RelayDevice

OURS = export_catalog.DEFAULT_PLUGIN_ID


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def devices(mock_indigo_base):
    collection = FakeIndigoDevices([
        RelayDevice(101, "Study Plug", onState=False),
        RelayDevice(102, "Porch Light", onState=False),
    ])
    mock_indigo_base.devices = collection
    return collection


@pytest.fixture
def plug(plugin_mod, devices):  # noqa: ARG001 - devices installs indigo.devices
    """A plugin in its post-startup shape, with the bridge mocked out."""
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = OURS
    p._version = "2026.7.28"
    p.pluginPrefs = {}
    p.exports = ExportStore(lambda: p.pluginPrefs, p.logger)
    p.export_bridge = Mock()
    # The fixture's allow-list is empty, so XG5 says there is no client — and a
    # bare Mock would otherwise answer `halted` truthily and add F10's note to
    # every status line in this file.
    p.export_bridge.active = False
    p._exported_ids = frozenset()
    p._subscribed_to_devices = False
    p._device_updates_seen = False
    p._no_update_ticks = 0
    p._resubscribe_attempts = 0
    p._resubscribe_gave_up = False
    p._export_callback_failed = set()
    p.runtime = None
    return p


class _ExplodingStore:
    """A store that fails the test on ANY attribute access (T2).

    ``deviceUpdated`` fires for every device on the server. Reading so much as
    ``exports.ids`` there is a cost every user pays on every state change in
    their house, forever — so the guard has to be "nothing was touched", not
    "nothing was called".
    """

    def __getattr__(self, name):
        raise AssertionError(f"store attribute {name!r} was read on the fast path")


def _values(**kwargs):
    base = {"exportFilter": "", "exportDevice": "0", "exportRole": "",
            "exportName": "", "exportInvert": False, "exportStatus": ""}
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# startup (T1)
# ---------------------------------------------------------------------------
class _FakeRuntime:
    is_running = True

    def start(self):
        pass

    def submit(self, coro):
        if hasattr(coro, "close"):
            coro.close()
        return Mock()


def _started_plugin(plugin_mod, monkeypatch, prefs):
    """Run the real ``Plugin.startup()`` with every I/O collaborator faked."""
    class FakeMatter:
        def __init__(self, *a, **k):
            pass

        def run(self):
            return None

    monkeypatch.setattr(plugin_mod, "MatterClient", FakeMatter)
    monkeypatch.setattr(plugin_mod, "AsyncRuntime", lambda logger: _FakeRuntime())
    monkeypatch.setattr(plugin_mod, "CommissionJobs", lambda *a, **k: Mock())
    monkeypatch.setattr(plugin_mod, "HttpApi", lambda *a, **k: Mock())
    # MUST be patched: an unpatched local-mode startup builds a real
    # ServerProcess against the real $HOME (see test_plugin_behaviour.py).
    monkeypatch.setattr(plugin_mod, "ServerProcess", lambda *a, **k: Mock())

    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = OURS
    p._version = "2026.7.28"
    p._subscribed_to_devices = False
    p.pluginPrefs = prefs
    p.proto = object()
    p.registry = object()
    p.device_sync = Mock()
    p.runtime = None
    p.server_process = None
    p.exports = None
    p.export_bridge = None
    p._exported_ids = frozenset()
    p.startup()
    return p


def _prefs_with_exports(*entries):
    """pluginPrefs carrying a persisted allow-list, written by the real store."""
    prefs: dict = {}
    store = ExportStore(lambda: prefs, Mock())
    for entry in entries:
        store.upsert(entry)
    return prefs


class TestStartupWithExistingExports:
    """T1: the startup half of ``_exports_changed`` had no test at all.

    Everything below is reachable only through the ``_exports_changed()`` call
    at the end of ``startup``. Delete that one line and the plugin still starts,
    still logs "N device(s) exported", still builds the bridge — and exports
    nothing, forever, for every user who already had an allow-list. Which is
    every user who restarts Indigo.
    """

    def test_a_persisted_allow_list_is_live_after_startup(self, plugin_mod, monkeypatch,
                                                          mock_indigo_base, devices):
        subscribe = Mock()
        mock_indigo_base.devices.subscribeToChanges = subscribe
        built: list = []
        monkeypatch.setattr(plugin_mod, "ExportBridge",
                            lambda *a, **k: built.append(Mock()) or built[-1])

        p = _started_plugin(plugin_mod, monkeypatch,
                            _prefs_with_exports(ExportEntry(101, "onOffLight"),
                                                ExportEntry(102, "onOffPlugInUnit")))

        assert p._exported_ids == frozenset({101, 102}), "the hot-path guard must be primed"
        subscribe.assert_called_once_with()
        assert len(built) == 1, "the bridge is built unconditionally"
        built[0].exports_changed.assert_called_once_with()

    def test_an_empty_allow_list_starts_inert(self, plugin_mod, monkeypatch, mock_indigo_base,
                                              devices):
        """XG5 — the same seam must NOT ask for the firehose on a fresh install."""
        subscribe = Mock()
        mock_indigo_base.devices.subscribeToChanges = subscribe
        monkeypatch.setattr(plugin_mod, "ExportBridge", lambda *a, **k: Mock())

        p = _started_plugin(plugin_mod, monkeypatch, {})

        assert p._exported_ids == frozenset()
        subscribe.assert_not_called()
        assert p._subscribed_to_devices is False


# ---------------------------------------------------------------------------
# The subscription decision
# ---------------------------------------------------------------------------
class TestSubscription:
    def test_an_empty_allow_list_never_asks_for_the_firehose(self, plug, mock_indigo_base):
        """XG5: a fresh install must cost an existing user nothing."""
        plug._exports_changed()
        mock_indigo_base.devices.subscribeToChanges = Mock()
        plug._exports_changed()
        mock_indigo_base.devices.subscribeToChanges.assert_not_called()

    def test_the_first_export_subscribes(self, plug, mock_indigo_base):
        subscribe = Mock()
        mock_indigo_base.devices.subscribeToChanges = subscribe
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        subscribe.assert_called_once_with()
        assert plug._subscribed_to_devices is True

    def test_it_subscribes_exactly_once(self, plug, mock_indigo_base):
        subscribe = Mock()
        mock_indigo_base.devices.subscribeToChanges = subscribe
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        for _ in range(5):
            plug._exports_changed()
        assert subscribe.call_count == 1

    def test_it_stays_subscribed_after_the_list_empties(self, plug, mock_indigo_base):
        """There is no documented unsubscribe — so this is a one-way door.

        Turning it off through an undocumented API is exactly the sort of thing
        that fails silently on an Indigo upgrade; the fast-path guard makes a
        stale subscription free anyway.
        """
        mock_indigo_base.devices.subscribeToChanges = Mock()
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        plug.exports.remove(101)
        plug._exports_changed()
        assert plug._subscribed_to_devices is True
        assert plug._exported_ids == frozenset()

    def test_a_failed_subscription_is_loud_and_not_fatal(self, plug, mock_indigo_base):
        mock_indigo_base.devices.subscribeToChanges = Mock(side_effect=RuntimeError("nope"))
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        assert plug._subscribed_to_devices is False
        assert plug.logger.error.called


# ---------------------------------------------------------------------------
# deviceUpdated
# ---------------------------------------------------------------------------
class TestDeviceUpdated:
    def test_a_non_exported_device_touches_nothing(self, plug, monkeypatch, plugin_mod):
        """THE hot path. It fires for every device change on the server.

        Anything beyond the set lookup — a classify, a store read, a handler
        lookup — is paid by every user for every device they never exported.
        """
        monkeypatch.setattr(plugin_mod.export_catalog, "classify",
                            Mock(side_effect=AssertionError("classified on the fast path")))
        monkeypatch.setattr(plugin_mod.export_handlers, "handler_for",
                            Mock(side_effect=AssertionError("handler looked up on the fast path")))
        # A Mock() answers every attribute happily, so it only catches a CALL.
        # This catches the ACCESS — including `exports.lock` or `exports.ids`,
        # either of which would put a lock or a set rebuild on the hot path.
        plug.exports = _ExplodingStore()
        plug._exported_ids = frozenset({999})

        plug.deviceUpdated(RelayDevice(101, "Study Plug", onState=False),
                           RelayDevice(101, "Study Plug", onState=True))

        plug.export_bridge.device_updated.assert_not_called()

    def test_the_base_class_still_gets_its_callback(self, plug):
        """The SDK's most-broken rule: the base does real work in here."""
        plug._exported_ids = frozenset()
        before, after = RelayDevice(101, "P"), RelayDevice(101, "P")
        plug.deviceUpdated(before, after)
        assert plug.base_calls == [("deviceUpdated", before, after)]

    def test_an_exported_device_is_handed_to_the_bridge(self, plug):
        plug._exported_ids = frozenset({101})
        before = RelayDevice(101, "Study Plug", onState=False)
        after = RelayDevice(101, "Study Plug", onState=True)
        plug.deviceUpdated(before, after)
        plug.export_bridge.device_updated.assert_called_once_with(before, after)

    def test_a_failing_bridge_never_breaks_indigos_callback(self, plug):
        plug._exported_ids = frozenset({101})
        plug.export_bridge.device_updated.side_effect = RuntimeError("boom")
        plug.deviceUpdated(RelayDevice(101, "P"), RelayDevice(101, "P"))
        assert plug.logger.exception.called

    def test_the_failure_names_the_device_and_repeats_once_per_streak(self, plug):
        """F6: a bare traceback per state change names nothing and never stops.

        This callback fires on every change of an exported device — a lamp on a
        dimmer ramp produces one per step. A stuck failure would write the same
        anonymous traceback into the event log tens of times a minute.
        """
        plug._exported_ids = frozenset({101})
        plug.export_bridge.device_updated.side_effect = RuntimeError("boom")
        for _ in range(10):
            plug.deviceUpdated(RelayDevice(101, "Study Plug"), RelayDevice(101, "Study Plug"))
        assert plug.logger.exception.call_count == 1
        errors = " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                          for c in plug.logger.error.call_args_list)
        assert "Study Plug" in errors and "101" in errors

    def test_a_recovered_device_can_report_again(self, plug):
        plug._exported_ids = frozenset({101})
        bridge = plug.export_bridge
        bridge.device_updated.side_effect = RuntimeError("boom")
        plug.deviceUpdated(RelayDevice(101, "P"), RelayDevice(101, "P"))
        bridge.device_updated.side_effect = None
        plug.deviceUpdated(RelayDevice(101, "P"), RelayDevice(101, "P"))
        bridge.device_updated.side_effect = RuntimeError("boom again")
        plug.deviceUpdated(RelayDevice(101, "P"), RelayDevice(101, "P"))
        assert plug.logger.exception.call_count == 2

    def test_it_survives_a_bridge_that_does_not_exist_yet(self, plug):
        plug.export_bridge = None
        plug._exported_ids = frozenset({101})
        plug.deviceUpdated(RelayDevice(101, "P"), RelayDevice(101, "P"))   # must not raise


# ---------------------------------------------------------------------------
# deviceDeleted
# ---------------------------------------------------------------------------
class TestDeviceDeleted:
    def test_a_non_exported_device_is_ignored_but_the_base_still_runs(self, plug):
        dev = RelayDevice(101, "Study Plug")
        plug.deviceDeleted(dev)
        plug.export_bridge.remove.assert_not_called()
        assert plug.base_calls == [("deviceDeleted", dev)]

    def test_an_exported_device_leaves_the_list_and_the_bridge(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        plug.deviceDeleted(RelayDevice(101, "Study Plug"))
        assert plug.exports.ids() == frozenset()
        # Issue #274 — a deletion is confirmed and permanent: the accessory
        # is destroyed outright.
        plug.export_bridge.remove.assert_called_once_with(101)
        assert plug._exported_ids == frozenset()

    def test_a_failed_store_write_still_removes_the_endpoint(self, plug, monkeypatch):
        """The device is gone either way — the accessory must not outlive it."""
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        monkeypatch.setattr(plug.exports, "remove",
                            Mock(side_effect=RuntimeError("prefs are read-only")))
        plug.deviceDeleted(RelayDevice(101, "Study Plug"))
        plug.export_bridge.remove.assert_called_once_with(101)
        assert plug.logger.error.called

    def test_a_raising_endpoint_removal_still_refreshes_the_cache(self, plug):
        """F9a: otherwise ``_exported_ids`` keeps an id whose device is gone.

        ``deviceUpdated`` would then hand a deleted device to the bridge on
        every later change, and ``deviceDeleted`` would never fire for it again
        — the cache has no other way back in sync until the plugin reloads.
        """
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        plug.export_bridge.remove.side_effect = RuntimeError("socket died")
        plug.deviceDeleted(RelayDevice(101, "Study Plug"))
        assert plug._exported_ids == frozenset()
        assert plug.logger.exception.called


# ---------------------------------------------------------------------------
# Dialog integration
# ---------------------------------------------------------------------------
class TestDialogNudges:
    def test_adding_an_export_upserts_the_endpoint(self, plug):
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        plug.export_bridge.upsert.assert_called_once_with(101)
        plug.export_bridge.replace.assert_not_called()
        assert plug._exported_ids == frozenset({101})

    def test_changing_only_the_name_is_still_an_upsert(self, plug):
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        plug.export_bridge.upsert.reset_mock()
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit", exportName="Desk"),
            "manageMatterExports")
        plug.export_bridge.upsert.assert_called_once_with(101)
        plug.export_bridge.replace.assert_not_called()

    def test_a_role_change_recreates_the_accessory(self, plug):
        """§4.1 refuses a role change in place, so it is remove-then-add."""
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        values = plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
        plug.export_bridge.replace.assert_called_once_with(101)
        assert "NEW one" in values["exportStatus"]
        assert "Apple Home" in values["exportStatus"]
        assert "issue #240" in values["exportStatus"]

    def test_an_ordinary_update_does_not_threaten_the_user(self, plug):
        values = plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        assert "NEW one" not in values["exportStatus"]

    def test_a_first_export_gets_no_published_as(self, plug):
        """Nothing has moved off the default derivation yet (issue #240)."""
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        assert plug.exports.get(101).published_as is None

    def test_an_ordinary_update_does_not_bump_the_generation(self, plug):
        """An update must never move the identity on its own (PR5 design §1.3)."""
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit", exportName="Desk"),
            "manageMatterExports")
        assert plug.exports.get(101).published_as is None

    def test_a_role_change_bumps_the_generation(self, plug):
        """Issue #240 — a role change moves the accessory to a fresh identity."""
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
        assert plug.exports.get(101).published_as == "indigo-101~2"

    def test_a_second_role_change_bumps_the_generation_again(self, plug):
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="doorLock"), "manageMatterExports")
        assert plug.exports.get(101).published_as == "indigo-101~3"

    def test_a_role_change_after_a_generation_bump_does_not_reset_it(self, plug):
        """An unrelated update between two role changes must not touch the
        identity it is not moving (PR5 design §1.3)."""
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffLight", exportName="Desk"),
            "manageMatterExports")
        assert plug.exports.get(101).published_as == "indigo-101~2"

    def test_a_refused_add_nudges_nothing(self, plug):
        plug.exportAddOrUpdate(_values(exportDevice="0"), "manageMatterExports")
        plug.export_bridge.upsert.assert_not_called()
        plug.export_bridge.replace.assert_not_called()

    def test_removing_an_export_removes_the_endpoint(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        plug.export_bridge.reset_mock()
        plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
        # Issue #274 — a deliberate un-export is as final as a deletion.
        plug.export_bridge.remove.assert_called_once_with(101)
        assert plug._exported_ids == frozenset()

    def test_removing_something_unexported_nudges_nothing(self, plug):
        plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
        plug.export_bridge.remove.assert_not_called()


class TestStatusSummary:
    def test_an_unbridgeable_role_is_called_out_in_the_dialog(self, plug, monkeypatch):
        """Otherwise the accessory is simply absent, with no visible cause.

        E4 made the handler table total over the v1 enum, so the only way to
        reach this branch is to take a handler away — which is exactly what a
        downgrade does to an allow-list written by a newer plugin.
        """
        import export_handlers
        monkeypatch.delitem(export_handlers.HANDLERS, "doorLock")
        plug.exports.upsert(ExportEntry(101, "doorLock"))
        summary = plug._export_summary()
        assert "cannot bridge" in summary

    def test_bridgeable_exports_get_no_such_note(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug.export_bridge.active = False
        assert "cannot bridge" not in plug._export_summary()

    def test_every_v1_role_is_bridgeable_so_the_note_never_fires(self, plug):
        """The E4 completion criterion, from the dialog's point of view."""
        for index, role in enumerate(sorted(bridge_protocol.ROLES)):
            plug.exports.upsert(ExportEntry(200 + index, role))
        assert "cannot bridge" not in plug._export_summary()

    def _bridge_in(self, plug, **state):
        """A bridge whose client is in some non-serving state."""
        client = Mock(halted=False, halted_reason=None, recovery=False, attached=True,
                      status=None)
        for key, value in state.items():
            setattr(client, key, value)
        plug.export_bridge.active = True
        plug.export_bridge.client = client

    def test_a_halted_bridge_is_named_in_the_dialog(self, plug):
        """F10: "2 device(s) exported." over a halted bridge is a lie of omission.

        The dialog is the only surface a user looks at to answer "why is my
        light not in Home?" — and every one of these states answers it.
        """
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug, halted=True, halted_reason="version skew", attached=False)
        summary = plug._export_summary()
        assert summary.startswith("1 device(s) exported.")
        assert "halted" in summary and "version skew" in summary
        assert "restart the bridge node" in summary

    def test_an_endpoint_map_rebuild_is_named(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug, recovery=True, attached=False)
        assert "endpoint-map" in plug._export_summary()

    def test_a_never_attached_bridge_says_exports_are_not_live(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug, attached=False)
        assert "Not connected to the bridge node" in plug._export_summary()
        assert "exports are not live" in plug._export_summary()

    def test_a_healthy_bridge_adds_nothing(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug)
        assert plug._export_summary() == "1 device(s) exported."

    def _status(self, **kw):
        import bridge_protocol
        base = dict(commissioned=True, fabrics=[], endpoint_count=1, endpoints=[],
                    drift=[], drift_checked=True, warnings=[])
        base.update(kw)
        return bridge_protocol.StatusReport(**base)

    def test_node_warnings_reach_the_dialog(self, plug):
        """§4.3 `warnings` were parsed and read by nobody.

        A map the node could not write showed up in the log at the moment it
        happened and nowhere afterwards — and this dialog is where a user goes
        when an accessory is behaving oddly.
        """
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug, status=self._status(warnings=["disk is full"]))
        assert "disk is full" in plug._export_summary()

    def test_drift_reaches_the_dialog(self, plug):
        import bridge_protocol
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        drift = bridge_protocol.parse_drift([{"uniqueId": "indigo-101", "expected": 2, "actual": 5}])
        self._bridge_in(plug, status=self._status(drift=drift))
        summary = plug._export_summary()
        assert "DRIFTED" in summary
        assert "never repaired automatically" in summary

    def test_an_unchecked_baseline_is_not_reported_as_an_all_clear(self, plug):
        """`drift: []` with `driftChecked: false` is an absence, not an answer."""
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug, status=self._status(drift_checked=False))
        assert "have not been checked" in plug._export_summary()

    def test_a_healthy_status_still_adds_nothing(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        self._bridge_in(plug, status=self._status())
        assert plug._export_summary() == "1 device(s) exported."

    def test_a_load_error_still_leads(self, plug):
        """The rescue copy is the thing a user must not be talked out of."""
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug.exports.load_error = "Could not read the export list."
        self._bridge_in(plug, halted=True, attached=False)
        summary = plug._export_summary()
        assert summary.startswith("Could not read the export list.")
        assert "halted" in summary


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
class TestLifecycleWiring:
    def test_shutdown_closes_the_bridge_before_the_loop_it_runs_on(self, plug):
        order = []
        plug.export_bridge.stop.side_effect = lambda *a, **k: order.append("bridge")
        plug.runtime = Mock(is_running=True, stop=lambda *a, **k: order.append("runtime"))
        plug.matter = None
        plug._install_thread = None
        plug._stopping = False
        plug.shutdown()
        assert order == ["bridge", "runtime"]

    def test_shutdown_leaves_the_agent_running(self, plug):
        """PM-B: a plugin reload must not un-pair anyone's ecosystems."""
        bridge = plug.export_bridge
        plug.runtime = Mock(is_running=True)
        plug.matter = None
        plug._install_thread = None
        plug._stopping = False
        plug.shutdown()
        bridge.stop.assert_called_once()
        assert not hasattr(bridge, "uninstall") or not bridge.uninstall.called

    def test_the_watchdog_ticks_the_bridge(self, plug):
        plug.runtime = Mock(is_running=True)
        plug.matter = None
        plug._health_tick()
        plug.export_bridge.health_tick.assert_called_once_with()

    def test_the_watchdog_skips_a_bridge_that_does_not_exist(self, plug):
        plug.export_bridge = None
        plug.runtime = Mock(is_running=True)
        plug.matter = None
        plug._health_tick()   # must not raise

    def test_the_watchdog_actually_drives_the_resubscribe_watchdog(self, plug, monkeypatch):
        """⊗ The resubscribe watchdog's ONLY production caller.

        Deleting ``self._resubscribe_tick()`` from ``_health_tick`` left the
        whole suite green, because every resubscribe test called the tick by
        hand. The feature exists to break a silence, so an unwired one is worth
        nothing at all — and nothing would ever have said so.
        """
        called = []
        monkeypatch.setattr(type(plug), "_resubscribe_tick",
                            lambda self: called.append(1), raising=False)
        plug.export_bridge = None
        plug.runtime = Mock(is_running=True)
        plug.matter = None
        plug._health_tick()
        assert called, "the watchdog tick must drive the resubscribe watchdog"


class TestResubscribeWatchdog:
    """E5: the belt and braces under an assumption the docs do not confirm.

    The whole outbound push path rests on ``subscribeToChanges`` behaving the
    same issued from a menu callback as from ``startup``. The canonical
    reference calls it a request to the server — which is why the conditional
    subscription is safe — but there is no acknowledgement to check and no
    unsubscribe to compare against, so if that is ever wrong the symptom is
    silence: exported accessories simply stop following Indigo.
    """

    def _exporting(self, plug, mock_indigo_base):
        subscribe = Mock()
        mock_indigo_base.devices.subscribeToChanges = subscribe
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        subscribe.reset_mock()
        return subscribe

    def test_it_re_issues_after_a_minute_with_no_device_updates_at_all(
            self, plug, mock_indigo_base):
        subscribe = self._exporting(plug, mock_indigo_base)
        for _ in range(4):
            plug._resubscribe_tick()
        subscribe.assert_called_once_with()

    def test_one_device_update_disarms_it_for_good(self, plug, mock_indigo_base):
        """Evidence the subscription took. Any device counts — it is server-wide."""
        subscribe = self._exporting(plug, mock_indigo_base)
        plug.deviceUpdated(RelayDevice(999, "Someone Else's Lamp"),
                           RelayDevice(999, "Someone Else's Lamp"))
        for _ in range(20):
            plug._resubscribe_tick()
        subscribe.assert_not_called()

    def test_it_gives_up_rather_than_nagging_forever(self, plug, mock_indigo_base):
        """"No updates" is also exactly what a quiet house looks like.

        An unbounded retry would be a permanent debug line for every user whose
        exported devices happen not to change, which is not a diagnostic.
        """
        subscribe = self._exporting(plug, mock_indigo_base)
        for _ in range(4 * 10):
            plug._resubscribe_tick()
        assert subscribe.call_count == 3

    def test_the_re_issues_are_a_minute_apart_not_back_to_back(
            self, plug, mock_indigo_base):
        """⊗ The streak counter must RESTART after a re-issue.

        Only the total was pinned, so deleting ``self._no_update_ticks = 0``
        survived: attempts 2 and 3 then fire on the two ticks immediately after
        the first, spending the whole bounded budget inside ~30s instead of
        giving the subscription three separate minutes to prove itself.
        """
        subscribe = self._exporting(plug, mock_indigo_base)
        for _ in range(4):
            plug._resubscribe_tick()
        assert subscribe.call_count == 1
        plug._resubscribe_tick()          # one tick later — must NOT re-issue
        assert subscribe.call_count == 1

    def test_giving_up_says_so_once_and_names_the_consequence(
            self, plug, mock_indigo_base):
        """⊗ It used to give up with a bare ``return`` at no log level at all.

        The one feature whose purpose is to break a silence ended by going
        silent, in exactly the house where it had failed to help.
        """
        self._exporting(plug, mock_indigo_base)
        for _ in range(4 * 10):
            plug._resubscribe_tick()
        giving_up = [c for c in plug.logger.warning.call_args_list
                     if "NOT following Indigo state" in str(c.args[0])]
        assert len(giving_up) == 1, "said once per streak, not once per tick"
        assert "reload the plugin" in str(giving_up[0].args[0])

    def test_it_stays_quiet_while_nothing_is_exported(self, plug, mock_indigo_base):
        subscribe = Mock()
        mock_indigo_base.devices.subscribeToChanges = subscribe
        for _ in range(20):
            plug._resubscribe_tick()
        subscribe.assert_not_called()


# ---------------------------------------------------------------------------
# issue #191 — the settable-attribute report's startup wiring
#
# Here rather than in test_diagnostics_menu.py because this needs the real
# ``Plugin.startup()`` harness above, and cloning forty lines of collaborator
# fakes to avoid a slightly odd file name is the worse trade.
# ---------------------------------------------------------------------------

class TestSurveyLogWiring:
    """Delete either line and everything still starts, still logs, still
    reconciles — and no device ever reports what it exposes, for anyone. The
    same shape of silent hole as the ``_exports_changed()`` case above, which is
    why it gets the same kind of test.
    """

    def test_startup_hands_device_sync_a_survey_log(self, plugin_mod, monkeypatch,
                                                    mock_indigo_base):
        import settings_report

        p = _started_plugin(plugin_mod, monkeypatch, {})
        assert isinstance(p.device_sync.survey_log, settings_report.SurveyLog), (
            "device_sync has no survey log, so the automatic report never fires"
        )

    def test_startup_restores_what_was_already_reported(self, plugin_mod, monkeypatch,
                                                        mock_indigo_base):
        """Without the load, every device re-reports on every plugin restart —
        which is exactly the wallpaper the once-per-device rule exists to avoid.
        """
        from plugin_constants import SURVEY_LOG_PREF

        p = _started_plugin(plugin_mod, monkeypatch, {SURVEY_LOG_PREF: '{"52": "fp-1"}'})
        assert not p.survey_log.should_report(0x34, "fp-1")

    def test_a_recorded_answer_reaches_prefs(self, plugin_mod, monkeypatch,
                                             mock_indigo_base):
        """The save hook has to be wired to THIS plugin's prefs, not to a
        captured mapping — Indigo rebinds pluginPrefs when the config dialog is
        saved."""
        from plugin_constants import SURVEY_LOG_PREF

        prefs: dict = {}
        p = _started_plugin(plugin_mod, monkeypatch, prefs)
        p.survey_log.record(0x34, "fp-9")
        assert json.loads(prefs[SURVEY_LOG_PREF]) == {"52": "fp-9"}
        assert mock_indigo_base.server.savePluginPrefs.called
