"""Tests for the Thread mesh IWS page — #334.

Two halves, mirroring ``test_pairing_menu.py``'s split for the pairing page:

* :mod:`thread_page` itself — pure rendering against the real fixture
  (``tests/fixtures/thread_mesh/nodes.json``, the same 7 nodes
  ``test_thread_mesh.py``/``test_thread_survey.py`` use).
* ``http_thread_page`` — the IWS glue in :mod:`http_api_mixin`: GET-only,
  ``?live=1`` reaching ``thread_survey.run_survey`` with ``live_sleepy=True``,
  and a matter-server failure rendering as the page's own banner rather than a
  500 or a silently empty map.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from thread_mesh import build_mesh, parse_node_diag
from thread_page import render_thread_page
from thread_survey import Survey

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "thread_mesh" / "nodes.json"
PLUGIN_ID = "com.simons-plugins.indigo-matter"


def _raw_nodes() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _fixture_diags() -> list:
    diags = []
    for raw in _raw_nodes():
        attrs = raw["attributes"]
        name = attrs.get("0/40/3") or attrs.get("0/40/1") or ""
        diag = parse_node_diag(raw["node_id"], name, raw["available"], attrs)
        assert diag is not None
        diags.append(diag)
    return diags


def _fixture_mesh():
    diags = _fixture_diags()
    return build_mesh(diags), diags


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------

def test_it_renders_every_owned_node_name():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="2026-09-01T12:00:00Z",
                              live=False, plugin_id=PLUGIN_ID)
    for diag in diags:
        assert diag.name in html


def test_it_names_the_foreign_leader_as_the_probable_border_router():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "Router 14" in html
    assert "probably your border router" in html


def test_it_carries_the_0x34_single_neighbour_flag():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "only one neighbour" in html


def test_it_carries_the_0x2E_weak_link_flag():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "BILRESA scroll wheel" in html
    assert "-95 dBm" in html


def test_a_hostile_node_name_is_escaped():
    attrs = {
        "0/53/1": 2,  # SleepyEndDevice
        "0/53/2": "net", "0/53/3": 1, "0/53/4": 1,
        "0/53/7": [{"0": 1, "1": 5, "2": 0x2800, "5": 3, "6": -60, "7": -60,
                    "8": 0, "9": 0, "10": True, "11": True, "13": False}],
    }
    diag = parse_node_diag(1, "<script>alert(1)</script>", True, attrs)
    mesh = build_mesh([diag])
    html = render_thread_page(mesh, [diag], generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_unreadable_section_appears_when_a_diag_has_a_read_error():
    mesh, diags = _fixture_mesh()
    diags[0].read_error = "live read timed out after 5 s"
    mesh = build_mesh(diags)
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "Unreadable / stale" in html
    assert "live read timed out after 5 s" in html
    assert diags[0].name in html


def test_partition_disagreements_appear_in_the_unreadable_section():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "Unreadable / stale" in html
    assert "reports partition" in html


def test_no_unreadable_section_when_nothing_is_wrong():
    attrs = {
        "0/53/1": 5,  # Router
        "0/53/2": "net", "0/53/3": 1, "0/53/4": 1,
        "0/53/8": [{"0": 1, "1": 0x2800, "2": 10, "3": 63, "4": 0, "5": 0, "6": 0,
                    "7": 0, "8": True, "9": False}],
        "0/53/7": [{"0": 2, "1": 5, "2": 0x2C00, "5": 3, "6": -50, "7": -50,
                    "8": 0, "9": 0, "10": True, "11": True, "13": False}],
    }
    diag = parse_node_diag(9, "healthy router", True, attrs)
    mesh = build_mesh([diag])
    html = render_thread_page(mesh, [diag], generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "Unreadable / stale" not in html


def test_live_true_shows_the_live_badge_not_cached():
    # The CSS block always defines BOTH `.badge-live`/`.badge-cached` rules, so
    # the check has to be on the applied class attribute, not on either
    # substring appearing anywhere in the document.
    mesh, diags = _fixture_mesh()
    html_live = render_thread_page(mesh, diags, generated_at="t", live=True, plugin_id=PLUGIN_ID)
    html_cached = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert 'class="badge badge-live"' in html_live
    assert 'class="badge badge-cached"' not in html_live
    assert 'class="badge badge-cached"' in html_cached
    assert 'class="badge badge-live"' not in html_cached


def test_error_renders_the_banner_and_no_map():
    mesh = build_mesh([])
    html = render_thread_page(mesh, [], generated_at="t", live=False, plugin_id=PLUGIN_ID,
                              error="matter-server could not be read: boom")
    assert "Could not read the Thread mesh" in html
    assert "boom" in html
    assert "<svg" not in html


def test_refresh_links_point_at_the_plugin_scoped_url():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert f"/message/{PLUGIN_ID}/thread?live=1" in html
    assert f'"/message/{PLUGIN_ID}/thread"' in html


def test_it_scales_with_a_viewbox_not_a_fixed_size():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "viewBox=" in html
    assert 'style="width:100%' in html


# ---------------------------------------------------------------------------
# HTTP glue — http_thread_page (mirrors TestPairingPage in test_pairing_menu.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def plugin_mod(mock_indigo_base):
    mock_indigo_base.server.getWebServerURL.return_value = "http://jarvis.local:8176"
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def plug(plugin_mod):
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = PLUGIN_ID
    p.runtime = Mock()
    p.matter = Mock(connected=True)
    p.device_sync = Mock()
    p.device_sync.list_nodes.return_value = []
    p.export_bridge = None
    return p


def _action(method="GET", query=None):
    return Mock(props={"incoming_request_method": method, "file_path": [],
                       "url_query_args": query or {}})


def test_it_serves_html_200(plug, monkeypatch):
    # NOTE: minimal compatibility patch for #334 post-review Step 2
    # (thread_survey.run_survey now returns a Survey, not a bare list — B2.4).
    mesh, diags = _fixture_mesh()
    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey",
                        lambda *a, **k: Survey(diags=diags, raw_count=len(diags), skipped=[]))
    reply = plug.http_thread_page(_action())
    assert reply["status"] == 200
    assert reply["headers"]["Content-Type"].startswith("text/html")
    assert "<!DOCTYPE html>" in reply["content"]
    assert "GRILLPLATS Plug" in reply["content"]


def test_only_GET_is_served(plug):
    assert plug.http_thread_page(_action("POST"))["status"] == 405


def test_live_1_calls_run_survey_with_live_sleepy_true(plug, monkeypatch):
    calls = []

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        calls.append(live_sleepy)
        return []

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action(query={"live": "1"}))
    assert reply["status"] == 200
    assert calls == [True]
    assert 'class="badge badge-live"' in reply["content"]


def test_no_live_query_calls_run_survey_with_live_sleepy_false(plug, monkeypatch):
    calls = []

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        calls.append(live_sleepy)
        return []

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    plug.http_thread_page(_action())
    assert calls == [False]


def test_matter_not_connected_renders_the_banner_not_a_500(plug):
    plug.matter = Mock(connected=False)
    reply = plug.http_thread_page(_action())
    assert reply["status"] == 200
    assert "not connected to matter-server" in reply["content"]
    assert "<svg" not in reply["content"]


def test_get_nodes_failure_renders_the_banner_not_an_empty_mesh(plug, monkeypatch):
    def _raise(*a, **k):
        raise ConnectionError("matter-server down")

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _raise)
    reply = plug.http_thread_page(_action())
    assert reply["status"] == 200
    assert "Could not read the Thread mesh" in reply["content"]
    assert "matter-server down" in reply["content"]
    assert "<svg" not in reply["content"]
