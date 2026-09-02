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
from datetime import datetime, timedelta, timezone
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
    p.device_sync.thread_event_stamps.return_value = {}  # #344
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
    assert reply["headers"]["Cache-Control"] == "no-store"
    assert "<!DOCTYPE html>" in reply["content"]
    assert "GRILLPLATS Plug" in reply["content"]


def test_only_GET_is_served(plug):
    reply = plug.http_thread_page(_action("POST"))
    assert reply["status"] == 405
    assert reply["headers"]["Cache-Control"] == "no-store"


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

def test_layout_glyphs_never_overlap():
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles = _layout(mesh)
    assert len(circles) == len(mesh.nodes)  # every node placed, none dropped
    items = list(circles.items())
    for i, (key_a, (xa, ya, ra)) in enumerate(items):
        for key_b, (xb, yb, rb) in items[i + 1:]:
            dist = ((xa - xb) ** 2 + (ya - yb) ** 2) ** 0.5
            assert dist >= ra + rb + 4, f"{key_a} overlaps {key_b}"


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


def _point_segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_sq))
    cx, cy = ax + t * dx, ay + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5


def _segment_intersects_rect(ax, ay, bx, by, x0, y0, x1, y1):
    """True if the closed segment (ax,ay)-(bx,by) intersects the axis-aligned
    rectangle [x0,x1] x [y0,y1] (Liang-Barsky clipping)."""
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q / p
            if p < 0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return True


def test_no_edge_passes_through_a_third_glyph():
    # #346: edges are straight lines now (no more arcs) — this pins the
    # property the arcs used to exist for, geometrically: no edge segment,
    # anywhere in the real fixture, comes closer to a THIRD node's glyph
    # than that glyph's own radius. The CENTRE node's excluded region is
    # bigger than its bare glyph (#346 fix 1): its own two label lines sit
    # just below it, so a chord that clears the glyph circle but still cuts
    # through "Router 14 (leader…" / "probably your border router" is still
    # a real defect the plain circle check couldn't see.
    from thread_page import _layout, _merged_links, _ring1_and_ring2  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles = _layout(mesh)

    leader, _ring1, _ring2, _linked_parent = _ring1_and_ring2(mesh)
    centre_label_rect = None
    if leader is not None:
        cx, cy, cr = circles[leader.key]
        half_w = 3.5 * min(len(leader.name), 22)
        centre_label_rect = (cx - half_w, cy + cr, cx + half_w, cy + cr + 34)

    for link in _merged_links(mesh.links):
        if link["a"] not in circles or link["b"] not in circles:
            continue
        ax, ay, ar = circles[link["a"]]
        bx, by, br = circles[link["b"]]
        dist = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
        if dist < 1e-6:
            continue
        ux, uy = (bx - ax) / dist, (by - ay) / dist
        sx, sy = ax + ux * ar, ay + uy * ar
        ex, ey = bx - ux * br, by - uy * br
        for key, (nx, ny, nr) in circles.items():
            if key in (link["a"], link["b"]):
                continue
            gap = _point_segment_distance(nx, ny, sx, sy, ex, ey)
            assert gap >= nr, f"edge {link['a']}->{link['b']} passes through {key}"
            if key == (leader.key if leader is not None else None) and centre_label_rect is not None:
                x0, y0, x1, y1 = centre_label_rect
                assert not _segment_intersects_rect(sx, sy, ex, ey, x0, y0, x1, y1), (
                    f"edge {link['a']}->{link['b']} passes through {key}'s label box")


def test_no_ring1_chord_passes_within_the_leaders_label_zone():
    # #346 fix 1: on the real fixture, the 0x27<->rid:12 chord used to sit
    # right on top of the leader's own two label lines. The ordering
    # objective now penalises any chord whose perpendicular distance from
    # the centre falls inside leader_r + 70.
    from thread_page import _chord_pairs, _layout, _ring1_and_ring2  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles = _layout(mesh)
    leader, ring1_members, _ring2, _linked_parent = _ring1_and_ring2(mesh)
    cx, cy, _cr = circles[leader.key]
    ring1_keys = {n.key for n in ring1_members}
    threshold = 30 + 70  # leader_r (always 30, per the fix) + the label-zone margin

    for a, b in _chord_pairs(ring1_keys, mesh.links):
        ax, ay, _ar = circles[a]
        bx, by, _br = circles[b]
        dx, dy = bx - ax, by - ay
        length = (dx ** 2 + dy ** 2) ** 0.5
        if length < 1e-6:
            continue
        dist = abs((cx - ax) * dy - (cy - ay) * dx) / length
        assert dist >= threshold, f"chord {a}->{b} at {dist:.1f} intrudes on the centre label zone"


# ---------------------------------------------------------------------------
# #334 finding 2, kept under the radial layout (#346) — node:0x40 (ALPSTUGA
# new, a REED whose parent is the leader rid:14) belongs on ring 1 beside the
# routers, not clustered on ring 2: one hop off the leader is one hop
# regardless of whether the far end is a router or not.
# ---------------------------------------------------------------------------

def test_a_leader_child_is_on_ring_1_same_distance_from_the_centre_as_the_routers():
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles = _layout(mesh)
    leader = next(n for n in mesh.nodes if n.is_leader)
    lx, ly, _lr = circles[leader.key]

    def _dist(key):
        x, y, _r = circles[key]
        return round(((x - lx) ** 2 + (y - ly) ** 2) ** 0.5, 6)

    router_dist = {_dist(k) for k in ("node:0x27", "node:0x34", "node:0x3F")}
    assert len(router_dist) == 1
    assert _dist("node:0x40") == next(iter(router_dist))


def test_viewbox_contains_every_glyph_plus_its_label_room():
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    mesh, diags = _fixture_mesh()
    circles = _layout(mesh)
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    import re  # pylint: disable=import-outside-toplevel
    match = re.search(r'viewBox="0 0 (\d+) (\d+)"', html)
    assert match is not None
    side = int(match.group(1))
    assert side == int(match.group(2))  # square, still centred on the leader
    for x, y, r in circles.values():
        assert x - r >= 0 and x + r <= side
        assert y - r >= 0 and y + r <= side


def test_every_label_box_lies_inside_the_viewbox_on_the_fixture():
    # #346 fix 3: the viewBox is computed from ACTUAL glyph + label extents,
    # not a flat label_room guess — this is the fixture-side half of that
    # fix's own required test.
    from thread_page import _display_name, _label_box, _layout_full  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles, ring2_angle, side = _layout_full(mesh)
    for node in mesh.nodes:
        if node.key not in circles:
            continue
        x, y, r = circles[node.key]
        angle = ring2_angle.get(node.key)
        x0, y0, x1, y1 = _label_box(x, y, r, _display_name(node), angle)
        assert x0 >= -1e-6 and x1 <= side + 1e-6, f"{node.key} label x out of the viewBox"
        assert y0 >= -1e-6 and y1 <= side + 1e-6, f"{node.key} label y out of the viewBox"


def test_viewbox_fits_a_22_char_name_far_left_and_far_right():
    # #346 fix 3's own synthetic case: two ring-2 names, one anchored well
    # into the right half (text-anchor start, extends further right) and one
    # well into the left half (text-anchor end, extends further left) — both
    # 22 characters, both must still fit inside the computed viewBox.
    from thread_mesh import Mesh, MeshLink, MeshNode  # pylint: disable=import-outside-toplevel
    from thread_page import _label_box, _layout_full  # pylint: disable=import-outside-toplevel

    router = MeshNode(key="node:0x1", node_id=1, name="Router A", role_name="Router", router_id=1,
                      rloc16=0x0400, foreign=False, is_leader=False, partition_id=None, children_count=0)
    left = MeshNode(key="node:0x2", node_id=2, name="L" * 22, role_name="EndDevice", router_id=None,
                    rloc16=None, foreign=False, is_leader=False, partition_id=None, children_count=0)
    right = MeshNode(key="node:0x3", node_id=3, name="R" * 22, role_name="EndDevice", router_id=None,
                     rloc16=None, foreign=False, is_leader=False, partition_id=None, children_count=0)
    links = [
        MeshLink(a="node:0x2", b="node:0x1", kind="parent", avg_rssi=-50, last_rssi=-50,
                 lqi=3, frame_error_pct=0, seen_from="node:0x2"),
        MeshLink(a="node:0x3", b="node:0x1", kind="parent", avg_rssi=-50, last_rssi=-50,
                 lqi=3, frame_error_pct=0, seen_from="node:0x3"),
    ]
    mesh = Mesh(network_name="net", channel=None, pan_id=None, partition_ids=[],
               majority_partition=None, majority_leader=None, nodes=[router, left, right], links=links,
               flags=[], unreadable=[], disagreements=[], warnings=[])

    circles, ring2_angle, side = _layout_full(mesh)
    # A single ring-1 node's sector spans the whole circle, so its two
    # children land at exactly +/-90 degrees from its own angle (0) —
    # "L..." (sorts first) on the left, "R..." on the right.
    assert ring2_angle["node:0x2"] % (2 * 3.141592653589793) > 3.0  # ~270 deg: left half
    assert 1.0 < ring2_angle["node:0x3"] < 2.0  # ~90 deg: right half

    for node in (router, left, right):
        x, y, r = circles[node.key]
        angle = ring2_angle.get(node.key)
        x0, y0, x1, y1 = _label_box(x, y, r, node.name, angle)
        assert x0 >= -1e-6 and x1 <= side + 1e-6, f"{node.key} label clipped horizontally"
        assert y0 >= -1e-6 and y1 <= side + 1e-6, f"{node.key} label clipped vertically"


# ---------------------------------------------------------------------------
# #346 — the map's own display bands (independent of thread_mesh's health
# thresholds; see thread_page.py's module-level comment on the constants)
# ---------------------------------------------------------------------------

def test_display_bands_are_the_documented_four():
    from thread_page import _edge_color  # pylint: disable=import-outside-toplevel
    assert _edge_color(-65) == "var(--edge-excellent)"
    assert _edge_color(-64) == "var(--edge-excellent)"
    assert _edge_color(-66) == "var(--edge-good)"
    assert _edge_color(-77) == "var(--edge-good)"
    assert _edge_color(-78) == "var(--edge-fair)"
    assert _edge_color(-87) == "var(--edge-fair)"
    assert _edge_color(-88) == "var(--edge-poor)"
    assert _edge_color(-200) == "var(--edge-poor)"
    assert _edge_color(None) == "var(--edge-unread)"


def test_display_bands_do_not_alter_health_thresholds():
    from thread_mesh import RSSI_BAD, RSSI_WEAK  # pylint: disable=import-outside-toplevel
    from thread_page import _edge_color  # pylint: disable=import-outside-toplevel
    assert RSSI_WEAK == -80
    assert RSSI_BAD == -90
    # -78 is >= RSSI_WEAK (still "good" for health — no weak_link flag) but
    # reads "fair" on the map's own display bands; the two are allowed to
    # disagree (they answer different questions).
    assert -78 >= RSSI_WEAK
    assert _edge_color(-78) == "var(--edge-fair)"


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
    # #334 post-review, B2.7/B3.6: the page path logs the timeout too, same
    # as the menu's own FuturesTimeoutError branch — "showing cached data" in
    # the page is not the only place this fact should be recorded.
    warn_msgs = [str(c) for c in plug.logger.warning.call_args_list]
    assert any("timed out" in m for m in warn_msgs)


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
# #334 post-review — B5.5: the page's live path uses its own per-node timeout
# (field correction #334, 2026-09-01: now pinned to the menu's budget,
# ATTRIBUTE_TIMEOUT, not a shorter one — a napping SED answers at its next
# poll, which a shorter budget structurally misses)
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
    # Semantic pin, not numeric: the live path matches the menu's budget.
    assert http_api_mixin._PAGE_LIVE_PER_NODE_TIMEOUT == http_api_mixin.ATTRIBUTE_TIMEOUT  # pylint: disable=protected-access


# ---------------------------------------------------------------------------
# #344 review — A4: event_stamps actually reaches run_survey (test-review #1)
# ---------------------------------------------------------------------------

def test_event_stamps_are_forwarded_to_run_survey(plug, monkeypatch):
    plug.device_sync.thread_event_stamps.return_value = {0x34: 555.0}
    captured = {}

    def _fake_run_survey(runtime, matter, *, live_sleepy, event_stamps=None, node_names=None, **kwargs):
        captured["event_stamps"] = event_stamps
        return Survey(diags=[], raw_count=0, skipped=[])

    monkeypatch.setattr("http_api_mixin.thread_survey.run_survey", _fake_run_survey)
    plug.http_thread_page(_action())
    assert captured["event_stamps"] == {0x34: 555.0}


# ---------------------------------------------------------------------------
# #344 — per-node cache age: stale chip, badge note, and the amber banner
# ---------------------------------------------------------------------------

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def test_cached_node_older_than_an_hour_gets_the_stale_chip_and_banner():
    mesh, diags = _fixture_mesh()
    diags[0].last_interview = NOW - timedelta(hours=6)
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    assert 'class="chip-stale"' in html
    assert "oldest node baseline: 6h" in html
    assert "Some of this data is matter-server" in html
    assert 'href="/message/com.simons-plugins.indigo-matter/thread?live=1"' in html
    assert "Refresh (live)" in html


def test_cached_node_under_an_hour_old_gets_neither_chip_nor_banner():
    mesh, diags = _fixture_mesh()
    for diag in diags:
        diag.last_interview = NOW - timedelta(minutes=10)
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    assert 'class="chip-stale"' not in html
    assert "Some of this data is matter-server" not in html
    # A known-but-fresh baseline still earns the header note (age is known).
    assert "oldest node baseline: 10m" in html


def test_live_render_shows_neither_banner_nor_oldest_baseline_note():
    # NOTE: a per-node stale CHIP is keyed to that diag's own `source` (a live
    # survey never live-reads routers, so a router can stay cache-sourced on
    # an otherwise-"live" page) — only the page-wide badge note and banner are
    # gated on the `live` flag itself, which is what this pins.
    mesh, diags = _fixture_mesh()
    for diag in diags:
        diag.last_interview = NOW - timedelta(days=6)
    html = render_thread_page(mesh, diags, generated_at="t", live=True, plugin_id=PLUGIN_ID, now=NOW)
    assert "oldest node baseline" not in html
    assert "Some of this data is matter-server" not in html


def test_unknown_interview_age_renders_an_em_dash_and_no_chip():
    mesh, diags = _fixture_mesh()  # fixture diags carry no last_interview at all
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    assert "interviewed — ·" in html
    assert 'class="chip-stale"' not in html
    assert "oldest node baseline" not in html


# ---------------------------------------------------------------------------
# #344 review — A3: "oldest node baseline" must not hide unknown baselines
# ---------------------------------------------------------------------------

def test_mixed_known_and_unknown_baselines_names_the_unknown_count():
    mesh, diags = _fixture_mesh()
    diags[0].last_interview = NOW - timedelta(minutes=10)
    diags[1].last_interview = NOW - timedelta(hours=6)
    # diags[2:] keep last_interview=None — genuinely unknown.
    unknown_count = len(diags) - 2
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    # This also kills the max-vs-min mutant: the OLDEST known age (6h) must
    # win, not the freshest (10m).
    assert f"oldest node baseline: 6h ({unknown_count} unknown)" in html
    assert "oldest node baseline: 10m" not in html
    assert f"{unknown_count} node(s) have no interview baseline at all." in html
    # Named BEFORE the "Use Refresh (live)" sentence (A3's ordering call).
    banner_start = html.index("Some of this data is matter-server")
    unknown_at = html.index(f"{unknown_count} node(s) have no interview baseline at all.")
    use_refresh_at = html.index("Use <a href=", banner_start)
    assert banner_start < unknown_at < use_refresh_at


def test_all_known_baselines_wording_is_unchanged():
    # A3: no "(N unknown)" suffix at all when every diag's baseline is known.
    mesh, diags = _fixture_mesh()
    for diag in diags:
        diag.last_interview = NOW - timedelta(hours=2)
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    assert "oldest node baseline: 2h</span>" in html
    assert "unknown)" not in html
    assert "have no interview baseline at all" not in html


# ---------------------------------------------------------------------------
# #344 review — C3: a live-sourced diag's own stale chip is keyed to ITS
# `source`, never the page-wide `live` flag
# ---------------------------------------------------------------------------

def test_live_sourced_diag_with_old_interview_gets_no_stale_chip():
    mesh, diags = _fixture_mesh()
    diags[0].source = "live"
    diags[0].last_interview = NOW - timedelta(hours=6)
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    assert 'class="chip-stale"' not in html
    assert "live · interviewed 6h ago" in html


def test_hostile_node_name_is_still_escaped_alongside_the_new_freshness_cell():
    attrs = {
        "0/53/1": 2,  # SleepyEndDevice
        "0/53/2": "net", "0/53/3": 1, "0/53/4": 1,
        "0/53/7": [{"0": 1, "1": 5, "2": 0x2800, "5": 3, "6": -60, "7": -60,
                    "8": 0, "9": 0, "10": True, "11": True, "13": False}],
    }
    diag = parse_node_diag(1, "<script>alert(1)</script>", True, attrs)
    diag.last_interview = NOW - timedelta(hours=6)
    mesh = build_mesh([diag])
    html = render_thread_page(mesh, [diag], generated_at="t", live=False, plugin_id=PLUGIN_ID, now=NOW)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    # The stale chip fires alongside it — proves the escaping survives the
    # new cell's own wrapping, not just the unrelated name column.
    assert 'class="chip-stale"' in html


# ---------------------------------------------------------------------------
# #346 — radial layout: leader at the centre, ring 1 equidistant, ring-2
# angle placement, crossing minimisation, edge shortening/labels, node
# glyphs (RLOC16, truncation, warning badges), and the legend.
# ---------------------------------------------------------------------------

def test_leader_is_at_the_viewbox_centre_and_every_ring1_node_is_equidistant():
    from thread_page import _layout_full, _ring1_and_ring2  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles, _ring2_angle, side = _layout_full(mesh)
    leader, ring1_members, _ring2, _linked_parent = _ring1_and_ring2(mesh)
    center = side / 2
    lx, ly, _lr = circles[leader.key]
    assert abs(lx - center) < 1e-6
    assert abs(ly - center) < 1e-6

    distances = {round(((circles[n.key][0] - lx) ** 2 + (circles[n.key][1] - ly) ** 2) ** 0.5, 6)
                 for n in ring1_members}
    assert len(distances) == 1


def test_each_ring2_nodes_angle_is_closer_to_its_own_parent_than_any_other_ring1_node():
    import math  # pylint: disable=import-outside-toplevel
    from thread_page import _layout_full, _ring1_and_ring2  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles, ring2_angle, side = _layout_full(mesh)
    center = side / 2
    _leader, ring1_members, _ring2, linked_parent = _ring1_and_ring2(mesh)

    ring1_angle = {}
    for node in ring1_members:
        x, y, _r = circles[node.key]
        ring1_angle[node.key] = math.atan2(x - center, -(y - center))

    def _angular_gap(a, b):
        diff = abs(a - b) % (2 * math.pi)
        return min(diff, 2 * math.pi - diff)

    checked = 0
    for key, angle in ring2_angle.items():
        parent_key = linked_parent.get(key)
        if parent_key not in ring1_angle:
            continue  # orphan: nothing to be "closer" to
        own_gap = _angular_gap(angle, ring1_angle[parent_key])
        for other_key, other_angle in ring1_angle.items():
            if other_key == parent_key:
                continue
            assert own_gap < _angular_gap(angle, other_angle)
        checked += 1
    assert checked > 0


def test_crossing_minimisation_places_the_diagonal_non_adjacent_and_is_deterministic():
    # Synthetic 4-router ring: A-B, B-C, C-D, D-A (the ring itself) plus the
    # A-C diagonal. The only zero-crossing arrangement puts A and C OPPOSITE
    # each other, not adjacent.
    from thread_mesh import Mesh, MeshLink, MeshNode  # pylint: disable=import-outside-toplevel
    from thread_page import (  # pylint: disable=import-outside-toplevel
        _chord_pairs, _count_crossings, _order_ring1, _ring1_and_ring2,
    )

    def _router(key, name, router_id):
        return MeshNode(key=key, node_id=router_id, name=name, role_name="Router", router_id=router_id,
                        rloc16=router_id << 10, foreign=False, is_leader=False, partition_id=None,
                        children_count=0)

    nodes = [_router(f"node:0x{i}", name, i) for i, name in enumerate("ABCD", start=1)]
    cycle = [("node:0x1", "node:0x2"), ("node:0x2", "node:0x3"), ("node:0x3", "node:0x4"),
             ("node:0x4", "node:0x1"), ("node:0x1", "node:0x3")]
    links = [MeshLink(a=a, b=b, kind="router", avg_rssi=-50, last_rssi=-50, lqi=3, frame_error_pct=0, seen_from=a)
             for a, b in cycle]
    mesh = Mesh(network_name="net", channel=None, pan_id=None, partition_ids=[],
               majority_partition=None, majority_leader=None, nodes=nodes, links=links,
               flags=[], unreadable=[], disagreements=[], warnings=[])

    _leader, ring1_members, _ring2, _linked_parent = _ring1_and_ring2(mesh)
    ring1_keys = {n.key for n in ring1_members}
    chords = _chord_pairs(ring1_keys, mesh.links)
    import math  # pylint: disable=import-outside-toplevel
    sector_width = 2 * math.pi / len(ring1_members)  # no leader, no ring 2: no orphan sector

    order_1 = [n.key for n in _order_ring1(ring1_members, chords, False, sector_width)]
    order_2 = [n.key for n in _order_ring1(ring1_members, chords, False, sector_width)]
    assert order_1 == order_2  # deterministic: same mesh, same order every time

    position = {key: i for i, key in enumerate(order_1)}
    ordered_nodes = sorted(ring1_members, key=lambda n: position[n.key])
    assert _count_crossings(ordered_nodes, chords) == 0
    gap = abs(position["node:0x1"] - position["node:0x3"])
    assert gap == 2  # opposite on a 4-node ring, not adjacent (gap 1 or 3)


def test_edge_endpoints_are_shortened_by_the_glyph_radius_not_the_bare_centres():
    import re  # pylint: disable=import-outside-toplevel
    from thread_page import _layout, _link_svg, _merged_links  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    circles = _layout(mesh)
    nodes_by_key = {n.key: n for n in mesh.nodes}
    checked = 0
    for link in _merged_links(mesh.links):
        if link["a"] not in circles or link["b"] not in circles:
            continue
        svg = _link_svg(link, circles, nodes_by_key)
        match = re.search(r'x1="([\d.-]+)" y1="([\d.-]+)" x2="([\d.-]+)" y2="([\d.-]+)"', svg)
        assert match is not None
        x1, y1, x2, y2 = (float(v) for v in match.groups())
        ax, ay, _ar = circles[link["a"]]
        bx, by, _br = circles[link["b"]]
        assert ((x1 - ax) ** 2 + (y1 - ay) ** 2) ** 0.5 > 1
        assert ((x2 - bx) ** 2 + (y2 - by) ** 2) ** 0.5 > 1
        checked += 1
    assert checked > 0


def test_no_edge_ever_shows_a_visible_label_every_edge_still_gets_a_title():
    # #346 fix 2: a poor edge's own visible label used to collide with a
    # short spoke's far-node name/role text; the colour + warning badge +
    # flags list + this <title> already say the same thing, so NO band gets
    # a visible label any more.
    from types import SimpleNamespace  # pylint: disable=import-outside-toplevel
    from thread_page import _link_svg  # pylint: disable=import-outside-toplevel
    positions = {"node:0x1": (0.0, 0.0, 20.0), "node:0x2": (200.0, 0.0, 20.0)}
    nodes_by_key = {"node:0x1": SimpleNamespace(name="Alpha"), "node:0x2": SimpleNamespace(name="Bravo")}

    def _link(rssi):
        return {"a": "node:0x1", "b": "node:0x2", "kind": "router",
                "rssi_values": [rssi] if rssi is not None else [],
                "overall_rssi": rssi, "lqi": 3, "frame_error_pct": 0}

    for rssi in (-60, -70, -85, -95):  # excellent / good / fair / poor
        svg = _link_svg(_link(rssi), positions, nodes_by_key)
        assert 'class="edge-label"' not in svg
        assert "<title>" in svg
        assert str(rssi) in svg  # the reading still lives in the title


def test_bad_flag_renders_the_red_badge_info_only_renders_none():
    from thread_mesh import HealthFlag  # pylint: disable=import-outside-toplevel
    from thread_page import _node_glyph_svg  # pylint: disable=import-outside-toplevel
    mesh, _diags = _fixture_mesh()
    node = next(n for n in mesh.nodes if not n.foreign and n.role_name == "Router")
    circle = (100.0, 100.0, 26.0)

    bad_flags = [HealthFlag(node_key=node.key, severity="bad", code="weak_link",
                             message=f"{node.name}: link at -95 dBm")]
    svg_bad = _node_glyph_svg(node, circle, bad_flags)
    assert "var(--badge-bad)" in svg_bad
    assert f"{node.name}: link at -95 dBm" in svg_bad

    info_flags = [HealthFlag(node_key=node.key, severity="info", code="stale", message="cached")]
    svg_info = _node_glyph_svg(node, circle, info_flags)
    assert "var(--badge-bad)" not in svg_info
    assert "var(--badge-warn)" not in svg_info


def test_rloc16_appears_inside_the_glyph_r_style_fallback_for_a_foreign_router():
    from thread_mesh import MeshNode  # pylint: disable=import-outside-toplevel
    from thread_page import _node_glyph_svg  # pylint: disable=import-outside-toplevel
    owned = MeshNode(key="node:0x34", node_id=0x34, name="GRILLPLATS Plug", role_name="Router",
                     router_id=62, rloc16=0xF800, foreign=False, is_leader=False,
                     partition_id=None, children_count=0)
    assert ">F800<" in _node_glyph_svg(owned, (100.0, 100.0, 26.0), [])

    foreign_no_rloc = MeshNode(key="rid:12", node_id=None, name="Router 12", role_name="Router",
                               router_id=12, rloc16=None, foreign=True, is_leader=False,
                               partition_id=None, children_count=0)
    assert ">R12<" in _node_glyph_svg(foreign_no_rloc, (100.0, 100.0, 24.0), [])


def test_long_name_is_truncated_visibly_but_complete_and_escaped_in_the_title():
    import re  # pylint: disable=import-outside-toplevel
    from thread_mesh import MeshNode  # pylint: disable=import-outside-toplevel
    from thread_page import _escape, _node_glyph_svg, _truncate  # pylint: disable=import-outside-toplevel
    long_name = "<script>alert(1)</script> a very long device name indeed"
    node = MeshNode(key="node:0x99", node_id=0x99, name=long_name, role_name="Router",
                    router_id=5, rloc16=0x1400, foreign=False, is_leader=False,
                    partition_id=None, children_count=0)
    svg = _node_glyph_svg(node, (100.0, 100.0, 26.0), [])
    assert "<script>alert(1)</script>" not in svg
    assert "&lt;script&gt;" in svg

    # #346 fix 4: 22 visible chars (was 18), only once the name exceeds 24.
    truncated = _truncate(long_name)
    assert len(truncated) <= 22
    assert truncated.endswith("…")
    tspans = re.findall(r'<tspan x="[\d.]+">(.*?)</tspan>', svg)
    assert tspans
    assert tspans[0] == _escape(truncated)  # the VISIBLE text is the truncated (then escaped) name

    title_match = re.search(r"<title>(.*?)</title>", svg, re.S)
    assert title_match is not None
    assert "&lt;script&gt;alert(1)&lt;/script&gt; a very long device name indeed" in title_match.group(1)


def test_truncation_limit_threshold_and_minimum_kept_characters():
    # #346 fix 4: a name that FITS in 22 is left alone even past the old 18
    # limit; only a name over 24 gets shortened, and never to fewer than 3
    # real characters before the ellipsis.
    from thread_page import _truncate  # pylint: disable=import-outside-toplevel
    name_20 = "A" * 20
    assert _truncate(name_20) == name_20  # 20 <= 24: untouched
    name_24 = "B" * 24
    assert _truncate(name_24) == name_24  # exactly the threshold: untouched
    name_25 = "C" * 25
    truncated = _truncate(name_25)
    assert truncated == "C" * 21 + "…"
    assert len(truncated) == 22
    assert _truncate("hello world", limit=2, threshold=5) == "hel…"  # never fewer than 3 kept


def test_legend_is_a_details_block_naming_every_glyph_and_all_five_bands():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert '<details class="legend msg">' in html
    assert "What do the colours mean?" in html
    for title in ("Leader", "Router", "REED", "End device / Sleepy end device", "Border router",
                  "Unidentified router", "Warning triangle"):
        assert title in html
    for band in ("Excellent", "Good", "Fair", "Poor", "Unknown"):
        assert band in html


def test_layout_of_an_empty_mesh_is_empty():
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    mesh = build_mesh([])
    assert _layout(mesh) == {}


def test_layout_of_a_leaderless_mesh_still_places_every_node():
    from thread_mesh import Mesh, MeshLink, MeshNode  # pylint: disable=import-outside-toplevel
    from thread_page import _layout  # pylint: disable=import-outside-toplevel
    router = MeshNode(key="node:0x1", node_id=1, name="Router A", role_name="Router", router_id=1,
                      rloc16=0x0400, foreign=False, is_leader=False, partition_id=None, children_count=0)
    sed = MeshNode(key="node:0x2", node_id=2, name="Plug", role_name="SleepyEndDevice", router_id=None,
                   rloc16=None, foreign=False, is_leader=False, partition_id=None, children_count=0)
    link = MeshLink(a="node:0x2", b="node:0x1", kind="parent", avg_rssi=-50, last_rssi=-50,
                    lqi=3, frame_error_pct=0, seen_from="node:0x2")
    mesh = Mesh(network_name="net", channel=None, pan_id=None, partition_ids=[],
               majority_partition=None, majority_leader=None, nodes=[router, sed], links=[link],
               flags=[], unreadable=[], disagreements=[], warnings=[])
    circles = _layout(mesh)
    assert set(circles) == {"node:0x1", "node:0x2"}


# ---------------------------------------------------------------------------
# #346 fix 5 — phone-width legibility: pannable wrapper + a floor under the
# SVG's own shrink so 14px text never renders as ~7px.
# ---------------------------------------------------------------------------

def test_map_svg_has_a_min_width_inside_a_horizontally_scrollable_wrapper():
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert '<div class="map-wrap">' in html
    wrap_start = html.index('<div class="map-wrap">')
    svg_start = html.index("<svg", wrap_start)
    assert "min-width:560px" in html[svg_start:svg_start + 300]
    assert "width:100%" in html[svg_start:svg_start + 300]


# ---------------------------------------------------------------------------
# #346 fix 6 — the role sub-line reads plain words, not the raw role_name;
# role_name itself is untouched everywhere else (table, flags).
# ---------------------------------------------------------------------------

def test_role_sub_line_reads_plain_words_not_the_raw_role_name():
    from thread_mesh import MeshNode  # pylint: disable=import-outside-toplevel
    from thread_page import _node_glyph_svg  # pylint: disable=import-outside-toplevel
    sed = MeshNode(key="node:0x1", node_id=1, name="Plug", role_name="SleepyEndDevice", router_id=None,
                   rloc16=None, foreign=False, is_leader=False, partition_id=None, children_count=0)
    svg = _node_glyph_svg(sed, (100.0, 100.0, 12.0), [])
    assert "Sleepy end device" in svg
    assert "SleepyEndDevice" not in svg


def test_role_sub_line_display_mapping_does_not_change_the_table_or_flags():
    # The Nodes table's Role column and every flag message still carry the
    # raw role_name — this is a display-only substitution inside the glyph's
    # own label, nowhere else.
    mesh, diags = _fixture_mesh()
    html = render_thread_page(mesh, diags, generated_at="t", live=False, plugin_id=PLUGIN_ID)
    assert "SleepyEndDevice" in html  # still literal in the Nodes table
    assert "Reed" in html  # thread_mesh's own role_name spelling, in the table
