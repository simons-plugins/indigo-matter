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
import xml.etree.ElementTree as ET
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
        assert rows["1"] == "Apple (index 1)"
        assert rows["2"] == "Amazon (index 2)"

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
        client = _bridge_with(plug, fabrics=[_fabric(1), _fabric(2, vendor=0x100B)])
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "2", "confirm": True, "confirmAgain": True})
        assert ok is True
        client.remove_fabric.assert_called_once_with(2)
        assert "has been unpaired" in _logged(plug.logger)

    def test_removing_the_LAST_fabric_says_the_bridge_has_reset_itself(self, plug):
        """⊗ E5's hardened case. matter.js factory-resets when the fabric set
        empties, so the user has just reset the whole bridge without using the
        reset menu — reporting a routine removal would be a lie."""
        client = _bridge_with(plug, fabrics=[_fabric(1)])
        ok, _values = plug.menuUnpairEcosystem(
            {"fabric": "1", "confirm": True, "confirmAgain": True})
        assert ok is True
        client.remove_fabric.assert_called_once_with(1)
        said = _logged(plug.logger)
        assert "LAST one paired" in said and "reset itself" in said

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
        assert "Apple (index 1)" in body, "still say who is paired"

    def test_it_renders_a_page_even_with_no_bridge_at_all(self, plug):
        """A blank page over a bridge that is merely not running is
        indistinguishable from a broken handler."""
        plug.export_bridge = None
        body = plug.http_pairing(self._action())["content"]
        assert "not connected to the Matter bridge node" in body

    def test_a_failed_get_pairing_becomes_a_page_not_a_500(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime(error=RuntimeError("socket gone"))
        reply = plug.http_pairing(self._action())
        assert reply["status"] == 200
        assert "socket gone" in reply["content"]

    def test_only_GET_is_served(self, plug):
        _bridge_with(plug)
        assert plug.http_pairing(self._action("POST"))["status"] == 405

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
        plug.bridge_process = Mock()
        plug.bridge_process.tail_error_log.return_value = "listen EADDRINUSE :::5540"
        assert "EADDRINUSE" in plug._bridge_agent_diagnosis()

    def test_the_diagnosis_never_restarts_anything(self, plug):
        """launchd owns respawn; a diagnostic that bounced the agent on every
        failure streak would turn a crash-loop into an unreadable one."""
        plug.bridge_process = Mock()
        plug.bridge_process.tail_error_log.return_value = "boom"
        plug._bridge_agent_diagnosis()
        plug.bridge_process.restart.assert_not_called()
        plug.bridge_process.ensure_installed.assert_not_called()
        plug.bridge_process.stop.assert_not_called()

    def test_an_empty_error_log_points_at_the_bridge_install_menu(self, plug):
        plug.bridge_process = Mock()
        plug.bridge_process.tail_error_log.return_value = None
        plug.bridge_process.project_dir = "/Users/x/indigo-matter"
        said = plug._bridge_agent_diagnosis()
        assert "indigo-matter-bridge" in said and "export bridge" in said

    def test_stop_is_a_stop_not_an_uninstall(self, plug):
        """The plist is cheap to keep and re-exporting should not re-derive it;
        the storage dir is never touched by either."""
        plug.bridge_process = Mock()
        plug.bridge_process.stop.return_value = True
        plug._stop_bridge_agent()
        plug.bridge_process.stop.assert_called_once()
        plug.bridge_process.uninstall.assert_not_called()

    def test_start_rebuilds_the_agent_from_CURRENT_prefs(self, plug, plugin_mod,
                                                         monkeypatch):
        """A snapshotted agent writes yesterday's ports while reporting success —
        the fault menuRestartMatterServer learned the hard way."""
        built = []

        def _factory(prefs, _logger):
            built.append(dict(prefs))
            return Mock(ensure_installed=Mock(return_value=True), ws_port="5581",
                        matter_port="5540")

        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", _factory)
        plug.pluginPrefs["bridgeMatterPort"] = "5541"
        plug._start_bridge_agent()
        assert built[-1]["bridgeMatterPort"] == "5541"

    def test_start_says_nothing_reassuring_when_preflight_failed(self, plug, plugin_mod,
                                                                 monkeypatch):
        """ensure_installed() returning None means the plist was torn down and
        the reason logged. There is nothing running to announce."""
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess",
                            lambda *_a, **_k: Mock(ensure_installed=Mock(return_value=None)))
        plug._start_bridge_agent()
        assert "LaunchAgent is running" not in _logged(plug.logger)

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
        """A running LaunchAgent does not pick up new files on disk."""
        agent = Mock(install=Mock(return_value=True), restart=Mock(return_value=True))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node()
        agent.restart.assert_called_once()
        assert "restarted onto the new version" in _logged(plug.logger)

    def test_a_failed_install_changes_nothing(self, plug, plugin_mod, monkeypatch):
        agent = Mock(install=Mock(return_value=False))
        monkeypatch.setattr(plugin_mod.bridge_agent, "BridgeProcess", lambda *_a, **_k: agent)
        plug._install_bridge_node()
        agent.restart.assert_not_called()
        assert "Nothing was changed" in _logged(plug.logger)


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
        assert "Apple (index 1)" in readout and "Amazon (index 2)" in readout

    def test_it_reports_an_open_window(self, plug):
        _bridge_with(plug, fabrics=[_fabric(1)], window="2026-08-05T12:00:00Z")
        assert "window is open until 2026-08-05T12:00:00Z" in plug._export_readout()

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
