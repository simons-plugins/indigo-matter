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
from unittest.mock import Mock

import pytest

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
    p._exported_ids = frozenset()
    p._subscribed_to_devices = False
    p.runtime = None
    return p


def _values(**kwargs):
    base = {"exportFilter": "", "exportDevice": "0", "exportRole": "",
            "exportName": "", "exportInvert": False, "exportStatus": ""}
    base.update(kwargs)
    return base


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
        plug.exports = Mock(side_effect=AssertionError("store read on the fast path"))
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
        assert "RE-CREATES" in values["exportStatus"]
        assert "Apple Home" in values["exportStatus"]

    def test_an_ordinary_update_does_not_threaten_the_user(self, plug):
        values = plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        assert "RE-CREATES" not in values["exportStatus"]

    def test_a_refused_add_nudges_nothing(self, plug):
        plug.exportAddOrUpdate(_values(exportDevice="0"), "manageMatterExports")
        plug.export_bridge.upsert.assert_not_called()
        plug.export_bridge.replace.assert_not_called()

    def test_removing_an_export_removes_the_endpoint(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        plug._exports_changed()
        plug.export_bridge.reset_mock()
        plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
        plug.export_bridge.remove.assert_called_once_with(101)
        assert plug._exported_ids == frozenset()

    def test_removing_something_unexported_nudges_nothing(self, plug):
        plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
        plug.export_bridge.remove.assert_not_called()


class TestStatusSummary:
    def test_an_e4_role_is_called_out_in_the_dialog(self, plug):
        """Otherwise the accessory is simply absent, with no visible cause."""
        plug.exports.upsert(ExportEntry(101, "doorLock"))
        summary = plug._export_summary()
        assert "cannot bridge yet" in summary

    def test_bridgeable_exports_get_no_such_note(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight"))
        assert "cannot bridge yet" not in plug._export_summary()


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
