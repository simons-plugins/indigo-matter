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
    plug.runtime = FakeRuntime(FakeFuture(exc=FuturesTimeoutError()))
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
