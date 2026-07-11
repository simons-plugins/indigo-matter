"""Behavioural tests for plugin.py glue (previously only hasattr-checked).

Plugin is built via __new__ to bypass indigo.PluginBase.__init__, then its
collaborators are replaced with fakes. Covers the action-command timeout/error
bridge, the status body, request parsing, the IWS reply envelope, and the
decommission error mapping (404 vs 503 vs 500).
"""
from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from concurrent.futures import TimeoutError as FuturesTimeoutError
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def plug(plugin_mod):
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p._version = "2026.0.1"
    p._start_ts = 0.0
    p.runtime = None
    p.matter = None
    p.device_sync = Mock()
    p.http = Mock()
    p.jobs = None
    p.pluginPrefs = {}
    return p


class FakeFuture:
    def __init__(self, exc=None, value=None):
        self._exc = exc
        self._value = value

    def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._value


class FakeRuntime:
    def __init__(self, future):
        self._future = future

    def submit(self, coro):
        if hasattr(coro, "close"):
            coro.close()  # we won't run it; avoid "never awaited"
        return self._future


# ---------------------------------------------------------------------------
# Action → Matter command bridge (timeout / error → setErrorStateOnServer)
# ---------------------------------------------------------------------------
def _dev():
    d = SimpleNamespace(name="Plug")
    d.error = None
    d.setErrorStateOnServer = lambda v: setattr(d, "error", v)
    return d


def test_send_command_timeout_sets_timeout_error(plug):
    from protocol import MatterCommand
    plug.runtime = FakeRuntime(FakeFuture(exc=FuturesTimeoutError()))
    plug.matter = Mock()
    dev = _dev()
    plug._send_matter_command(MatterCommand(1, 1, 6, "On", {}), dev)
    assert dev.error == "timeout"
    plug.logger.error.assert_called()


def test_send_command_failure_sets_cmd_failed(plug):
    from protocol import MatterCommand
    plug.runtime = FakeRuntime(FakeFuture(exc=ValueError("boom")))
    plug.matter = Mock()
    dev = _dev()
    plug._send_matter_command(MatterCommand(1, 1, 6, "On", {}), dev)
    assert dev.error == "cmd failed"


def test_send_command_noop_when_runtime_missing(plug):
    from protocol import MatterCommand
    plug.runtime = None
    dev = _dev()
    plug._send_matter_command(MatterCommand(1, 1, 6, "On", {}), dev)  # must not raise
    assert dev.error is None


# ---------------------------------------------------------------------------
# Universal actions (Request Status / Energy Update / Energy Reset / Beep)
# ---------------------------------------------------------------------------
def _universal_plug(plug):
    plug.runtime = FakeRuntime(FakeFuture(value=None))
    plug.matter = Mock()
    return plug


@pytest.mark.parametrize("act", ["RequestStatus", "EnergyUpdate"])
def test_action_universal_status_and_update_refresh_node(plug, mock_indigo_base, act):
    _universal_plug(plug)
    dev = _dev()
    dev.pluginProps = {"nodeId": "5"}
    action = SimpleNamespace(deviceAction=getattr(mock_indigo_base.kUniversalAction, act))
    plug.actionControlUniversal(action, dev)
    plug.matter.interview_node.assert_called_once_with(5)


def test_action_universal_energy_reset_is_noop_with_notice(plug, mock_indigo_base):
    _universal_plug(plug)
    dev = _dev()
    dev.pluginProps = {"nodeId": "5"}
    action = SimpleNamespace(deviceAction=mock_indigo_base.kUniversalAction.EnergyReset)
    plug.actionControlUniversal(action, dev)
    plug.matter.interview_node.assert_not_called()
    plug.logger.info.assert_called()


def test_action_universal_refresh_noop_without_node_id(plug, mock_indigo_base):
    _universal_plug(plug)
    dev = _dev()
    dev.pluginProps = {}
    action = SimpleNamespace(deviceAction=mock_indigo_base.kUniversalAction.RequestStatus)
    plug.actionControlUniversal(action, dev)  # must not raise
    plug.matter.interview_node.assert_not_called()


def test_action_universal_refresh_timeout_logs_and_sets_error(plug, mock_indigo_base):
    plug.runtime = FakeRuntime(FakeFuture(exc=FuturesTimeoutError()))
    plug.matter = Mock()
    dev = _dev()
    dev.pluginProps = {"nodeId": "5"}
    action = SimpleNamespace(deviceAction=mock_indigo_base.kUniversalAction.RequestStatus)
    plug.actionControlUniversal(action, dev)
    plug.logger.error.assert_called()
    assert dev.error == "timeout"


def test_action_universal_refresh_failure_logs_and_sets_error(plug, mock_indigo_base):
    plug.runtime = FakeRuntime(FakeFuture(exc=ValueError("boom")))
    plug.matter = Mock()
    dev = _dev()
    dev.pluginProps = {"nodeId": "5"}
    action = SimpleNamespace(deviceAction=mock_indigo_base.kUniversalAction.EnergyUpdate)
    plug.actionControlUniversal(action, dev)
    plug.logger.error.assert_called()
    assert dev.error == "cmd failed"


def test_action_universal_noop_when_runtime_missing(plug, mock_indigo_base):
    plug.runtime = None
    plug.matter = Mock()
    dev = _dev()
    dev.pluginProps = {"nodeId": "5"}
    action = SimpleNamespace(deviceAction=mock_indigo_base.kUniversalAction.RequestStatus)
    plug.actionControlUniversal(action, dev)  # must not raise
    plug.matter.interview_node.assert_not_called()


def test_action_universal_unknown_action_ignored(plug, mock_indigo_base):
    _universal_plug(plug)
    dev = _dev()
    dev.pluginProps = {"nodeId": "5"}
    action = SimpleNamespace(deviceAction=mock_indigo_base.kUniversalAction.Beep)
    plug.actionControlUniversal(action, dev)
    plug.matter.interview_node.assert_not_called()


# ---------------------------------------------------------------------------
# Status body
# ---------------------------------------------------------------------------
def test_status_body_ready(plug):
    plug.matter = SimpleNamespace(
        connected=True,
        server_info={"sdk_version": "matter-server/0.6.2", "fabric_id": "0xAB"},
    )
    plug.device_sync.node_count = lambda: 3
    body = plug._status_body()
    assert body["ready"] is True
    assert body["matterServerVersion"] == "matter-server/0.6.2"
    assert body["fabricId"] == "0xAB"
    assert body["nodeCount"] == 3
    assert body["controllerVersion"] == "2026.0.1"
    assert "error" not in body and "message" not in body


def test_status_body_not_ready_when_disconnected(plug):
    plug.matter = SimpleNamespace(connected=False, server_info=None,
                                  uri="ws://localhost:5580/ws")
    plug.device_sync.node_count = lambda: 0
    body = plug._status_body()
    assert body["ready"] is False
    assert body["matterServerVersion"] == "unknown"
    # API.md §3.1: the 503 body carries the error envelope Domio reads
    assert body["error"] == "matter_server_unreachable"
    assert body["message"] == "Cannot reach matter-server at ws://localhost:5580/ws"


# ---------------------------------------------------------------------------
# Request parsing + reply envelope
# ---------------------------------------------------------------------------
def test_parse_request_extracts_method_path_query(plugin_mod):
    action = SimpleNamespace(props={
        "incoming_request_method": "POST",
        "file_path": ["job-1"],
        "url_query_args": {"setupCode": "123"},
        "body_params": {"suggestedName": "X"},
    })
    method, path_args, query = plugin_mod.Plugin._parse_request(action)
    assert method == "POST"
    assert path_args == ["job-1"]
    assert query == {"setupCode": "123", "suggestedName": "X"}


def test_reply_serialises_to_indigo_dict(plugin_mod):
    reply = plugin_mod.Plugin._reply(202, {"jobId": "abc"})
    assert reply["status"] == 202
    assert reply["headers"]["Content-Type"] == "application/json"
    assert json.loads(reply["content"]) == {"jobId": "abc"}


def test_http_status_handler_returns_reply(plug):
    plug.http.status = lambda: (200, {"ready": True})
    reply = plug.http_status(SimpleNamespace(props={}))
    assert reply["status"] == 200
    assert json.loads(reply["content"])["ready"] is True


# ---------------------------------------------------------------------------
# Decommission error mapping (the C3 fix)
# ---------------------------------------------------------------------------
def test_decommission_sync_timeout_raises_unavailable(plug):
    from http_handlers import MatterUnavailable
    plug.matter = Mock()  # past the not-connected guard, so the timeout path is what's tested
    plug.runtime = FakeRuntime(FakeFuture(exc=FuturesTimeoutError()))
    with pytest.raises(MatterUnavailable):
        plug._decommission_sync(42)


def test_decommission_sync_matter_none_raises_unavailable(plug):
    # runtime up but WS client not yet assigned (startup race): must 503, not
    # misread the AttributeError as "device offline" and delete devices anyway.
    from http_handlers import MatterUnavailable
    plug.matter = None
    plug.runtime = FakeRuntime(FakeFuture(value=None))
    with pytest.raises(MatterUnavailable):
        plug._decommission_sync(42)


def test_decommission_sync_no_runtime_raises_unavailable(plug):
    from http_handlers import MatterUnavailable
    plug.runtime = None
    with pytest.raises(MatterUnavailable):
        plug._decommission_sync(42)


def test_decommission_inner_offline_reports_fabric_not_removed(plug):
    async def scenario():
        async def boom(node_id):
            raise ConnectionError("offline")
        plug.matter = SimpleNamespace(remove_node=boom)
        plug.device_sync.delete_node = lambda nid: [111]
        result = await plug._decommission(42)
        assert result["fabricRemoved"] is False
        assert result["removedIndigoDeviceIds"] == [111]
        assert result["nodeId"] == "0x2A"
    asyncio.run(scenario())


def test_decommission_inner_unknown_unreachable_returns_none(plug):
    async def scenario():
        async def boom(node_id):
            raise ConnectionError("offline")
        plug.matter = SimpleNamespace(remove_node=boom)
        plug.device_sync.delete_node = lambda nid: []  # no devices either
        result = await plug._decommission(999)
        assert result is None  # → 404 unknown node
    asyncio.run(scenario())


def test_on_disconnected_marks_all_unreachable(plug):
    plug.device_sync = Mock()
    plug._on_disconnected()
    plug.device_sync.mark_all_unreachable.assert_called_once()


def test_resync_failure_does_not_reconcile(plug):
    # if get_nodes raises, reconcile_all must NOT run — so unreachable flags set
    # on disconnect are left intact rather than wrongly cleared.
    class BoomMatter:
        async def get_nodes(self):
            raise RuntimeError("ws down")

    plug.matter = BoomMatter()
    plug.device_sync = Mock()
    asyncio.run(plug._resync())
    plug.device_sync.reconcile_all.assert_not_called()


def test_startup_wires_connect_and_disconnect_callbacks(plugin_mod, monkeypatch):
    # a callback-swap (or dropping one) would silently break the whole M8 story;
    # pin that startup passes the right methods to MatterClient.
    captured = {}

    class FakeMatter:
        def __init__(self, proto, logger, prefs, **kw):
            captured.update(kw)

        def run(self):
            return None

    class FakeRuntimeObj:
        is_running = True

        def start(self):
            pass

        def submit(self, coro):
            if hasattr(coro, "close"):
                coro.close()
            return Mock()  # future-like (startup attaches a done-callback)

    monkeypatch.setattr(plugin_mod, "MatterClient", FakeMatter)
    monkeypatch.setattr(plugin_mod, "AsyncRuntime", lambda logger: FakeRuntimeObj())
    monkeypatch.setattr(plugin_mod, "CommissionJobs", lambda *a, **k: Mock())
    monkeypatch.setattr(plugin_mod, "HttpApi", lambda *a, **k: Mock())

    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginPrefs = {}
    p.proto = object()
    p.registry = object()
    p.device_sync = Mock()
    p.runtime = None
    p.server_process = None

    p.startup()
    assert captured["on_connect"] == p._resync
    assert captured["on_disconnect"] == p._on_disconnected
    assert captured["on_event"] == p._on_matter_event

# ---------------------------------------------------------------------------
# node_added → commission-job reconcile wiring (#16)
# ---------------------------------------------------------------------------
def test_node_added_event_triggers_job_reconcile(plug):
    import protocol
    plug.jobs = Mock()
    data = {"node_id": 7, "attributes": {}}
    evt = SimpleNamespace(kind=protocol.EVT_NODE_ADDED,
                          raw={"event": "node_added", "data": data})
    plug._on_matter_event(evt)
    plug.device_sync.handle_event.assert_called_once_with(evt)  # devices first
    plug.jobs.reconcile_node_added.assert_called_once_with(data)


def test_device_sync_failure_does_not_starve_reconcile(plug):
    # Stage isolation: handle_event blowing up must not stop the job table's
    # reconcile (which can still recover the job via its own create pass).
    import protocol
    plug.jobs = Mock()
    plug.device_sync.handle_event.side_effect = RuntimeError("device sync blew up")
    data = {"node_id": 7, "attributes": {}}
    evt = SimpleNamespace(kind=protocol.EVT_NODE_ADDED,
                          raw={"event": "node_added", "data": data})
    plug._on_matter_event(evt)  # must not raise
    plug.jobs.reconcile_node_added.assert_called_once_with(data)
    plug.logger.exception.assert_called_once()  # failure surfaced, not swallowed


def test_non_node_added_event_does_not_reconcile(plug):
    import protocol
    plug.jobs = Mock()
    evt = SimpleNamespace(kind=protocol.EVT_ATTRIBUTE_UPDATED, raw={})
    plug._on_matter_event(evt)
    plug.jobs.reconcile_node_added.assert_not_called()


def test_node_added_event_safe_without_jobs(plug):
    # startup not finished (jobs None) — the event path must not blow up
    import protocol
    evt = SimpleNamespace(kind=protocol.EVT_NODE_ADDED, raw={"data": {"node_id": 7}})
    plug._on_matter_event(evt)  # no AttributeError


# ---------------------------------------------------------------------------
# Fabric backup / restore menu handlers (#26)
# ---------------------------------------------------------------------------
def _storage_with_fabric(tmp_path):
    storage = tmp_path / "appsupport" / "matter-server"
    storage.mkdir(parents=True)
    (storage / "config").write_text("fabric")
    return str(storage)


def test_menu_export_fabric_backup_writes_real_zip(plug, plugin_mod, tmp_path, monkeypatch):
    import fabric_backup
    storage = _storage_with_fabric(tmp_path)
    plug.server_process = SimpleNamespace(storage_path=storage)
    plug.pluginPrefs = {}
    plug.menuExportFabricBackup()
    # a real archive landed in the sibling backups dir
    backups = fabric_backup.list_backups(storage)
    assert len(backups) == 1
    plug.logger.exception.assert_not_called()
    plug.logger.info.assert_called()


def test_menu_export_fabric_backup_logs_error_on_no_fabric(plug, tmp_path):
    # storage dir missing → create_backup raises FileNotFoundError → handler logs
    # an explicit "no fabric to back up" ERROR and never raises (H5).
    plug.server_process = SimpleNamespace(storage_path=str(tmp_path / "missing"))
    plug.pluginPrefs = {}
    plug.menuExportFabricBackup()  # must not raise
    plug.logger.error.assert_called()
    # the message must make clear nothing was written
    msg = " ".join(str(a) for c in plug.logger.error.call_args_list for a in c.args)
    assert "nothing was written" in msg.lower()


def test_get_fabric_backups_lists_for_picker(plug, plugin_mod, tmp_path):
    import fabric_backup
    storage = _storage_with_fabric(tmp_path)
    plug.server_process = SimpleNamespace(storage_path=storage)
    plug.pluginPrefs = {}
    from datetime import datetime, timezone
    archive = fabric_backup.create_backup(storage, now=datetime(2026, 6, 10, 12, 41, 45, tzinfo=timezone.utc))
    options = plug.getFabricBackups()
    assert len(options) == 1
    path, label = options[0]
    assert path == archive
    assert "fabric-20260610T124145Z.zip" in label
    assert "UTC" in label


def test_menu_restore_refuses_in_manual_mode(plug):
    plug.server_process = None
    ok, _vd, errors = plug.menuRestoreFabricBackup({"backup": "/x.zip", "confirm": True}, "restoreFabricBackup")
    assert ok is False
    assert "backup" in errors
    plug.logger.warning.assert_called()


def test_menu_restore_requires_confirmation(plug, tmp_path):
    plug.server_process = SimpleNamespace(storage_path=_storage_with_fabric(tmp_path))
    plug.pluginPrefs = {}
    ok, _vd, errors = plug.menuRestoreFabricBackup({"backup": "/x.zip", "confirm": False}, "restoreFabricBackup")
    assert ok is False
    assert "confirm" in errors


def test_menu_restore_requires_selection(plug, tmp_path):
    plug.server_process = SimpleNamespace(storage_path=_storage_with_fabric(tmp_path))
    plug.pluginPrefs = {}
    ok, _vd, errors = plug.menuRestoreFabricBackup({"backup": "", "confirm": True}, "restoreFabricBackup")
    assert ok is False
    assert "backup" in errors


def test_menu_restore_success_path(plug, plugin_mod, tmp_path):
    import fabric_backup
    from datetime import datetime, timezone
    storage = _storage_with_fabric(tmp_path)
    archive = fabric_backup.create_backup(storage, now=datetime(2026, 6, 10, 12, 41, 45, tzinfo=timezone.utc))

    calls = []
    plug.server_process = SimpleNamespace(
        storage_path=storage,
        stop=lambda: calls.append("stop") or True,
        start=lambda: calls.append("start") or True,
    )
    plug.pluginPrefs = {}

    result = plug.menuRestoreFabricBackup({"backup": archive, "confirm": True}, "restoreFabricBackup")
    ok, _vd = result
    assert ok is True
    assert calls == ["stop", "start"]
    plug.logger.exception.assert_not_called()
    # honest messaging: tells the user the server is restarting + the real signal.
    msg = " ".join(str(a) for c in plug.logger.info.call_args_list for a in c.args)
    assert "restarting" in msg.lower()
    assert "reconciled" in msg.lower()


def test_menu_restore_surfaces_error_dict_when_restore_fails(plug, tmp_path):
    import fabric_backup
    from datetime import datetime, timezone
    storage = _storage_with_fabric(tmp_path)
    (Path(storage) / "config").write_text("ORIGINAL-LIVE")
    archive = fabric_backup.create_backup(storage, now=datetime(2026, 6, 10, 12, 41, 45, tzinfo=timezone.utc))

    # start() returns False on the first (real) call → restore fails → rollback →
    # restore_backup raises → handler must surface an errorsDict, NOT report success.
    start_calls = {"n": 0}

    def fake_start():
        start_calls["n"] += 1
        return start_calls["n"] > 1  # first start False, rollback start True

    plug.server_process = SimpleNamespace(
        storage_path=storage,
        stop=lambda: True,
        start=fake_start,
    )
    plug.pluginPrefs = {}

    ok, _vd, errors = plug.menuRestoreFabricBackup({"backup": archive, "confirm": True}, "restoreFabricBackup")
    assert ok is False
    assert "backup" in errors  # UI shows a dialog
    plug.logger.error.assert_called()
    # original fabric was preserved (rolled back) — not wiped
    assert (Path(storage) / "config").read_text() == "ORIGINAL-LIVE"


# ---------------------------------------------------------------------------
# Decommission menu (getMatterNodes picker + menuDecommissionDevice)
# ---------------------------------------------------------------------------
def test_get_matter_nodes_empty_when_not_ready(plug):
    plug.device_sync = None
    assert plug.getMatterNodes() == []


def test_get_matter_nodes_labels_values_and_hex_node(plug):
    plug.device_sync.list_nodes = lambda: [(34, ["Study matter plug"]), (35, [])]
    options = plug.getMatterNodes()
    assert options[0] == ("34", "Study matter plug — node 0x22")
    assert options[1][0] == "35"
    assert "(no Indigo devices)" in options[1][1]


# ---------------------------------------------------------------------------
# Manual-commission folder picker (getDeviceFolders + menuCommissionDeviceManually)
# ---------------------------------------------------------------------------
def test_get_device_folders_lists_existing_plus_none(plug, mock_indigo_base):
    mock_indigo_base.devices.folders = [
        SimpleNamespace(id=1, name="Lights"),
        SimpleNamespace(id=2, name="Sensors"),
    ]
    options = plug.getDeviceFolders()
    # Leading no-folder sentinel is the non-empty id "0" — Indigo rejects "".
    assert options[0] == ("0", "(no folder)")
    assert ("1", "Lights") in options
    assert ("2", "Sensors") in options


def test_get_device_folders_ids_are_never_empty_strings(plug, mock_indigo_base):
    # Regression: an empty list id triggers "illegal ID string (ignoring)".
    mock_indigo_base.devices.folders = [SimpleNamespace(id=7, name="Garage")]
    assert all(key != "" for key, _label in plug.getDeviceFolders())


def test_get_device_folders_degrades_to_none_on_error(plug, mock_indigo_base):
    boom = Mock()
    boom.__iter__ = Mock(side_effect=RuntimeError("boom"))
    mock_indigo_base.devices.folders = boom
    assert plug.getDeviceFolders() == [("0", "(no folder)")]
    plug.logger.exception.assert_called()


def test_manual_commission_maps_folder_id_to_room_name(plug, mock_indigo_base):
    mock_indigo_base.devices.folders = [SimpleNamespace(id=5, name="Lights")]
    captured = {}
    plug.jobs = SimpleNamespace(create_job=lambda params: (captured.update(params) or (202, {})))
    ok, _vd = plug.menuCommissionDeviceManually(
        {"setupCode": "MT:ABC", "suggestedName": "Plug", "folder": "5"}, "commissionDeviceManually")
    assert ok is True
    assert captured["suggestedRoom"] == "Lights"
    assert captured["setupCode"] == "MT:ABC"


def test_manual_commission_no_folder_maps_to_none(plug, mock_indigo_base):
    mock_indigo_base.devices.folders = [SimpleNamespace(id=5, name="Lights")]
    captured = {}
    plug.jobs = SimpleNamespace(create_job=lambda params: (captured.update(params) or (202, {})))
    for sel in ("0", "", None):
        captured.clear()
        plug.menuCommissionDeviceManually(
            {"setupCode": "MT:ABC", "suggestedName": "Plug", "folder": sel}, "commissionDeviceManually")
        assert captured["suggestedRoom"] is None


def test_manual_commission_unknown_folder_id_falls_back_to_none(plug, mock_indigo_base):
    mock_indigo_base.devices.folders = [SimpleNamespace(id=5, name="Lights")]
    captured = {}
    plug.jobs = SimpleNamespace(create_job=lambda params: (captured.update(params) or (202, {})))
    plug.menuCommissionDeviceManually(
        {"setupCode": "MT:ABC", "suggestedName": "Plug", "folder": "999"}, "commissionDeviceManually")
    assert captured["suggestedRoom"] is None


def test_menu_decommission_requires_selection(plug):
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "", "confirm": True}, "decommissionDevice")
    assert ok is False
    assert "node" in errors


def test_menu_decommission_requires_confirm(plug):
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "34", "confirm": False}, "decommissionDevice")
    assert ok is False
    assert "confirm" in errors


def test_menu_decommission_rejects_non_numeric_selection(plug):
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "bogus", "confirm": True}, "decommissionDevice")
    assert ok is False
    assert "node" in errors


def test_menu_decommission_unavailable_maps_to_dialog_error(plug):
    plug.runtime = None  # _decommission_sync raises MatterUnavailable
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "34", "confirm": True}, "decommissionDevice")
    assert ok is False
    assert "node" in errors
    plug.logger.error.assert_called()


def test_menu_decommission_unknown_node_is_dialog_error(plug):
    plug.matter = Mock()
    plug.runtime = FakeRuntime(FakeFuture(value=None))
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "999", "confirm": True}, "decommissionDevice")
    assert ok is False
    assert "node" in errors


def test_menu_decommission_success_logs_and_closes(plug):
    plug.matter = Mock()
    plug.runtime = FakeRuntime(FakeFuture(value={
        "nodeId": "0x22", "removedIndigoDeviceIds": [678761951], "fabricRemoved": True}))
    ok, _vd = plug.menuDecommissionDevice({"node": "34", "confirm": True}, "decommissionDevice")
    assert ok is True
    plug.logger.info.assert_called()


def test_menu_decommission_passes_selected_node_id(plug):
    # Destructive action: a refactor that hands the wrong variable to
    # _decommission_sync would decommission the wrong physical device.
    plug._decommission_sync = Mock(return_value={
        "nodeId": "0x22", "removedIndigoDeviceIds": [1], "fabricRemoved": True})
    ok, _vd = plug.menuDecommissionDevice({"node": "34", "confirm": True}, "decommissionDevice")
    assert ok is True
    plug._decommission_sync.assert_called_once_with(34)  # int, not "34"


def test_menu_decommission_unexpected_error_is_dialog_error(plug):
    # _decommission_sync's contract: None → unknown, MatterUnavailable → 503,
    # OTHER → caller's problem. The menu must catch it, log it, and keep the
    # dialog open — not dump a raw traceback into Indigo's UI thread.
    plug.matter = Mock()
    plug.runtime = FakeRuntime(FakeFuture(exc=KeyError("index corrupted")))
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "34", "confirm": True}, "decommissionDevice")
    assert ok is False
    assert "node" in errors
    plug.logger.exception.assert_called()


def test_menu_decommission_offline_is_dialog_error_and_warns_resurrection(plug):
    # remove_node failed → fabric NOT removed → node still in matter-server →
    # reconcile will recreate the deleted Indigo devices. A half-done removal
    # must NOT close the dialog as success — the user would believe the device
    # is gone, then watch it reappear.
    plug.matter = Mock()
    plug.runtime = FakeRuntime(FakeFuture(value={
        "nodeId": "0x22", "removedIndigoDeviceIds": [678761951], "fabricRemoved": False}))
    ok, _vd, errors = plug.menuDecommissionDevice({"node": "34", "confirm": True}, "decommissionDevice")
    assert ok is False
    assert "reappear" in errors["node"]
    plug.logger.warning.assert_called()
    assert "reappear" in plug.logger.warning.call_args[0][0]


def test_get_matter_nodes_never_raises_into_dialog(plug):
    # A list-callback exception breaks the whole dialog with only a raw
    # traceback to explain it. Degrade to an empty picker + log instead
    # (same contract as getFabricBackups).
    plug.device_sync.list_nodes = Mock(side_effect=RuntimeError("boom"))
    assert plug.getMatterNodes() == []
    plug.logger.exception.assert_called()


def test_menu_commission_manually_not_ready_surfaces_dialog_error(plug):
    # jobs is None during startup: the dialog must say why OK did nothing.
    plug.jobs = None
    ok, _vd, errors = plug.menuCommissionDeviceManually({"setupCode": "MT:X"}, "commissionDeviceManually")
    assert ok is False
    assert "setupCode" in errors
    plug.logger.warning.assert_called()


# ---------------------------------------------------------------------------
# Fabric label (set_default_fabric_label on connect)
# ---------------------------------------------------------------------------
class LabelMatter:
    """Async matter fake for the _resync/_assert_fabric_label path."""

    def __init__(self, server_label):
        self.server_info = {"fabric_label": server_label}
        self.label_calls = []

    async def set_default_fabric_label(self, label):
        self.label_calls.append(label)

    async def get_nodes(self):
        return []


def test_fabric_label_set_when_server_differs(plug):
    # matter-server's default label is 'HomeAssistant' — vendor apps would list
    # the Indigo fabric as Home Assistant. Default pref must rename it to Indigo.
    plug.matter = LabelMatter("HomeAssistant")
    asyncio.run(plug._resync())
    assert plug.matter.label_calls == ["Indigo"]
    plug.device_sync.reconcile_all.assert_called_once()  # and reconcile still ran


def test_fabric_label_not_resent_when_already_correct(plug):
    # set_default_fabric_label pushes UpdateFabricLabel to every node — don't
    # spam per-device writes on every reconnect when nothing changed.
    plug.matter = LabelMatter("Indigo")
    asyncio.run(plug._resync())
    assert plug.matter.label_calls == []


def test_fabric_label_pref_respected_truncated_and_blank_falls_back(plug):
    plug.matter = LabelMatter("HomeAssistant")
    plug.pluginPrefs = {"fabricLabel": "  My Smart Castle Automation Server XL  "}
    asyncio.run(plug._resync())
    assert plug.matter.label_calls == ["My Smart Castle Automation Serve"]  # 32 chars
    plug.matter = LabelMatter("HomeAssistant")
    plug.pluginPrefs = {"fabricLabel": "   "}
    asyncio.run(plug._resync())
    assert plug.matter.label_calls == ["Indigo"]


def test_fabric_label_failure_does_not_block_reconcile(plug):
    # Cosmetic op on an older matter-server without the command: log at debug,
    # never block the reconcile that makes devices work.
    class OldMatter(LabelMatter):
        async def set_default_fabric_label(self, label):
            raise RuntimeError("unknown command")

    plug.matter = OldMatter("HomeAssistant")
    asyncio.run(plug._resync())
    plug.device_sync.reconcile_all.assert_called_once()
    plug.logger.debug.assert_called()


# ---------------------------------------------------------------------------
# Install/update matter-server menu action (pins node so run == install)
# ---------------------------------------------------------------------------

def test_install_handler_pins_node_and_reinstalls(plug, plugin_mod, monkeypatch):
    plug.server_process = SimpleNamespace(install=lambda: True,
                                          resolved_bin_dir="/opt/homebrew/bin")
    plug.pluginPrefs = {"serverLocation": "local"}
    plug._stopping = False
    plug._restart_expected_until = 0.0
    reinstalled = SimpleNamespace(ensure_installed=Mock(), restart=Mock())
    monkeypatch.setattr(plugin_mod, "ServerProcess", lambda *a, **k: reinstalled)
    plug._install_matter_server()
    # the node used for install is pinned so the LaunchAgent runs the same one
    assert plug.pluginPrefs["nodeBinDir"] == "/opt/homebrew/bin"
    assert plug.server_process is reinstalled
    reinstalled.ensure_installed.assert_called_once()
    reinstalled.restart.assert_called_once()                   # auto-restart onto new version
    assert plug._restart_expected_until > 0                    # crash-diagnostic suppressed
    plugin_mod.indigo.server.savePluginPrefs.assert_called()   # pin PERSISTS
    plug.logger.exception.assert_not_called()


def test_install_handler_aborts_and_logs_when_install_fails(plug, plugin_mod, monkeypatch):
    plug.server_process = SimpleNamespace(install=lambda: False, resolved_bin_dir="/x")
    plug.pluginPrefs = {"serverLocation": "local"}
    plug._stopping = False
    reinstalled = SimpleNamespace(ensure_installed=Mock())
    monkeypatch.setattr(plugin_mod, "ServerProcess", lambda *a, **k: reinstalled)
    plug._install_matter_server()
    assert "nodeBinDir" not in plug.pluginPrefs        # no pin on failure
    reinstalled.ensure_installed.assert_not_called()   # no reinstall on failure
    plug.logger.error.assert_called()                  # failure is surfaced, not silent


def test_install_handler_skips_state_mutation_when_stopping(plug, plugin_mod, monkeypatch):
    plug.server_process = SimpleNamespace(install=lambda: True, resolved_bin_dir="/x")
    plug.pluginPrefs = {"serverLocation": "local"}
    plug._stopping = True                              # plugin tearing down
    monkeypatch.setattr(plugin_mod, "ServerProcess",
                        lambda *a, **k: SimpleNamespace(ensure_installed=Mock()))
    plug._install_matter_server()
    assert "nodeBinDir" not in plug.pluginPrefs        # no pin/rewrite against teardown


def test_menu_install_spawns_daemon_thread_in_local_mode(plug, plugin_mod, monkeypatch):
    plug.pluginPrefs = {"serverLocation": "local"}
    plug._install_thread = None
    captured = {}

    class FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            captured.update(target=target, daemon=daemon)

        def start(self):
            captured["started"] = True

        def is_alive(self):
            return False

    monkeypatch.setattr(plugin_mod.threading, "Thread", FakeThread)
    plug.menuInstallMatterServer()
    assert captured.get("started") is True
    assert captured["daemon"] is True
    assert captured["target"] == plug._install_matter_server


def test_menu_install_refuses_when_already_running(plug):
    plug.pluginPrefs = {"serverLocation": "local"}
    plug._install_thread = SimpleNamespace(is_alive=lambda: True)
    plug.menuInstallMatterServer()
    plug.logger.warning.assert_called()               # "already in progress", no 2nd install


def test_menu_install_refuses_in_remote_mode(plug):
    plug.pluginPrefs = {"serverLocation": "remote"}
    plug._install_thread = None
    plug.menuInstallMatterServer()
    plug.logger.error.assert_called()


# --- restart-suppression window: defer (not drop) the crash diagnostic ---

def test_on_server_unreachable_suppresses_and_rearms_within_window(plug):
    plug.server_process = SimpleNamespace(tail_error_log=Mock(return_value="boom"), project_dir="/x")
    plug.matter = SimpleNamespace(rearm_failure_diagnostic=Mock())
    plug._restart_expected_until = 9e18          # far future = inside the window
    plug._restart_notice_shown = False
    plug._on_server_unreachable(2)
    plug.logger.error.assert_not_called()                        # not reported as a crash
    plug.server_process.tail_error_log.assert_not_called()
    plug.matter.rearm_failure_diagnostic.assert_called_once()    # DEFERRED, not dropped


def test_on_server_unreachable_surfaces_crash_outside_window(plug):
    plug.server_process = SimpleNamespace(tail_error_log=Mock(return_value="stack trace"), project_dir="/x")
    plug.matter = SimpleNamespace(rearm_failure_diagnostic=Mock())
    plug._restart_expected_until = 0.0           # window closed
    plug._restart_notice_shown = False
    plug._on_server_unreachable(2)
    plug.logger.error.assert_called()                            # real crash surfaced
    plug.matter.rearm_failure_diagnostic.assert_not_called()


def test_on_server_unreachable_no_server_process_is_noop(plug):
    plug.server_process = None
    plug._on_server_unreachable(2)               # must not raise
    plug.logger.error.assert_not_called()


def test_install_handler_logs_when_restart_fails(plug, plugin_mod, monkeypatch):
    plug.server_process = SimpleNamespace(install=lambda: True, resolved_bin_dir="/x")
    plug.pluginPrefs = {"serverLocation": "local"}
    plug._stopping = False
    plug._restart_expected_until = 0.0
    plug._restart_notice_shown = False
    reinstalled = SimpleNamespace(ensure_installed=Mock(), restart=Mock(return_value=False))
    monkeypatch.setattr(plugin_mod, "ServerProcess", lambda *a, **k: reinstalled)
    plug._install_matter_server()
    plug.logger.error.assert_called()            # restart failure surfaced, not silent success
    assert plug._restart_expected_until == 0.0   # window cleared so the crash diagnostic works


def test_menu_restart_sets_window_on_success(plug):
    plug.server_process = SimpleNamespace(restart=Mock(return_value=True))
    plug._restart_expected_until = 0.0
    plug._restart_notice_shown = True
    plug.menuRestartMatterServer()
    assert plug._restart_expected_until > 0       # outage treated as expected
    plug.server_process.restart.assert_called_once()


def test_menu_restart_clears_window_on_failure(plug):
    plug.server_process = SimpleNamespace(restart=Mock(return_value=False))
    plug._restart_expected_until = 0.0
    plug._restart_notice_shown = False
    plug.menuRestartMatterServer()
    assert plug._restart_expected_until == 0.0    # failed restart must not suppress diagnostics
    plug.logger.error.assert_called()
