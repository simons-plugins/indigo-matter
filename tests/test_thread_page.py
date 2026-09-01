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
    # #334 post-review, B3.11: the fake used to return [] — which meant this
    # test could never actually catch a broken ?live=1 rendering path (the
    # page would just show "no Thread devices" either way). Real diags make
    # ?live=1 render through the whole glue.
    calls = []
    diags = _fixture_diags()

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        calls.append(live_sleepy)
        return Survey(diags=diags, raw_count=len(diags), skipped=[])

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action(query={"live": "1"}))
    assert reply["status"] == 200
    assert calls == [True]
    assert 'class="badge badge-live"' in reply["content"]
    assert "GRILLPLATS Plug" in reply["content"]


def test_no_live_query_calls_run_survey_with_live_sleepy_false(plug, monkeypatch):
    calls = []
    diags = _fixture_diags()

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        calls.append(live_sleepy)
        return Survey(diags=diags, raw_count=len(diags), skipped=[])

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action())
    assert calls == [False]
    assert "GRILLPLATS Plug" in reply["content"]


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


# ---------------------------------------------------------------------------
# #334 post-review — A4: layout (no-overlap, same-layer arcs, viewBox growth)
# ---------------------------------------------------------------------------

def test_layout_boxes_never_overlap():
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    boxes = _layout(mesh)
    assert len(boxes) == len(mesh.nodes)  # every node placed, none dropped
    items = list(boxes.items())
    for i, (key_a, (xa, ya, wa, ha)) in enumerate(items):
        for key_b, (xb, yb, wb, hb) in items[i + 1:]:
            overlap_x = xa < xb + wb and xb < xa + wa
            overlap_y = ya < yb + hb and yb < ya + ha
            assert not (overlap_x and overlap_y), f"{key_a} overlaps {key_b}"


def test_a_detached_sed_still_gets_a_box():
    # #334 post-review, B3.10: a node with no parent link at all (a detached
    # SED, B5.1's own no_parent case) must not silently vanish from the
    # layout because it has nowhere obvious to be placed under.
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    attrs = {"0/53/1": 2}  # SED, no NeighborTable at all -> no parent
    diag = parse_node_diag(99, "detached sed", True, attrs)
    mesh = build_mesh([diag])
    boxes = _layout(mesh)
    assert mesh.nodes[0].key in boxes


def test_same_layer_link_is_drawn_as_an_arc_not_a_straight_line():
    # #334 post-review, A4: 0x27 (owned router 51) -> foreign Router 12 is a
    # same-ROW router-to-router link in the real fixture — a straight <line>
    # at the same y as its endpoints used to sit exactly on top of whatever
    # box was between them and read as invisible.
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert '<path d="M' in html
    assert 'class="edge edge-arc"' in html


def test_viewbox_grows_to_fit_the_widest_row():
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    boxes = _layout(mesh)
    right_edge = max(left + width for left, _top, width, _height in boxes.values())
    html = render_thread_page(mesh, _diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    # the rendered viewBox width must be at least wide enough for every box
    import re  # pylint: disable=import-outside-toplevel
    match = re.search(r'viewBox="0 0 (\d+) \d+"', html)
    assert match is not None
    assert int(match.group(1)) >= right_edge


# ---------------------------------------------------------------------------
# #334 post-review — B4.7: RSSI colour bands match thread_mesh's own
# health-check thresholds (one source of truth)
# ---------------------------------------------------------------------------

def test_edge_colour_bands_match_thread_meshs_own_thresholds():
    from thread_mesh import RSSI_BAD, RSSI_WEAK
    from thread_page import _edge_color  # pylint: disable=import-outside-toplevel
    assert _edge_color(RSSI_WEAK) == _edge_color(0)  # >= -80: green, same as a great link
    assert _edge_color(RSSI_WEAK - 1) != _edge_color(RSSI_WEAK)  # -81: amber, not green
    assert _edge_color(RSSI_BAD) == _edge_color(RSSI_WEAK - 1)  # -90: still amber
    assert _edge_color(RSSI_BAD - 1) not in (_edge_color(RSSI_WEAK), _edge_color(RSSI_BAD))  # -91: red


def test_legend_names_the_same_bands_thread_mesh_uses():
    from thread_mesh import RSSI_BAD, RSSI_WEAK
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert f"{RSSI_WEAK}" in html
    assert f"{RSSI_BAD}" in html


# ---------------------------------------------------------------------------
# #334 post-review — B3.10: _merged_links uses the worse reading
# ---------------------------------------------------------------------------

def test_merged_links_colours_by_the_worse_of_the_two_sides():
    from thread_mesh import MeshLink
    from thread_page import _edge_color, _merged_links  # pylint: disable=import-outside-toplevel
    side_a = MeshLink(a="node:0x1", b="node:0x2", kind="router", avg_rssi=-60, last_rssi=-60,
                      lqi=3, frame_error_pct=0, seen_from="node:0x1")
    side_b = MeshLink(a="node:0x2", b="node:0x1", kind="router", avg_rssi=-85, last_rssi=-85,
                      lqi=3, frame_error_pct=2, seen_from="node:0x2")
    merged = _merged_links([side_a, side_b])
    assert len(merged) == 1
    assert merged[0]["overall_rssi"] == -85
    assert _edge_color(merged[0]["overall_rssi"]) == _edge_color(-85)
    assert -60 in merged[0]["rssi_values"] and -85 in merged[0]["rssi_values"]  # both shown, disagreeing


# ---------------------------------------------------------------------------
# #334 post-review — A1: the page's partition header matches the shared text
# ---------------------------------------------------------------------------

def test_page_header_uses_the_shared_partition_header_text():
    from thread_mesh import partition_header_text  # pylint: disable=import-outside-toplevel
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert _escape_for_html(partition_header_text(mesh, diags)) in html


def _escape_for_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# #334 post-review — B4.16: footer keying wording
# ---------------------------------------------------------------------------

def test_footer_correctly_describes_owned_nodes_as_keyed_by_matter_node_id():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "Owned nodes are keyed by their Matter node id" in html
    assert "this page keys every node by its router id" not in html


# ---------------------------------------------------------------------------
# #334 post-review — B5.9: http_thread_page's source stays free of writes
# ---------------------------------------------------------------------------

def test_http_thread_page_source_contains_no_write_tokens():
    """ADR-0004's own source-level test only covers diagnostics_menu_mixin.py
    and thread_survey.py — it cannot be extended to the whole of
    http_api_mixin.py, which legitimately holds the commission/decommission
    write paths. This pins the narrower guarantee this handler CAN make: its
    own source, and the snapshot helper it calls, carry none of the same
    forbidden write tokens (#334 finding B5.9)."""
    import inspect  # pylint: disable=import-outside-toplevel
    import http_api_mixin  # pylint: disable=import-outside-toplevel
    source = (inspect.getsource(http_api_mixin.HttpApiMixin.http_thread_page)
              + inspect.getsource(http_api_mixin.HttpApiMixin._thread_mesh_snapshot))
    for forbidden in ("MatterWrite", ".write(", "send_command", "MatterCommand"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# #334 post-review — B3.8: an Indigo device name reaches the page
# ---------------------------------------------------------------------------

def test_indigo_device_names_reach_the_page(plug, monkeypatch):
    diags = _fixture_diags()
    plug.device_sync.list_nodes.return_value = [(0x34, ["Grillplats socket"])]
    plug.device_sync.lookup.return_value = None  # no matterNode device — falls back to the join
    captured_names = {}

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        captured_names.update(node_names or {})
        return Survey(diags=diags, raw_count=len(diags), skipped=[])

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action())
    assert captured_names.get(0x34) == "Grillplats socket"
    assert reply["status"] == 200


# ---------------------------------------------------------------------------
# #334 post-review — B2.4: the three Survey outcomes at the HTTP layer
# ---------------------------------------------------------------------------

def test_raw_count_zero_renders_the_no_commissioned_nodes_banner(plug, monkeypatch):
    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey",
                        lambda *a, **k: Survey(diags=[], raw_count=0, skipped=[]))
    reply = plug.http_thread_page(_action())
    assert reply["status"] == 200
    assert "no commissioned nodes at all" in reply["content"]
    assert "<svg" not in reply["content"]


def test_all_raw_nodes_unaddressable_renders_an_error_banner(plug, monkeypatch):
    monkeypatch.setattr(
        "http_api_mixin.thread_survey.run_survey",
        lambda *a, **k: Survey(diags=[], raw_count=1, skipped=["raw node[0]: no node_id"]))
    reply = plug.http_thread_page(_action())
    assert reply["status"] == 200
    assert "could not be read" in reply["content"]
    assert "no node_id" in reply["content"]


def test_wifi_only_fabric_is_the_friendly_success_not_an_error(plug, monkeypatch):
    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey",
                        lambda *a, **k: Survey(diags=[], raw_count=1, skipped=[]))
    reply = plug.http_thread_page(_action())
    assert reply["status"] == 200
    assert "Could not read the Thread mesh" not in reply["content"]
    assert "No Thread devices are commissioned" in reply["content"]


# ---------------------------------------------------------------------------
# #334 post-review — B3.2: a ?live=1 timeout falls back to the cache
# ---------------------------------------------------------------------------

def test_live_timeout_falls_back_to_cache_and_says_so(plug, monkeypatch):
    diags = _fixture_diags()
    calls = []

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        calls.append(live_sleepy)
        if live_sleepy:
            from concurrent.futures import TimeoutError as FuturesTimeoutError  # pylint: disable=import-outside-toplevel
            raise FuturesTimeoutError()
        return Survey(diags=diags, raw_count=len(diags), skipped=[])

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action(query={"live": "1"}))
    assert reply["status"] == 200
    assert calls == [True, False]  # live attempted first, then the cache fallback
    assert "Live refresh timed out" in reply["content"]
    assert "showing cached data" in reply["content"]
    assert "GRILLPLATS Plug" in reply["content"]  # real data still rendered
    assert "<svg" in reply["content"]


def test_live_timeout_then_cache_failure_falls_back_to_the_bare_banner(plug, monkeypatch):
    from concurrent.futures import TimeoutError as FuturesTimeoutError  # pylint: disable=import-outside-toplevel

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        raise FuturesTimeoutError()

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action(query={"live": "1"}))
    assert reply["status"] == 200
    assert "did not answer in time" in reply["content"]
    assert "<svg" not in reply["content"]


def test_a_plain_cached_request_timing_out_does_not_retry(plug, monkeypatch):
    # A ?live=0 (default) request that times out has no cache to fall back TO
    # — it already was the cache attempt — so this must reach the plain "did
    # not answer in time" banner directly, with no second attempt.
    from concurrent.futures import TimeoutError as FuturesTimeoutError  # pylint: disable=import-outside-toplevel
    calls = []

    def _fake_run_survey(runtime, matter, *, live_sleepy, node_names=None, **kwargs):
        calls.append(live_sleepy)
        raise FuturesTimeoutError()

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    reply = plug.http_thread_page(_action())  # no ?live=1
    assert reply["status"] == 200
    assert calls == [False]  # exactly one attempt, no fallback-to-fallback
    assert "did not answer in time" in reply["content"]


# ---------------------------------------------------------------------------
# #334 post-review — B5.5: the page's live path uses its own (shorter) timeout
# ---------------------------------------------------------------------------

def test_live_path_uses_the_pages_own_per_node_timeout(plug, monkeypatch):
    import http_api_mixin  # pylint: disable=import-outside-toplevel
    captured = {}

    def _fake_run_survey(runtime, matter, *, live_sleepy, per_node_timeout=None, node_names=None, **kwargs):
        captured["per_node_timeout"] = per_node_timeout
        return Survey(diags=[], raw_count=0, skipped=[])

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    plug.http_thread_page(_action(query={"live": "1"}))
    assert captured["per_node_timeout"] == http_api_mixin._PAGE_LIVE_PER_NODE_TIMEOUT  # pylint: disable=protected-access
    assert http_api_mixin._PAGE_LIVE_PER_NODE_TIMEOUT < http_api_mixin.ATTRIBUTE_TIMEOUT  # pylint: disable=protected-access
