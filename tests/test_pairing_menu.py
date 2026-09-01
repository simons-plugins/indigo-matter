"""E6: pairing, fabric management, the QR page, and the §5.5 config readout.

The user-facing half of PRD-indigo-matter-export §6. Everything the *node* needs
for this shipped in E5 (`get_pairing`, `open_commissioning_window`,
`remove_fabric`, and the §5 events); until now the plugin could not reach any of
it, so a bridge could be built and never paired.

Three shapes are pinned here because they are forced by what Indigo dialogs can
actually do, not by preference:

* **the codes go to the EVENT LOG**, because Indigo dialogs have no dynamic
  labels — a value computed by the callback cannot be shown in the dialog that
  produced it;
* **the QR goes on an IWS-served page**, because a log line cannot carry an
  image;
* **the config readout is a read-only textfield seeded by
  ``getPrefsConfigUiValues``**, for the same no-dynamic-labels reason.

Every confirm-gate test asserts against **the CLIENT mock**, not merely against
the errors dict: the E5 round-two review found both recovery gates were inert in
the tests and live in production, because a second gate one line below filled the
same ``errors`` key and the assertion could not tell the two apart.
"""
from __future__ import annotations

import importlib
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import bridge_protocol
import export_catalog
from export_store import ExportEntry, ExportStore

from fakes import FakeIndigoDevices, RelayDevice

SERVER_PLUGIN = (Path(__file__).parent.parent
                 / "indigo-matter.indigoPlugin" / "Contents" / "Server Plugin")
MENU_ITEMS_XML = SERVER_PLUGIN / "MenuItems.xml"
PLUGIN_CONFIG_XML = SERVER_PLUGIN / "PluginConfig.xml"
ACTIONS_XML = SERVER_PLUGIN / "Actions.xml"
OURS = export_catalog.DEFAULT_PLUGIN_ID


@pytest.fixture
def plugin_mod(mock_indigo_base):
    mock_indigo_base.server.getWebServerURL.return_value = "http://jarvis.local:8176"
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def devices(mock_indigo_base):
    collection = FakeIndigoDevices([RelayDevice(101, "Study Plug")])
    mock_indigo_base.devices = collection
    return collection


class _FakeRuntime:
    """Resolves whatever the menu submits, or raises. Mirrors test_export_menu's."""

    def __init__(self, results=None, error=None):
        self.results = list(results or [])
        self.error = error
        self.submitted: list = []

    def submit(self, coro):
        self.submitted.append(coro)
        coro.close()
        return self

    def result(self, timeout=None):  # noqa: ARG002
        if self.error is not None:
            raise self.error
        return self.results.pop(0) if self.results else None


@pytest.fixture
def plug(plugin_mod, devices):  # noqa: ARG001 - devices installs indigo.devices
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = OURS
    p.pluginPrefs = {}
    p.exports = ExportStore(lambda: p.pluginPrefs, p.logger)
    p.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
    p.export_bridge = None
    p.bridge_process = None
    p.runtime = _FakeRuntime()
    p._exported_ids = frozenset({101})
    p._install_thread = None
    p._stopping = False
    return p


def _bridge_with(plug, *, connected=True, fabrics=None, active=True, enabled=True,
                 window=None):
    client = Mock(connected=connected, attached=connected, halted=False, recovery=False)
    bridge = Mock(client=client, fabrics=fabrics, active=active, enabled=enabled,
                  window_expires_at=window)
    plug.export_bridge = bridge
    return client


def _pairing(commissioned=True, window_open=True, manual="1234-567-8901",
             qr="MT:ABCDEFG", expires="2026-08-05T12:00:00Z", fabrics=()):
    return bridge_protocol.PairingReport(
        commissioned=commissioned, window_open=window_open, window_expires_at=expires,
        manual_pairing_code=manual, qr_pairing_code=qr, fabrics=list(fabrics))


def _fabric(index, vendor=0x1349, label=""):
    return bridge_protocol.FabricInfo(fabric_index=index, label=label, vendor_id=vendor)


def _iso(*, minutes: int) -> str:
    """An RFC 3339 stamp `minutes` from now — never a literal date.

    The readout compares against the clock, so a hard-coded timestamp is a test
    that starts failing on a date nobody wrote down.
    """
    when = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _removal(removed=True, remaining=1):
    return bridge_protocol.FabricRemoval(removed=removed, remaining=remaining)


def _unpair_bridge(plug, *, fabrics, removal):
    """A bridge wired for the unpair path: removal result, then the cache re-read.

    The menu makes two round trips — ``remove_fabric`` and the ``get_pairing``
    that refreshes the picker's cache — so the fake runtime has to answer both,
    in order.
    """
    client = _bridge_with(plug, fabrics=fabrics)
    plug.runtime = _FakeRuntime([removal, _pairing(fabrics=fabrics[:-1])])
    return plug.export_bridge, client


def _first_run_agent(tmp_path, logger):
    """A REAL :class:`BridgeProcess` on a machine that has never installed the package.

    Faithful where a ``Mock`` is not, which is the whole point: the first-run
    dead end lives in the interaction between ``ensure_installed`` (writes the
    plist, but only once preflight passes) and ``restart`` (which needs a plist
    to exist on disk). A ``Mock(restart=…True)`` has a plist by virtue of being a
    Mock, so the old test asserted the bug away.

    Only ``install`` is replaced, and it does what npm would: plant the package
    entry. launchctl is a FakeRunner; the filesystem is really written to.
    """
    import bridge_agent as bridge_agent_mod
    from test_server_process import FakeRunner

    home = tmp_path / "home"
    bindir = home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "npx").write_text("#!/bin/sh\n")
    (bindir / "node").write_text("#!/bin/sh\n")

    class _FirstRunBridge(bridge_agent_mod.BridgeProcess):
        def install(self, install_spec=bridge_agent_mod.DEFAULT_INSTALL_SPEC):  # noqa: ARG002
            entry = Path(self.project_dir) / "node_modules" / self.spec.package / "dist" / "main.js"
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.write_text("// installed by the fake npm\n")
            return True

    return _FirstRunBridge({}, logger, home=str(home), npx_path=str(bindir / "npx"),
                           runner=FakeRunner(), sleep=lambda *_a: None)


def _running_agent(state="running"):
    """An agent double whose `run_state()` answers `state`, with the real names."""
    from launch_agent import LaunchAgent

    return Mock(ensure_installed=Mock(return_value=False), ws_port="5581",
                matter_port="5540", run_state=Mock(return_value=state),
                RUNNING=LaunchAgent.RUNNING, UNKNOWN=LaunchAgent.UNKNOWN,
                NOT_LOADED=LaunchAgent.NOT_LOADED,
                LOADED_NOT_RUNNING=LaunchAgent.LOADED_NOT_RUNNING,
                preflight=Mock(return_value=None),
                tail_error_log=Mock(return_value=None),
                log_dir="/tmp/logs", project_dir="/tmp/proj")


def _healthy_agent(**overrides):
    """A bridge agent double whose preflight PASSES (so the log tail is reached).

    `Mock()` alone will not do since the diagnosis learned to ask preflight
    first: a bare Mock returns a truthy Mock, which reads as "it cannot start".
    """
    agent = Mock(project_dir="/Users/x/indigo-matter",
                 log_dir="/Users/x/Library/Logs/indigo-matter",
                 matter_port="5540", ws_port="5581", **overrides)
    agent.preflight.return_value = None
    return agent


def indigo_dict(plugin_mod):
    return plugin_mod.indigo.Dict()


def plugin_module_no_selection_id() -> str:
    import plugin as plugin_module
    return plugin_module.NO_SELECTION_ID


def _logged(logger) -> str:
    calls = (logger.info.call_args_list + logger.warning.call_args_list
             + logger.error.call_args_list)
    return " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                    for c in calls)


# ---------------------------------------------------------------------------
# XML shape
# ---------------------------------------------------------------------------
def _menu_item(item_id):
    for item in ET.parse(MENU_ITEMS_XML).getroot().findall("MenuItem"):
        if item.get("id") == item_id:
            return item
    raise AssertionError(f"{item_id} menu item missing")


def _config_fields():
    return {f.get("id"): f for f in ET.parse(PLUGIN_CONFIG_XML).getroot().findall("Field")}


class TestMenuShape:
    def test_the_pair_menu_exists_and_is_wired(self, plugin_mod):
        item = _menu_item("pairMatterBridge")
        assert item.findtext("CallbackMethod") == "menuPairMatterBridge"
        assert hasattr(plugin_mod.Plugin, "menuPairMatterBridge")
        # A CallbackMethod plus a ConfigUI means Execute/Cancel; the button says
        # what it does rather than "Execute".
        assert item.findtext("ButtonTitle") == "Open pairing window"

    def test_the_pair_dialog_defaults_to_the_900_second_maximum(self):
        fields = {f.get("id"): f for f in _menu_item("pairMatterBridge")
                  .find("ConfigUI").findall("Field")}
        assert fields["duration"].get("defaultValue") == "900"
        intro = fields["pairIntro"].findtext("Label")
        assert "EVENT LOG" in intro, "the user must be told where the code appears"
        assert "uncertified" in intro, "expected in every ecosystem — say so first"

    def test_the_unpair_menu_demands_TWO_ticks_and_a_dynamic_picker(self, plugin_mod):
        item = _menu_item("unpairEcosystem")
        assert item.findtext("CallbackMethod") == "menuUnpairEcosystem"
        assert hasattr(plugin_mod.Plugin, "menuUnpairEcosystem")
        fields = {f.get("id"): f for f in item.find("ConfigUI").findall("Field")}
        for name in ("confirm", "confirmAgain"):
            assert fields[name].get("type") == "checkbox"
            assert fields[name].get("defaultValue") == "false"
        picker = fields["fabric"].find("List")
        assert picker.get("method") == "getBridgeFabrics"
        assert hasattr(plugin_mod.Plugin, "getBridgeFabrics")
        # The last-fabric case E5 hardened must be named before it happens.
        assert "resets itself" in fields["unpairWarning"].findtext("Label")

    def test_the_bridge_install_menu_is_a_sibling_of_the_controllers(self, plugin_mod):
        """Separately versioned packages, separately installed: a user recovering
        a wedged bridge must not be made to reinstall a working controller."""
        item = _menu_item("installBridgeNode")
        assert item.findtext("CallbackMethod") == "menuInstallBridgeNode"
        assert hasattr(plugin_mod.Plugin, "menuInstallBridgeNode")
        assert _menu_item("installMatterServer").findtext("CallbackMethod") \
            == "menuInstallMatterServer"


class TestConfigShape:
    def test_the_export_section_carries_the_switch_the_ports_and_the_readout(self):
        fields = _config_fields()
        assert fields["exportEnabled"].get("type") == "checkbox"
        assert fields["exportEnabled"].get("defaultValue") == "true"
        assert fields["bridgeWsPort"].get("defaultValue") == "5581"
        assert fields["bridgeMatterPort"].get("defaultValue") == "5540"
        # Labels are static in Indigo, so the readout has to be a read-only field.
        assert fields["exportReadout"].get("type") == "textfield"
        assert fields["exportReadout"].get("readonly") == "true"

    def test_every_conditional_export_field_reserves_its_height(self):
        """Without it the window is sized for the default view and the taller
        conditional sections overflow."""
        fields = _config_fields()
        for name in ("bridgeWsPort", "bridgeMatterPort"):
            assert fields[name].get("visibleBindingId") == "showExportAdvanced"
            assert fields[name].get("alwaysUseInDialogHeightCalc") == "true"

    def test_the_port_prefs_are_the_keys_the_code_actually_reads(self):
        import bridge_agent
        fields = _config_fields()
        assert bridge_protocol.PREF_WS_PORT in fields
        assert bridge_agent.PREF_MATTER_PORT in fields
        assert fields[bridge_protocol.PREF_WS_PORT].get("defaultValue") \
            == bridge_protocol.DEFAULT_WS_PORT
        assert fields[bridge_agent.PREF_MATTER_PORT].get("defaultValue") \
            == bridge_agent.DEFAULT_MATTER_PORT


def test_the_pairing_page_is_a_hidden_iws_action(plugin_mod):
    for action in ET.parse(ACTIONS_XML).getroot().findall("Action"):
        if action.get("id") == "pairing":
            assert action.get("uiPath") == "hidden"
            assert action.findtext("CallbackMethod") == "http_pairing"
            assert hasattr(plugin_mod.Plugin, "http_pairing")
            return
    raise AssertionError("the pairing action is missing from Actions.xml")


# ---------------------------------------------------------------------------
# Pair
# ---------------------------------------------------------------------------
class TestPairMenu:
    def test_it_refuses_with_no_bridge_connection_and_says_why(self, plug):
        """The precondition is not obvious: the client exists only while
        something is exported (XG5), so pairing is genuinely unreachable until
        the user has exported a device. That is XAC2's ordering."""
        plug.pluginPrefs.clear()  # the allow-list lives in prefs — empty it properly
        plug.exports = ExportStore(lambda: plug.pluginPrefs, plug.logger)
        assert not len(plug.exports)
        plug.export_bridge = None
        ok, _values, errors = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is False and "duration" in errors
        assert "Export at least one device" in _logged(plug.logger)

    @pytest.mark.parametrize("bad", ["60", "1000", "abc", "-1"])
    def test_a_duration_outside_matters_band_is_refused_before_any_call(self, bad, plug):
        """⊗ Asserted on the CLIENT: clamping silently would give a user 900
        seconds when they asked for 60, which is a difference they should be told
        about while the dialog is still open."""
        client = _bridge_with(plug)
        ok, _values, errors = plug.menuPairMatterBridge({"duration": bad})
        assert ok is False and "duration" in errors
        client.open_commissioning_window.assert_not_called()
        client.get_pairing.assert_not_called()

    def test_a_blank_duration_uses_the_900_second_default(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(commissioned=True, window_open=False,
                                              manual=None, qr=None),
                                     bridge_protocol.CommissioningWindow(
                                         "1111-222-3333", "MT:XYZ",
                                         "2026-08-05T12:00:00Z")])
        ok, _values = plug.menuPairMatterBridge({"duration": ""})
        assert ok is True
        client.open_commissioning_window.assert_called_once_with(
            bridge_protocol.DEFAULT_WINDOW_SECONDS)

    def test_it_opens_a_window_and_logs_the_codes_the_expiry_and_the_page(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(commissioned=True, window_open=False,
                                              manual=None, qr=None),
                                     bridge_protocol.CommissioningWindow(
                                         "1111-222-3333", "MT:XYZ",
                                         "2026-08-05T12:00:00Z")])
        ok, _values = plug.menuPairMatterBridge({"duration": "300"})
        assert ok is True
        client.open_commissioning_window.assert_called_once_with(300)
        said = _logged(plug.logger)
        assert "1111-222-3333" in said and "MT:XYZ" in said
        assert "2026-08-05T12:00:00Z" in said
        assert "/message/com.simons-plugins.indigo-matter/pairing/" in said
        assert "Add Anyway" in said
        # The §5.5 readout has to know a window is open without polling.
        plug.export_bridge.note_window_opened.assert_called_once_with(
            "2026-08-05T12:00:00Z")

    def test_a_NEVER_commissioned_bridge_reports_its_existing_code(self, plug):
        """⊗ §3.7: an uncommissioned node already has its basic window open with
        the persisted originals. Deriving a fresh enhanced code there would
        invalidate a code the user may already be typing."""
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(commissioned=False, window_open=True,
                                              manual="0000-111-2222", expires=None)])
        ok, _values = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is True
        client.open_commissioning_window.assert_not_called()
        said = _logged(plug.logger)
        assert "0000-111-2222" in said
        assert "ALREADY advertising" in said

    def test_a_window_that_is_already_open_is_reported_not_re_opened(self, plug):
        """§3.8's assertClosed refuses a second one — and doing it anyway would
        kill the code the user is holding."""
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(commissioned=True, window_open=True,
                                              manual="9999-888-7777")])
        ok, _values = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is True
        client.open_commissioning_window.assert_not_called()
        assert "9999-888-7777" in _logged(plug.logger)

    def test_a_failed_get_pairing_opens_no_window(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime(error=RuntimeError("socket gone"))
        ok, _values, errors = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is False and "duration" in errors
        client.open_commissioning_window.assert_not_called()
        assert "No pairing window was opened" in _logged(plug.logger)

    def test_commissioning_window_failed_is_reported_as_a_failure(self, plug):
        """Never report success over a window that did not open: the user would
        stand in front of the Home app waiting for a code that does not exist."""
        _bridge_with(plug)

        class _Runtime(_FakeRuntime):
            def result(self, timeout=None):  # noqa: ARG002
                if not self.results:
                    raise bridge_protocol.BridgeProtocolError(
                        bridge_protocol.ERR_COMMISSIONING_WINDOW_FAILED, "already open")
                return self.results.pop(0)

        plug.runtime = _Runtime([_pairing(commissioned=True, window_open=False,
                                          manual=None, qr=None)])
        ok, _values, errors = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is False and "duration" in errors
        said = _logged(plug.logger)
        assert "existing pairings are untouched" in said


# ---------------------------------------------------------------------------
# Unpair
# ---------------------------------------------------------------------------
class TestFabricPicker:
    def test_it_lists_each_fabric_by_vendor_and_index(self, plug):
        _bridge_with(plug, fabrics=[_fabric(1), _fabric(2, vendor=0x1217)])
        rows = dict(plug.getBridgeFabrics())
        assert rows["1"] == "Apple Home (index 1)"
        assert rows["2"] == "Amazon Alexa (index 2)"

    def test_the_first_row_is_a_no_selection_row(self, plug):
        """⊗ Indigo pre-selects row one. Without this the destructive dialog
        opened with a real ecosystem already chosen and its Execute button one
        click away — and this was the ONE picker in the plugin without it."""
        _bridge_with(plug, fabrics=[_fabric(1), _fabric(2, vendor=0x1217)])
        rows = plug.getBridgeFabrics()
        assert rows[0][0] == plugin_module_no_selection_id()
        assert "select an ecosystem" in rows[0][1]

    def test_the_unpair_dialog_is_seeded_with_the_no_selection_row(self, plug):
        """A picker row is only half of it: an unseeded field still lands on the
        first row, and a seeded value with no matching row renders blank."""
        values = plug.get_menu_action_config_ui_values("unpairEcosystem")
        assert values["fabric"] == plugin_module_no_selection_id()

    def test_seeding_the_unpair_dialog_does_not_seed_other_menus(self, plug):
        """This callback fires for EVERY menu with a ConfigUI."""
        assert dict(plug.get_menu_action_config_ui_values("resetBridgePairings")) == {}

    def test_never_connected_and_not_paired_are_DIFFERENT_answers(self, plug):
        """Both are unpickable, but only one of them means "you are not paired"."""
        _bridge_with(plug, fabrics=None)
        assert "not connected" in dict(plug.getBridgeFabrics())["0"]
        _bridge_with(plug, fabrics=[])
        assert "no paired ecosystems" in dict(plug.getBridgeFabrics())["0"]

    def test_it_never_blocks_the_dialog_on_a_wS_round_trip(self, plug):
        """A list callback runs while the dialog opens; a node that is down would
        hang it rather than render an empty one."""
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        plug.getBridgeFabrics()
        client.get_status.assert_not_called()
        client.get_pairing.assert_not_called()

    def test_a_broken_picker_says_so_rather_than_rendering_empty(self, plug):
        plug.export_bridge = Mock()
        type(plug.export_bridge).fabrics = property(
            lambda _self: (_ for _ in ()).throw(RuntimeError("boom")))
        rows = dict(plug.getBridgeFabrics())
        assert "error building list" in list(rows.values())[0]


class TestFabricBackupPicker:
    def test_a_broken_backup_picker_says_so_rather_than_looking_empty(self, plug):
        """An empty picker and a broken picker used to render identically here,
        and "you have no backups" is the one answer a user ACTS on — by
        concluding there is nothing to restore and going looking for a fabric
        they in fact still have (issue #310)."""
        plug._resolve_storage_path = Mock(side_effect=RuntimeError("prefs unreadable"))
        rows = dict(plug.getFabricBackups())
        assert "error building list" in list(rows.values())[0]

    def test_a_failure_LISTING_the_backups_no_longer_takes_the_dialog_down(self, plug, monkeypatch):
        """The storage path resolving fine and the listing then failing used to
        escape this callback entirely — a raw traceback instead of a dialog.
        The decorator covers the whole body now, not just the path lookup."""
        import fabric_backup
        plug._resolve_storage_path = Mock(return_value="/tmp/does-not-matter")
        monkeypatch.setattr(fabric_backup, "list_backups",
                            Mock(side_effect=RuntimeError("unreadable directory")))
        rows = dict(plug.getFabricBackups())
        assert "error building list" in list(rows.values())[0]


class TestUnpairMenu:
    def test_neither_tick_removes_nothing(self, plug):
        """⊗ Asserted against THE CLIENT, with a live client present."""
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        ok, _values, errors = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": False, "confirmAgain": False})
        assert ok is False and "confirm" in errors
        client.remove_fabric.assert_not_called()

    def test_one_tick_is_not_enough(self, plug):
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        ok, _values, errors = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": True, "confirmAgain": False})
        assert ok is False and "confirmAgain" in errors
        client.remove_fabric.assert_not_called()

    def test_the_second_tick_alone_is_not_enough_either(self, plug):
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        ok, _values, errors = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": False, "confirmAgain": True})
        assert ok is False and "confirm" in errors
        client.remove_fabric.assert_not_called()

    def test_no_selection_removes_nothing(self, plug):
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        for value in ("", "0"):
            ok, _values, errors = plug.menuUnpairEcosystem(
                {"fabric": value, "confirm": True, "confirmAgain": True})
            assert ok is False and "fabric" in errors
        client.remove_fabric.assert_not_called()

    def test_both_ticks_call_remove_fabric_with_the_picked_index(self, plug):
        bridge, client = _unpair_bridge(plug, fabrics=[_fabric(1), _fabric(2, vendor=0x6006)],
                                        removal=_removal(True, 1))
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is True
        client.remove_fabric.assert_called_once_with(2)
        assert "has been unpaired" in _logged(plug.logger)
        # The picker's cache must not keep offering what was just removed.
        bridge.note_fabrics.assert_called_once()

    def test_removing_the_LAST_fabric_says_the_bridge_has_reset_itself(self, plug):
        """⊗ E5's hardened case. matter.js factory-resets when the fabric set
        empties, so the user has just reset the whole bridge without using the
        reset menu — reporting a routine removal would be a lie."""
        _bridge, client = _unpair_bridge(plug, fabrics=[_fabric(1)], removal=_removal(True, 0))
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": True, "confirmAgain": True})
        assert ok is True
        client.remove_fabric.assert_called_once_with(1)
        said = _logged(plug.logger)
        assert "LAST one paired" in said and "reset itself" in said

    def test_the_NODES_count_beats_the_stale_cache_for_last_fabric(self, plug):
        """⊗ The cache said one fabric was left; the node says none are.

        `_is_last_fabric` reads a list that can be a session old, and it selects
        between "routine removal" and "your bridge has just factory-reset
        itself". The node's own post-removal count is the answer when it has one.
        """
        _bridge, _client = _unpair_bridge(plug, fabrics=[_fabric(1), _fabric(2)],
                                          removal=_removal(True, 0))
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is True
        assert "LAST one paired" in _logged(plug.logger)

    def test_an_already_gone_fabric_is_NOT_reported_as_an_unpairing(self, plug):
        """⊗ The node-side no-op the menu reported as a completed removal.

        `remove_fabric` early-returns when the index holds nothing, and used to
        answer `{}` — the same frame a real removal produced — so the menu logged
        "has been unpaired. Every accessory Indigo exports has been removed from
        it" over an operation that did nothing at all. The picker is built from a
        CACHED fabric list, so this is the designed way to get here, not a typo.
        """
        _bridge, client = _unpair_bridge(plug, fabrics=[_fabric(1), _fabric(2)],
                                         removal=_removal(False, 2))
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is True
        client.remove_fabric.assert_called_once_with(2)
        said = _logged(plug.logger)
        assert "ALREADY gone" in said
        assert "has been unpaired" not in said
        assert "LAST one paired" not in said

    def test_a_node_that_cannot_count_falls_back_to_the_cache(self, plug):
        """`remaining` is null when the node could not read its own fabric set —
        legitimate mid-reset. A fabricated 0 there would claim a factory reset."""
        _bridge, _client = _unpair_bridge(plug, fabrics=[_fabric(1), _fabric(2)],
                                          removal=_removal(True, None))
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is True
        said = _logged(plug.logger)
        assert "has been unpaired" in said and "LAST one paired" not in said

    def test_a_failed_cache_refresh_does_not_fail_the_unpairing(self, plug):
        """The removal already happened; failing to re-read the list afterwards
        is not a reason to tell the user it did not."""
        bridge, _client = _unpair_bridge(plug, fabrics=[_fabric(1), _fabric(2)],
                                         removal=_removal(True, 1))
        bridge.note_fabrics.side_effect = RuntimeError("gone")
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is True
        assert "has been unpaired" in _logged(plug.logger)

    def test_a_failed_removal_says_pairings_are_unchanged(self, plug):
        _bridge_with(plug, fabrics=[_fabric(1), _fabric(2)])
        plug.runtime = _FakeRuntime(error=RuntimeError("node refused"))
        ok, _values, errors = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is False and "confirmAgain" in errors
        assert "Pairings are unchanged" in _logged(plug.logger)

    def test_it_refuses_without_a_bridge_connection(self, plug):
        plug.export_bridge = None
        ok, _values, errors = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": True, "confirmAgain": True})
        assert ok is False and "confirmAgain" in errors

    def test_a_DISCONNECTED_client_is_never_handed_remove_fabric(self, plug):
        """⊗ The fixture has carried `connected=False` since E6 and no test used
        it. `_pairing_client` checks it; nothing proved so, and this is the
        destructive one."""
        client = _bridge_with(plug, connected=False, fabrics=[_fabric(1)])
        ok, _values, errors = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": True, "confirmAgain": True})
        assert ok is False and "confirmAgain" in errors
        client.remove_fabric.assert_not_called()


class TestPairingClientGate:
    """⊗ `_pairing_client` refuses a client that exists but is not connected.

    Every pairing action goes through it, and a disconnected client accepts the
    call and then blocks the Indigo UI thread until the deadline. The fixture has
    carried `connected=False` since E6 without a single test using it.
    """

    def test_the_pair_menu_refuses_a_disconnected_client(self, plug):
        client = _bridge_with(plug, connected=False)
        ok, _values, errors = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is False and "duration" in errors
        client.get_pairing.assert_not_called()
        client.open_commissioning_window.assert_not_called()

    def test_it_names_the_real_precondition_when_nothing_is_exported(self, plug,
                                                                     plugin_mod):
        """XAC2's ordering: the client exists only while something is exported,
        so "pair the bridge" is genuinely unreachable until then."""
        _bridge_with(plug, connected=False)
        plug.exports = ExportStore(lambda: {}, plug.logger)
        plug._pairing_client(indigo_dict(plugin_mod), "duration")
        assert "Export at least one device" in _logged(plug.logger)

    def test_it_says_the_node_is_not_answering_when_something_IS_exported(self, plug,
                                                                          plugin_mod):
        _bridge_with(plug, connected=False)
        plug._pairing_client(indigo_dict(plugin_mod), "duration")
        assert "not answering" in _logged(plug.logger)


# ---------------------------------------------------------------------------
# The QR page
# ---------------------------------------------------------------------------
class TestPairingPage:
    def _action(self, method="GET"):
        return Mock(props={"incoming_request_method": method, "file_path": [],
                           "url_query_args": {}})

    def test_it_serves_html_carrying_both_codes(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(manual="1234-567-8901", qr="MT:Y.K90")])
        reply = plug.http_pairing(self._action())
        assert reply["status"] == 200
        assert reply["headers"]["Content-Type"].startswith("text/html")
        assert reply["headers"]["Cache-Control"] == "no-store"
        body = reply["content"]
        assert "1234-567-8901" in body and "MT:Y.K90" in body
        assert "<!DOCTYPE html>" in body

    def test_the_qr_viewer_link_url_encodes_the_payload(self, plug):
        """An `MT:` string is base-38 and can legitimately contain `+`, `/` or
        `%` — unencoded, the tool opens on a silently truncated payload, which is
        a QR that scans and means the wrong thing."""
        _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(qr="MT:A+B/C%D")])
        body = plug.http_pairing(self._action())["content"]
        assert "MT%3AA%2BB%2FC%25D" in body

    def test_it_says_so_when_no_window_is_open(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(window_open=False, manual=None, qr=None,
                                              fabrics=[_fabric(1)])])
        body = plug.http_pairing(self._action())["content"]
        assert "No pairing window is open" in body
        assert "Apple Home (index 1)" in body, "still say who is paired"

    def test_it_renders_a_page_even_with_no_bridge_at_all(self, plug):
        """A blank page over a bridge that is merely not running is
        indistinguishable from a broken handler."""
        plug.export_bridge = None
        body = plug.http_pairing(self._action())["content"]
        assert "not connected to the Matter bridge node" in body

    def test_a_DISCONNECTED_client_is_never_asked_for_a_page(self, plug):
        """⊗ The fixture has carried `connected=False` since E6 and no test used
        it. A disconnected client handed `get_pairing` blocks the IWS handler on
        a socket that is not there until the 15s deadline expires."""
        client = _bridge_with(plug, connected=False)
        body = plug.http_pairing(self._action())["content"]
        client.get_pairing.assert_not_called()
        assert "not connected to the Matter bridge node" in body

    def test_the_page_says_the_code_is_a_LIVE_credential(self, plug):
        """The page is served by IWS, which authenticates only if the user has
        switched authentication on — so while a window is open, anyone on the LAN
        who can reach this URL can commission the bridge onto their own fabric."""
        _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing()])
        body = plug.http_pairing(self._action())["content"]
        assert "live commissioning passcode" in body
        assert "authentication" in body

    def test_the_log_line_carries_the_same_warning(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime([_pairing(window_open=False, manual=None, qr=None),
                                     bridge_protocol.CommissioningWindow(
                                         manual_pairing_code="1234-567-8901",
                                         qr_pairing_code="MT:X",
                                         window_expires_at=_iso(minutes=+15))])
        ok, _values = plug.menuPairMatterBridge({"duration": "900"})
        assert ok is True
        said = _logged(plug.logger)
        assert "SECURITY" in said and "THEIR Apple Home" in said

    def test_a_failed_get_pairing_becomes_a_page_not_a_500(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime(error=RuntimeError("socket gone"))
        reply = plug.http_pairing(self._action())
        assert reply["status"] == 200
        assert "socket gone" in reply["content"]

    def test_only_GET_is_served(self, plug):
        _bridge_with(plug)
        reply = plug.http_pairing(self._action("POST"))
        assert reply["status"] == 405
        assert reply["headers"]["Cache-Control"] == "no-store"

    def test_the_page_escapes_what_it_renders(self, plugin_mod):
        """Everything on the page comes from the node or from an exception
        string; neither is trusted markup."""
        page = plugin_mod._pairing_html(None, "<script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


# ---------------------------------------------------------------------------
# E7: the agent seams and the bridge install action
# ---------------------------------------------------------------------------
class TestBridgeAgentWiring:
    def test_startup_hands_the_export_engine_all_three_seams(self, plugin_mod, plug,
                                                             monkeypatch):
        """⊗ The seams have to be PASSED, or the agent is never started at all —
        the exact "the helper is covered, the real caller is not" shape the E5
        round-two review named."""
        captured = {}

        def _fake_export_bridge(*_args, **kwargs):
            captured.update(kwargs)
            return Mock()

        monkeypatch.setattr(plugin_mod, "ExportBridge", _fake_export_bridge)
        monkeypatch.setattr(plugin_mod, "MatterClient", Mock())
        monkeypatch.setattr(plugin_mod, "CommissionJobs", Mock())
        monkeypatch.setattr(plugin_mod, "HttpApi", Mock())
        monkeypatch.setattr(plugin_mod, "AsyncRuntime", Mock())
        monkeypatch.setattr(plugin_mod, "ServerProcess", Mock())
        plug.debug = False
        plug._version = "2026.8.1"
        plug.proto = Mock()
        plug.registry = Mock()
        plug.device_sync = Mock()
        plug.server_process = None
        plug.matter = None
        plug._subscribed_to_devices = False
        plug._start_ts = 0.0
        plug.startup()
        for seam in ("agent_start", "agent_stop", "agent_diagnose"):
            assert captured.get(seam) is not None, f"{seam} was not passed"

    def test_a_fresh_start_builds_no_bridge_process(self, plug):
        """⊗ XAC1: constructing one is side-effect free, but building it eagerly
        is one ensure_installed away from a plist nobody asked for."""
        assert plug.bridge_process is None

    def test_the_diagnosis_before_any_agent_names_the_real_precondition(self, plug):
        assert "export at least one device" in plug._bridge_agent_diagnosis().lower()

    def test_the_diagnosis_reads_the_agents_own_error_log(self, plug):
        plug.bridge_process = _healthy_agent()
        plug.bridge_process.tail_error_log.return_value = "listen EADDRINUSE :::5540"
        assert "EADDRINUSE" in plug._bridge_agent_diagnosis()

    def test_the_diagnosis_asks_PREFLIGHT_before_guessing_at_the_log(self, plug):
        """⊗ It said "the package may not be installed (checked {project_dir})"
        having checked only the error log — and preflight() holds that exact
        fact. An uninstalled package is also the case where the log is empty for
        the right reason: launchd never got far enough to write one."""
        plug.bridge_process = _healthy_agent()
        plug.bridge_process.preflight.return_value = (
            "the indigo-matter-bridge package is not installed (…/dist/main.js is missing)")
        plug.bridge_process.tail_error_log.return_value = "some ancient unrelated crash"
        said = plug._bridge_agent_diagnosis()
        assert "not installed" in said
        assert "ancient" not in said, "the real cause must not be buried under a stale log tail"

    def test_the_diagnosis_never_restarts_anything(self, plug):
        """launchd owns respawn; a diagnostic that bounced the agent on every
        failure streak would turn a crash-loop into an unreadable one."""
        plug.bridge_process = _healthy_agent()
        plug.bridge_process.tail_error_log.return_value = "boom"
        plug._bridge_agent_diagnosis()
        plug.bridge_process.restart.assert_not_called()
        plug.bridge_process.ensure_installed.assert_not_called()
        plug.bridge_process.stop.assert_not_called()

    def test_the_log_tail_is_not_called_RECENT(self, plug):
        """The file is appended to and never truncated, so the last 20 lines can
        be a crash-loop from days ago that has since been fixed."""
        plug.bridge_process = _healthy_agent()
        plug.bridge_process.tail_error_log.return_value = "boom"
        said = plug._bridge_agent_diagnosis()
        assert "Recent" not in said
        assert "may be old" in said
        assert "bridge-node.err.log" in said, "name the file so timestamps can be checked"

    def test_an_empty_error_log_with_the_package_present_says_so(self, plug):
        """Preflight passed, so "may not be installed" is no longer a guess that
        is even available — it is installed. Point at the other cause."""
        plug.bridge_process = _healthy_agent()
        plug.bridge_process.tail_error_log.return_value = None
        said = plug._bridge_agent_diagnosis()
        assert "is installed" in said and "port" in said

    def test_stop_removes_the_plist_so_a_REBOOT_cannot_restart_it(self, plug, tmp_path):
        """⊗ XG5/XAC1 across a reboot.

        `stop()` kept the plist, and the plist carries `RunAtLoad: True` — so at
        the next login launchd started an unpaired bridge node with an EMPTY
        allow-list, advertising on the Matter port, that this plugin never
        started and (XAC1's latch) would never stop. The guarantee is "a fresh or
        emptied install runs no bridge process", and it has to survive a restart.
        """
        plist = tmp_path / "com.simons-plugins.indigo-matter.bridge.plist"
        plist.write_text("<plist/>")
        agent = Mock(plist_path=str(plist))
        agent.is_running.side_effect = [True, False]
        agent.uninstall.side_effect = lambda: plist.unlink()
        plug.bridge_process = agent
        plug._stop_bridge_agent()
        agent.uninstall.assert_called_once()
        assert not plist.exists(), "the plist must be gone, not merely booted out"
        assert "cannot bring it back" in _logged(plug.logger)

    def test_stop_resets_a_standing_conflict_so_nothing_later_calls_it_resolved(self, plug,
                                                                                tmp_path):
        """#187 review: with a conflict standing, uninstalling the agent must not
        leave `_last_bridge_port_conflict` around for the next port-conflict tick
        to see `port_conflict_report()` go quiet (no pid — we just uninstalled)
        and announce "resolved" — the rival, if any, still holds the port. This is
        the braces; Commit 1's pid-gate on that log line is the belt."""
        plist = tmp_path / "com.simons-plugins.indigo-matter.bridge.plist"
        plist.write_text("<plist/>")
        agent = Mock(plist_path=str(plist))
        agent.is_running.side_effect = [True, False]
        agent.uninstall.side_effect = lambda: plist.unlink()
        plug.bridge_process = agent
        plug._last_bridge_port_conflict = "port 5581 is held by pid 777"
        plug._stop_bridge_agent()
        assert plug._last_bridge_port_conflict is None

    def test_a_stop_that_did_not_work_is_LOUD(self, plug, tmp_path):
        """⊗ The silent branch. `stop()` returning False said nothing at all and
        `_agent_started` had already been cleared, so nothing retried: the node
        kept serving every paired ecosystem with the log asserting the opposite
        by omission."""
        plist = tmp_path / "bridge.plist"
        plist.write_text("<plist/>")
        agent = Mock(plist_path=str(plist))
        agent.is_running.return_value = True          # still loaded afterwards
        plug.bridge_process = agent
        plug._stop_bridge_agent()
        said = _logged(plug.logger)
        assert "could not be stopped" in said
        assert "NOTHING retries" in said
        assert "launchctl bootout" in said

    def test_stopping_when_nothing_was_loaded_is_not_reported_as_a_failure(self, plug,
                                                                          tmp_path):
        """The other False `stop()` conflated: "there was no such job"."""
        agent = Mock(plist_path=str(tmp_path / "absent.plist"))
        agent.is_running.return_value = False
        plug.bridge_process = agent
        plug._stop_bridge_agent()
        assert "could not be" not in _logged(plug.logger)

    def test_the_stop_menu_exists_and_clears_the_XAC1_latch(self, plug, plugin_mod,
                                                            monkeypatch, tmp_path):
        """A user who disables the plugin otherwise leaves the node running with
        no UI at all — the allow-list lever needs the plugin to be running."""
        agent = Mock(plist_path=str(tmp_path / "absent.plist"))
        agent.is_running.return_value = False
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess",
                            lambda *_a, **_k: agent)
        bridge = Mock()
        plug.export_bridge = bridge
        ok, _values = plug.menuStopBridgeNode({"confirm": True})
        assert ok is True
        agent.uninstall.assert_called_once()
        bridge.note_agent_stopped.assert_called_once()

    def test_the_stop_menu_needs_the_tick(self, plug, plugin_mod, monkeypatch):
        built = []
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess",
                            lambda *_a, **_k: built.append(1))
        ok, _values, errors = plug.menuStopBridgeNode({"confirm": False})
        assert ok is False and "confirm" in errors
        assert built == []

    def test_start_rebuilds_the_agent_from_CURRENT_prefs_EVERY_TIME(self, plug, plugin_mod,
                                                                    monkeypatch):
        """A snapshotted agent writes yesterday's ports while reporting success —
        the fault menuRestartMatterServer learned the hard way.

        Called TWICE with changed prefs in between: called once from a
        `bridge_process is None` fixture, this test could not tell "rebuilds on
        every call" from "builds once and caches", which is the thing it exists
        to pin.
        """
        built = []

        def _factory(prefs, _logger):
            built.append(dict(prefs))
            return _running_agent()

        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _factory)
        plug.pluginPrefs["bridgeMatterPort"] = "5541"
        plug._start_bridge_agent()
        assert built[-1]["bridgeMatterPort"] == "5541"
        # The agent now exists and is cached on the plugin — a build-once
        # implementation passes everything above and fails from here.
        assert plug.bridge_process is not None
        plug.pluginPrefs["bridgeMatterPort"] = "5542"
        plug._start_bridge_agent()
        assert len(built) == 2, "a cached agent would serve yesterday's ports"
        assert built[-1]["bridgeMatterPort"] == "5542"

    def test_start_says_nothing_reassuring_when_preflight_failed(self, plug, plugin_mod,
                                                                 monkeypatch):
        """ensure_installed() returning None means the plist was torn down and
        the reason logged. There is nothing running to announce."""
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess",
                            lambda *_a, **_k: Mock(ensure_installed=Mock(return_value=None)))
        plug._start_bridge_agent()
        assert "LaunchAgent is running" not in _logged(plug.logger)

    def test_start_does_not_claim_RUNNING_when_launchd_never_loaded_it(self, plug,
                                                                       plugin_mod,
                                                                       monkeypatch):
        """⊗ The wrong signal.

        `ensure_installed()` returns False for "already loaded and healthy" AND
        for "bootout worked, bootstrap and load both failed" — and this printed
        "bridge node LaunchAgent is running" for both, over a job launchd has
        never heard of.
        """
        agent = _running_agent(state="not_loaded")
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._start_bridge_agent()
        said = _logged(plug.logger)
        assert "LaunchAgent is running" not in said
        assert "could not be loaded" in said

    def test_start_does_not_claim_RUNNING_over_a_loaded_but_DEAD_job(self, plug, plugin_mod,
                                                                     monkeypatch):
        """The #104 fault-2 state: loaded, no pid, and launchd will not respawn it."""
        agent = _running_agent(state="loaded_not_running")
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._start_bridge_agent()
        said = _logged(plug.logger)
        assert "LaunchAgent is running" not in said
        assert "did not start" in said

    def test_start_reports_an_UNPARSEABLE_pid_as_neither_success_nor_failure(
            self, plug, plugin_mod, monkeypatch):
        """A pid line we could not read is not evidence of death; claiming the
        job failed would report a healthy bridge as stopped."""
        agent = _running_agent(state="unknown")
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._start_bridge_agent()
        said = _logged(plug.logger)
        assert "is loaded" in said and "readable pid" in said
        assert not plug.logger.error.call_args_list

    def test_start_announces_a_genuinely_running_agent(self, plug, plugin_mod, monkeypatch):
        agent = _running_agent()
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._start_bridge_agent()
        assert "LaunchAgent is running" in _logged(plug.logger)

    def test_start_carries_a_pending_verification_over_a_leave_alone_rebuild(
            self, plug, plugin_mod, monkeypatch, tmp_path):
        """#187 review: `_start_bridge_agent` rebuilds a fresh `BridgeProcess` on
        every call. If the rebuild's own `ensure_installed()` takes the
        digest-match 'leave alone' path (nothing actually changed), the replaced
        instance's still-pending #187 verification must survive onto the new one
        — or a second export inside the grace window silently drops it."""
        import bridge_agent as bridge_agent_mod
        from bridge_agent import BridgeProcess as RealBridgeProcess
        from test_server_process import FakeRunner

        home = tmp_path / "home"
        bindir = home / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / "npx").write_text("#!/bin/sh\n")
        (bindir / "node").write_text("#!/bin/sh\n")
        entry = (home / "indigo-matter" / "node_modules" / bridge_agent_mod.BRIDGE_PACKAGE
                 / "dist" / "main.js")
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake entry\n")

        def _factory(prefs, logger):
            # RealBridgeProcess, NOT bridge_agent_mod.BridgeProcess: the latter IS what
            # monkeypatch.setattr below replaces, and calling it here would recurse
            # into this very factory.
            return RealBridgeProcess(prefs, logger, home=str(home),
                                     npx_path=str(bindir / "npx"),
                                     runner=FakeRunner(), sleep=lambda *_a: None)

        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _factory)
        plug._start_bridge_agent()                  # first export: fresh bootstrap, armed
        first = plug.bridge_process
        assert first._bootstrap_verify_after is not None
        plug._start_bridge_agent()                  # second export, same prefs: leave-alone path
        second = plug.bridge_process
        assert second is not first
        assert second._bootstrap_verify_after == first._bootstrap_verify_after

    def test_start_lets_a_fresh_bootstrap_win_over_an_older_pending_verification(
            self, plug, plugin_mod, monkeypatch, tmp_path):
        """A rebuild whose OWN `ensure_installed()` bootstraps fresh must arm its
        OWN deadline — never be overwritten by whatever the replaced instance
        still had pending."""
        import bridge_agent as bridge_agent_mod
        from bridge_agent import BridgeProcess as RealBridgeProcess
        from test_server_process import FakeRunner

        home = tmp_path / "home"
        bindir = home / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        (bindir / "npx").write_text("#!/bin/sh\n")
        (bindir / "node").write_text("#!/bin/sh\n")
        entry = (home / "indigo-matter" / "node_modules" / bridge_agent_mod.BRIDGE_PACKAGE
                 / "dist" / "main.js")
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake entry\n")

        def _factory(prefs, logger):
            # RealBridgeProcess, NOT bridge_agent_mod.BridgeProcess: the latter IS what
            # monkeypatch.setattr below replaces, and calling it here would recurse
            # into this very factory.
            return RealBridgeProcess(prefs, logger, home=str(home),
                                     npx_path=str(bindir / "npx"),
                                     runner=FakeRunner(), sleep=lambda *_a: None)

        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _factory)
        plug._start_bridge_agent()
        first = plug.bridge_process
        first_deadline = first._bootstrap_verify_after
        assert first_deadline is not None
        plug.pluginPrefs["bridgeMatterPort"] = "5541"     # a real settings change
        plug._start_bridge_agent()                         # must bootstrap fresh, not leave-alone
        second = plug.bridge_process
        assert second._bootstrap_verify_after is not None
        assert second._bootstrap_verify_after != first_deadline

    def test_the_install_action_refuses_a_concurrent_npm_run(self, plug):
        """One npm root; two concurrent installs into it corrupt each other."""
        plug._install_thread = Mock(is_alive=Mock(return_value=True))
        plug.menuInstallBridgeNode()
        assert "already in progress" in _logged(plug.logger)

    def test_installing_with_nothing_exported_does_NOT_start_the_bridge(self, plug,
                                                                        plugin_mod,
                                                                        monkeypatch):
        """⊗ XG5. The difference from the controller's install: bringing the agent
        up because a package was updated would leave a bridge running for nothing."""
        agent = Mock(install=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.pluginPrefs.clear()
        plug.exports = ExportStore(lambda: plug.pluginPrefs, plug.logger)
        plug._install_bridge_node()
        agent.restart.assert_not_called()
        assert "nothing is exported yet" in _logged(plug.logger)

    def test_installing_with_an_export_restarts_onto_the_new_version(self, plug,
                                                                     plugin_mod,
                                                                     monkeypatch):
        """A running LaunchAgent does not pick up new files on disk.

        ``ensure_installed`` returning False is "the current definition was
        already loaded and left alone" — i.e. still running the OLD code — which
        is exactly when a restart is needed.
        """
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node()
        agent.restart.assert_called_once()
        assert "restarted onto the new version" in _logged(plug.logger)

    def test_installing_writes_the_plist_BEFORE_restarting(self, plug, plugin_mod,
                                                           monkeypatch, tmp_path):
        """⊗ The first-run dead end, against an agent that behaves like the real one.

        The bridge's plist is written by exactly ONE place — `_start_bridge_agent`
        — and on a machine where the package has never been installed that place
        cannot get past its own preflight, so it writes nothing and tears any
        stale plist down. The user's route out is this menu; it went install() →
        restart(), restart found no plist, and logged "nothing to restart. Fix
        the problem reported above" (the install had just SUCCEEDED) followed by
        "the restart FAILED — the old version may still be running" (nothing was
        running). The old test could not catch it: it asserted against a
        `Mock(restart=…True)`, which has a plist by virtue of being a Mock.
        """
        agent = _first_run_agent(tmp_path, plug.logger)
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        assert not os.path.exists(agent.plist_path), "the premise: a machine with no plist"
        plug._install_bridge_node()
        said = _logged(plug.logger)
        assert os.path.exists(agent.plist_path), \
            "nothing ever wrote the plist, so nothing can ever run"
        assert "nothing to restart" not in said
        assert "FAILED" not in said

    def test_installing_does_not_bounce_a_job_ensure_installed_just_bootstrapped(
            self, plug, plugin_mod, monkeypatch):
        """`ensure_installed` returning True means launchd has already loaded the
        NEW files. Restarting again is a second outage for nothing."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node()
        agent.restart.assert_not_called()

    def test_installing_says_so_when_the_plist_could_not_be_written(self, plug, plugin_mod,
                                                                    monkeypatch):
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=None))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node()
        agent.restart.assert_not_called()
        assert "LaunchAgent could not be written" in _logged(plug.logger)

    def test_the_clean_reinstall_menu_removes_the_package_first(self, plug, plugin_mod,
                                                               monkeypatch):
        """The bridge shipped without the exit the controller has had since
        2026.7, so a wedged install had a menu that reinstalled OVER the wedge."""
        order = []
        agent = Mock(
            remove_package=Mock(side_effect=lambda: (order.append("remove"), True)[1]),
            install=Mock(side_effect=lambda: (order.append("install"), True)[1]),
            ensure_installed=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node(clean=True)
        assert order == ["remove", "install"]

    def test_a_clean_reinstall_that_cannot_remove_does_NOT_install_over_it(
            self, plug, plugin_mod, monkeypatch):
        """⊗ `remove_package` used to log "Removed the … package" whatever
        happened, so a failed removal became a plain reinstall on top of the
        wedge the user came here to clear — reported as a clean one."""
        agent = Mock(remove_package=Mock(return_value=False), install=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node(clean=True)
        agent.install.assert_not_called()
        assert "ABANDONED" in _logged(plug.logger)

    def test_a_failed_install_changes_nothing(self, plug, plugin_mod, monkeypatch):
        agent = Mock(install=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node()
        agent.restart.assert_not_called()
        assert "Nothing was changed" in _logged(plug.logger)

    # -- issue #135: the post-install retry_now() poke ----------------------

    def test_the_RESTARTED_success_terminal_pokes_the_export_bridge(self, plug, plugin_mod,
                                                                    monkeypatch):
        """One of the two success terminals: applied is False and restart() wins."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock(revive_after_install=Mock(return_value=False))
        plug._install_bridge_node()
        plug.export_bridge.retry_now.assert_called_once()

    def test_the_FRESH_BOOTSTRAP_success_terminal_pokes_the_export_bridge(self, plug, plugin_mod,
                                                                         monkeypatch):
        """The other success terminal: applied is True, so restart() is never
        called (ensure_installed already bootstrapped launchd) — the poke must
        still fire, since it means the same thing to the user either way."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock(revive_after_install=Mock(return_value=False))
        plug._install_bridge_node()
        agent.restart.assert_not_called()
        plug.export_bridge.retry_now.assert_called_once()

    def test_a_poke_that_LANDED_says_reconnecting_now(self, plug, plugin_mod, monkeypatch):
        """The message is truthful, not assumed — accepted-poke half."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock(revive_after_install=Mock(return_value=False),
                                  retry_now=Mock(return_value=True))
        plug._install_bridge_node()
        said = _logged(plug.logger)
        assert "reconnecting now" in said
        assert "reload the plugin to reconnect" not in said

    def test_a_DECLINED_poke_says_reload_the_plugin_instead(self, plug, plugin_mod, monkeypatch):
        """⊗ A halted/closing client (or one that is simply absent) declines the
        poke; the old line claimed "reconnecting now" over a run loop that had
        exited (#154's shape). The message must follow the poke's answer."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock(revive_after_install=Mock(return_value=False),
                                  retry_now=Mock(return_value=False))
        plug._install_bridge_node()
        said = _logged(plug.logger)
        assert "reload the plugin to reconnect" in said
        assert "reconnecting now" not in said

    # -- issue #154: reviving a client halted on version skew ---------------

    def test_a_REVIVED_halt_says_so_and_never_reaches_the_poke(self, plug, plugin_mod,
                                                               monkeypatch):
        """Revival is tried FIRST. When it lands, the retry_now() poke — which a
        halted client would have declined anyway — must not even be attempted."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock(revive_after_install=Mock(return_value=True))
        plug._install_bridge_node()
        plug.export_bridge.retry_now.assert_not_called()
        said = _logged(plug.logger)
        assert "halted connection has been replaced" in said
        assert "reload the plugin to reconnect" not in said

    def test_a_DECLINED_revival_falls_through_to_the_poke(self, plug, plugin_mod, monkeypatch):
        """The ordinary #135 poke is still reached for every OTHER reason a
        client is not connected (never built, closing, or halted for a reason
        revival does not cover) — revival returning False must change nothing
        about that path."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock(revive_after_install=Mock(return_value=False),
                                  retry_now=Mock(return_value=True))
        plug._install_bridge_node()
        plug.export_bridge.revive_after_install.assert_called_once()
        plug.export_bridge.retry_now.assert_called_once()
        assert "reconnecting now" in _logged(plug.logger)

    def test_a_missing_export_bridge_is_not_a_crash(self, plug, plugin_mod, monkeypatch):
        """A session that has not built one yet (or ever) must not blow up here."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True),
                     ensure_installed=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        assert plug.export_bridge is None
        plug._install_bridge_node()  # must not raise

    def test_a_failed_install_does_not_poke(self, plug, plugin_mod, monkeypatch):
        agent = Mock(install=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock()
        plug._install_bridge_node()
        plug.export_bridge.retry_now.assert_not_called()

    def test_a_failed_restart_does_not_poke(self, plug, plugin_mod, monkeypatch):
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=False),
                     ensure_installed=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock()
        plug._install_bridge_node()
        plug.export_bridge.retry_now.assert_not_called()

    def test_the_nothing_exported_branch_does_not_poke(self, plug, plugin_mod, monkeypatch):
        """⊗ XG5: no client exists on this branch by design — the poke would be
        inert either way, but it must not even be reached from here."""
        agent = Mock(install=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.pluginPrefs.clear()
        plug.exports = ExportStore(lambda: plug.pluginPrefs, plug.logger)
        plug.export_bridge = Mock()
        plug._install_bridge_node()
        plug.export_bridge.retry_now.assert_not_called()

    def test_a_missing_plist_does_not_poke(self, plug, plugin_mod, monkeypatch):
        agent = Mock(install=Mock(return_value=True), ensure_installed=Mock(return_value=None))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug.export_bridge = Mock()
        plug._install_bridge_node()
        plug.export_bridge.retry_now.assert_not_called()


# ---------------------------------------------------------------------------
# The §5.5 readout
# ---------------------------------------------------------------------------
class TestConfigReadout:
    def test_it_is_seeded_into_the_dialog(self, plug):
        _bridge_with(plug, fabrics=[_fabric(1)])
        values, errors = plug.getPrefsConfigUiValues()
        assert values["exportReadout"]
        assert errors == {}

    def test_both_callback_spellings_agree(self, plug):
        """Indigo 2025.2 carries snake_case aliases alongside the camelCase
        ConfigUI callbacks, and which it dispatches on is undocumented."""
        _bridge_with(plug, fabrics=[_fabric(1)])
        assert plug.get_prefs_config_ui_values() == plug.getPrefsConfigUiValues()

    def test_it_names_the_paired_ecosystems_not_the_slot_count(self, plug):
        """PRD §5.5: matter.js allows 254 fabrics, so the count is never the
        interesting number — WHICH ecosystems hold one is."""
        _bridge_with(plug, fabrics=[_fabric(1), _fabric(2, vendor=0x1217)])
        readout = plug._export_readout()
        assert "Apple Home (index 1)" in readout and "Amazon Alexa (index 2)" in readout
        # The list is a cache the §5 events maintain, never a WS round trip on a
        # dialog open — so it must not be presented as this instant's truth.
        assert "last reported" in readout

    def test_it_reports_an_open_window(self, plug):
        soon = _iso(minutes=+10)
        _bridge_with(plug, fabrics=[_fabric(1)], window=soon)
        assert f"window is open until {soon}" in plug._export_readout()

    def test_a_window_whose_time_has_PASSED_is_not_reported_as_open(self, plug):
        """⊗ `window_expires_at` is cleared only by the §5 `window_closed` event,
        which the node does NOT send on shutdown — so a node (or plugin) restart
        left the timestamp standing and the readout claimed an open window
        indefinitely, including for a time hours in the past."""
        gone = _iso(minutes=-30)
        _bridge_with(plug, fabrics=[_fabric(1)], window=gone)
        readout = plug._export_readout()
        assert "is open until" not in readout
        assert f"expired at {gone}" in readout

    def test_an_unparseable_expiry_is_reported_rather_than_dropped(self, plug):
        _bridge_with(plug, fabrics=[_fabric(1)], window="whenever")
        assert "expiring whenever" in plug._export_readout()

    def test_a_CRASH_LOOPING_node_is_not_reported_as_paired(self, plug):
        """⊗ `active` means "a client OBJECT exists" and is set for the life of
        the engine, reconnect attempts included. Read as "the node is running" it
        told a user whose bridge had been down for a day "3 device(s) exported.
        Paired with: Apple Home" — the readout PRD §5.5 exists to prevent."""
        _bridge_with(plug, fabrics=[_fabric(1)], connected=False)
        readout = plug._export_readout()
        assert "NOT connected" in readout
        assert "Paired with" not in readout

    def test_connected_but_not_serving_says_so(self, plug):
        """§1.1: a node that is refusing answers the socket and serves nothing."""
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        client.attached = False
        assert "not serving accessories" in plug._export_readout()

    def test_it_reports_the_switch_being_off(self, plug):
        _bridge_with(plug, fabrics=[_fabric(1)], enabled=False)
        assert "switched off" in plug._export_readout()

    def test_it_distinguishes_not_running_from_not_yet_connected(self, plug):
        _bridge_with(plug, active=False)
        assert "bridge node is not running" in plug._export_readout()
        _bridge_with(plug, active=True, fabrics=None)
        assert "not yet connected" in plug._export_readout()

    def test_it_does_no_io(self, plug):
        """This runs while the dialog is opening."""
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        plug.getPrefsConfigUiValues()
        client.get_status.assert_not_called()
        client.get_pairing.assert_not_called()

    def test_it_survives_the_plugin_not_having_started(self, plug):
        plug.export_bridge = None
        assert "still starting" in plug._export_readout()


class TestSavingConfig:
    """⊗ ``closedPrefsConfigUi``'s ``exports_changed()`` re-run.

    Three lines with no local symptom, and the ONLY path that makes the "Enable
    Matter export" switch act without a plugin reload — which PluginConfig.xml
    promises the user in prose. Deleting them left the whole suite green.
    """

    def test_saving_config_applies_the_export_switch_immediately(self, plug):
        bridge = Mock()
        plug.export_bridge = bridge
        plug.closedPrefsConfigUi({"verboseLogging": False}, False)
        bridge.exports_changed.assert_called_once()

    def test_cancelling_changes_nothing(self, plug):
        bridge = Mock()
        plug.export_bridge = bridge
        plug.closedPrefsConfigUi({"verboseLogging": False}, True)
        bridge.exports_changed.assert_not_called()

    def test_a_failure_to_apply_it_gets_a_SENTENCE_not_a_bare_traceback(self, plug):
        """The user's mental model is "I clicked Save"; a stack trace under that
        reads as a crash in saving, which is the one thing that did work."""
        bridge = Mock()
        bridge.exports_changed.side_effect = RuntimeError("launchd said no")
        plug.export_bridge = bridge
        plug.closedPrefsConfigUi({"verboseLogging": False}, False)
        said = " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                        for c in plug.logger.error.call_args_list)
        assert "settings were saved" in said and "Reload the plugin" in said

    def test_the_no_engine_path_says_something(self, plug):
        """Switched off with no client: silence here reads as "applied"."""
        plug.export_bridge = None
        plug.closedPrefsConfigUi({"verboseLogging": False}, False)
        assert plug.logger.debug.called


class TestPairingPageUrl:
    def test_it_uses_the_INDIGO_web_server_url_not_localhost(self, plug, plugin_mod):
        """⊗ Deleting the `getWebServerURL()` call survived, even though the
        fixture stubs it to jarvis.local — and a localhost URL is useless on the
        phone the user is holding, which is the only device this page is for."""
        url = plug._pairing_page_url()
        plugin_mod.indigo.server.getWebServerURL.assert_called()
        assert url.startswith("http://jarvis.local:8176/")
        assert "localhost" not in url

    def test_a_failure_falls_back_to_loopback_rather_than_no_url(self, plug, plugin_mod):
        """A wrong-host URL a user can edit beats no URL at all."""
        plugin_mod.indigo.server.getWebServerURL.side_effect = RuntimeError("no reflector")
        try:
            assert plug._pairing_page_url().startswith("http://localhost:8176/")
        finally:
            plugin_mod.indigo.server.getWebServerURL.side_effect = None

    def test_it_points_at_this_plugins_pairing_handler(self, plug):
        assert plug._pairing_page_url().endswith(f"/message/{OURS}/pairing/")


class TestIwsPageUrl:
    """``HttpApiMixin._iws_page_url`` (#339) — the generalisation
    ``_pairing_page_url`` above now delegates to. Same three shapes, pinned
    the same way, plus that the ``action_id`` argument actually varies the
    handler the URL points at (``_pairing_page_url`` only ever asks for one)."""

    def test_it_uses_the_INDIGO_web_server_url_not_localhost(self, plug, plugin_mod):
        url = plug._iws_page_url("thread")
        plugin_mod.indigo.server.getWebServerURL.assert_called()
        assert url.startswith("http://jarvis.local:8176/")
        assert "localhost" not in url

    def test_a_failure_falls_back_to_loopback_rather_than_no_url(self, plug, plugin_mod):
        """A wrong-host URL a user can edit beats no URL at all."""
        plugin_mod.indigo.server.getWebServerURL.side_effect = RuntimeError("no reflector")
        try:
            assert plug._iws_page_url("thread").startswith("http://localhost:8176/")
        finally:
            plugin_mod.indigo.server.getWebServerURL.side_effect = None

    def test_it_points_at_the_requested_action(self, plug):
        assert plug._iws_page_url("thread").endswith(f"/message/{OURS}/thread/")
        assert plug._iws_page_url("pairing").endswith(f"/message/{OURS}/pairing/")

    def test_pairing_page_url_still_delegates_to_it(self, plug):
        """``_pairing_page_url`` is kept as a one-line delegate (#339) — its
        own callers/tests name it directly — but it must still resolve to
        exactly what ``_iws_page_url("pairing")`` would."""
        assert plug._pairing_page_url() == plug._iws_page_url("pairing")

    def test_a_trailing_slash_on_the_resolved_base_is_not_doubled(self, plug, plugin_mod):
        """#339 review, R6: a reflector/Bonjour URL ending in "/" used to leave
        the resolved page URL reading "…8176//message/…"."""
        plugin_mod.indigo.server.getWebServerURL.return_value = "http://jarvis.local:8176/"
        url = plug._iws_page_url("thread")
        assert "//message" not in url
        assert url == f"http://jarvis.local:8176/message/{OURS}/thread/"


class TestEscaping:
    def test_none_becomes_empty_not_the_word_None(self, plugin_mod):
        """⊗ The reason `_escape` is hand-rolled rather than `html.escape`, named
        in its own docstring and asserted by nothing: every value on the page
        comes from the node or an exception, and an absent one must not render
        the word "None" into a field the user is about to type into a phone."""
        assert plugin_mod._escape(None) == ""
        assert "None" not in plugin_mod._pairing_html(None, "")

    def test_it_escapes_the_five_characters_that_matter(self, plugin_mod):
        assert plugin_mod._escape('<a href="x">&</a>') == \
            "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;"


class TestUiThreadDeadlines:
    """⊗ Every `.result()` on the Indigo UI thread carries a timeout.

    Removing one is invisible: a node that accepts the socket and then stops
    answering hangs the dialog — and the Indigo client — with no way out but
    force-quitting it. Nothing about a missing `timeout=` is visible at the call
    site, so it is pinned here by value.
    """

    class _TimeoutSpy:
        def __init__(self, results):
            self.results = list(results)
            self.timeouts: list = []

        def submit(self, coro):
            coro.close()
            return self

        def result(self, timeout=None):
            self.timeouts.append(timeout)
            return self.results.pop(0) if self.results else None

    def test_get_pairing_and_open_window_are_bounded(self, plug, plugin_mod):
        _bridge_with(plug)
        spy = self._TimeoutSpy([_pairing(window_open=False, manual=None, qr=None),
                                bridge_protocol.CommissioningWindow(
                                    manual_pairing_code="1", qr_pairing_code="MT:1",
                                    window_expires_at=_iso(minutes=+15))])
        plug.runtime = spy
        plug.menuPairMatterBridge({"duration": "900"})
        assert spy.timeouts == [plugin_mod.PAIRING_READ_TIMEOUT,
                                plugin_mod.WINDOW_OPEN_TIMEOUT]

    def test_unpair_and_its_cache_refresh_are_bounded(self, plug, plugin_mod):
        _bridge_with(plug, fabrics=[_fabric(1), _fabric(2)])
        spy = self._TimeoutSpy([_removal(True, 1), _pairing()])
        plug.runtime = spy
        plug.menuUnpairEcosystem({"fabric": "2", "confirm": True, "confirmAgain": True})
        assert spy.timeouts == [plugin_mod.UNPAIR_TIMEOUT, plugin_mod.PAIRING_READ_TIMEOUT]

    def test_the_pairing_page_read_is_bounded(self, plug, plugin_mod):
        _bridge_with(plug)
        spy = self._TimeoutSpy([_pairing()])
        plug.runtime = spy
        plug.http_pairing(Mock(props={"incoming_request_method": "GET", "file_path": [],
                                      "url_query_args": {}}))
        assert spy.timeouts == [plugin_mod.PAIRING_READ_TIMEOUT]

    def test_every_deadline_is_a_real_positive_number(self, plugin_mod):
        for name in ("PAIRING_READ_TIMEOUT", "WINDOW_OPEN_TIMEOUT", "UNPAIR_TIMEOUT",
                     "FACTORY_RESET_TIMEOUT"):
            value = getattr(plugin_mod, name)
            assert isinstance(value, (int, float)) and value > 0, name
