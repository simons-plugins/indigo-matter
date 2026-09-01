"""Tests for the Thread mesh model — #334.

Fixture: ``tests/fixtures/thread_mesh/nodes.json``, 7 real nodes captured live from
jarvis on 2026-09-01 (0x27, 0x2E, 0x2F, 0x34, 0x38, 0x3F, 0x40). Every "real fact"
below is independently derivable from that fixture's raw RouteTable/NeighborTable
data (see the investigation that produced this file) and is asserted here so a
future change to the parsing rules cannot silently get them wrong again — the same
motivation as ADR-0003's "no human transcription" rule for ``settings_report``.

Covers: both attribute key shapes, self-entry inference (including the genuinely
ambiguous 0x34 case), neighbour/route field mapping, health flags, the foreign
leader, partition disagreement for stale cached sleepy-device reads, precision-lossy
ExtAddress rendering, and degradation on malformed/missing/unavailable input —
never a crash.
"""
from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from thread_mesh import (
    ATTR_LEADER_ROUTER_ID,
    ATTR_NEIGHBOR_TABLE,
    ATTR_NETWORK_NAME,
    ATTR_PARTITION_ID,
    ATTR_ROUTE_TABLE,
    ATTR_ROUTING_ROLE,
    CLUSTER_THREAD_DIAG,
    INVALID_ROUTER_ID,
    ROLE_UNSPECIFIED,
    HealthFlag,
    Mesh,
    MeshLink,
    MeshNode,
    Neighbour,
    NodeDiag,
    Route,
    build_mesh,
    format_ext_address,
    freshness_text,
    health_flags,
    humanise_age,
    parse_node_diag,
    partition_header_text,
    render_report,
    rloc_label,
    summary_states,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "thread_mesh" / "nodes.json"


def _raw_nodes() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _tuple_keyed(attrs: dict) -> dict:
    """Convert matter-server's raw "0/53/7" string keys into the
    ``matter_model.NodeInfo``-style ``(0, 0x35, 7)`` tuple keys."""
    out = {}
    for key, value in attrs.items():
        endpoint, cluster, attribute = (int(part) for part in key.split("/"))
        out[(endpoint, cluster, attribute)] = value
    return out


def _parse_fixture(node_id: int, *, tuple_keys: bool = False) -> NodeDiag:
    raw = next(n for n in _raw_nodes() if n["node_id"] == node_id)
    attrs = _tuple_keyed(raw["attributes"]) if tuple_keys else raw["attributes"]
    name = raw["attributes"].get("0/40/3") or raw["attributes"].get("0/40/1") or ""
    diag = parse_node_diag(node_id, name, raw["available"], attrs)
    assert diag is not None
    return diag


def _all_diags(*, tuple_keys: bool = False) -> list[NodeDiag]:
    return [_parse_fixture(n["node_id"], tuple_keys=tuple_keys) for n in _raw_nodes()]


# ---------------------------------------------------------------------------
# Both attribute key shapes
# ---------------------------------------------------------------------------

class TestKeyShapes:
    def test_string_keys_parse(self):
        diag = _parse_fixture(0x27)
        assert diag.role == 5
        assert diag.channel == 25

    def test_tuple_keys_parse_identically(self):
        string_diag = _parse_fixture(0x27, tuple_keys=False)
        tuple_diag = _parse_fixture(0x27, tuple_keys=True)
        assert string_diag.role == tuple_diag.role
        assert string_diag.partition_id == tuple_diag.partition_id
        assert len(string_diag.neighbours) == len(tuple_diag.neighbours)
        assert len(string_diag.routes) == len(tuple_diag.routes)


# ---------------------------------------------------------------------------
# Real facts (verified live 2026-09-01)
# ---------------------------------------------------------------------------

class TestRealFacts:
    def test_majority_partition_and_foreign_leader(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        assert 1388375940 in mesh.partition_ids
        assert mesh.majority_partition == 1388375940
        assert mesh.majority_leader == 14
        leader = next(n for n in mesh.nodes if n.is_leader)
        assert leader.foreign is True
        assert leader.router_id == 14
        assert leader.rloc16 == 0x3800
        assert "leader" in leader.name.lower()

    def test_0x3f_is_router_23_with_one_child_and_three_neighbours(self):
        # 0x3F (node 63): self-entry router_id 23, one child 0x5C04, three neighbours.
        diag = _parse_fixture(0x3F)
        assert diag.router_id == 23
        assert diag.rloc16 == 0x5C00
        assert len(diag.neighbours) == 3
        children = diag.children
        assert len(children) == 1
        assert children[0].rloc16 == 0x5C04

    def test_0x34_has_one_neighbour_which_is_node_0x3f(self):
        diag_34 = _parse_fixture(0x34)
        diag_3f = _parse_fixture(0x3F)
        assert len(diag_34.neighbours) == 1
        neighbour = diag_34.neighbours[0]
        assert neighbour.rloc16 == 0x5C00
        assert neighbour.router_id == diag_3f.router_id == 23

    def test_0x27_is_router_51_and_parent_of_sed_0x2e(self):
        diag_27 = _parse_fixture(0x27)
        diag_2e = _parse_fixture(0x2E)
        assert diag_27.router_id == 51
        assert diag_27.rloc16 == 0xCC00
        parent = diag_2e.parent
        assert parent is not None
        assert parent.router_id == 51
        assert parent.last_rssi == -95
        assert diag_2e.counters.get("parent_changes") == 10

    def test_0x2f_parent_is_foreign_router_49(self):
        diag = _parse_fixture(0x2F)
        parent = diag.parent
        assert parent is not None
        assert parent.router_id == 49
        diags = _all_diags()
        owned_router_ids = {d.router_id for d in diags if d.router_id is not None}
        assert 49 not in owned_router_ids

    def test_0x38_parent_is_router_62_which_is_owned_node_0x34(self):
        # Follow-up correction: the self-entry rule is role-gated and requires
        # LinkEstablished == False, which resolves 0x34 to router 62 (0xF800) —
        # so 0x38's parent is NOT foreign after all, it is 0x34 itself.
        diag_38 = _parse_fixture(0x38)
        diag_34 = _parse_fixture(0x34)
        parent = diag_38.parent
        assert parent is not None
        assert parent.router_id == 62
        assert diag_34.router_id == 62
        mesh = build_mesh(_all_diags())
        link = next(link for link in mesh.links if link.a == "node:0x38")
        assert link.kind == "parent"
        assert link.b == "node:0x34"

    def test_0x40_is_a_reed(self):
        diag = _parse_fixture(0x40)
        assert diag.role_name == "Reed"
        assert diag.is_router is False
        assert diag.router_id is None

    def test_0x34_self_entry_resolves_to_router_62(self):
        # Follow-up correction: verified against the fixture that a node is never
        # its own radio neighbour, so the self-entry is the NextHop==63/PathCost==0/
        # Allocated row with LinkEstablished == False — 0x34's other candidate
        # (0x5C00 / router 23) is its neighbour 0x3F, which has LinkEstablished True.
        diag = _parse_fixture(0x34)
        assert diag.role_name == "Router"
        assert diag.router_id == 62
        assert diag.rloc16 == 0xF800
        assert diag.parse_warnings == []

    def test_synthetic_self_entry_still_genuinely_ambiguous_returns_none(self):
        # A Router role with two RouteTable rows that BOTH satisfy the full
        # self-entry shape (including LinkEstablished == False) and disagree on
        # router id — a node cannot be two routers at once, so this must degrade
        # to None with a recorded warning rather than pick one arbitrarily.
        attrs = {
            "0/53/1": 5,  # Router
            "0/53/8": [
                {"0": 1, "1": 1024, "2": 1, "3": 63, "4": 0, "5": 0, "6": 0, "7": 0,
                 "8": True, "9": False},
                {"0": 2, "1": 2048, "2": 2, "3": 63, "4": 0, "5": 0, "6": 0, "7": 0,
                 "8": True, "9": False},
            ],
        }
        diag = parse_node_diag(99, "ambiguous router", True, attrs)
        assert diag is not None
        assert diag.router_id is None
        assert diag.rloc16 is None
        assert any("self-entry ambiguous" in warning for warning in diag.parse_warnings)

    def test_reed_route_row_shaped_like_a_self_entry_is_not_mistaken_for_one(self):
        # 0x40 (REED, role 4) has a RouteTable row (router 23, NextHop 63, PathCost
        # 0, Allocated, LinkEstablished False) that is shaped exactly like a
        # self-entry but describes 0x3F, not itself — the role gate is why REED
        # never gets a router_id at all, regardless of what its routes contain.
        diag = _parse_fixture(0x40)
        assert diag.role_name == "Reed"
        assert diag.router_id is None

    def test_stale_cached_partitions_are_disagreements_not_a_split(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        joined = " | ".join(mesh.disagreements)
        assert "0x2F" in joined
        assert "604415567" in joined
        assert "0x38" in joined
        assert "1981064012" in joined
        assert "1388375940" in joined  # the majority, named in each line
        # Never asserted as a split: no disagreement text claims a partition split.
        assert not any("split" in d.lower() for d in mesh.disagreements)


# ---------------------------------------------------------------------------
# Health flags
# ---------------------------------------------------------------------------

class TestHealthFlags:
    def test_0x34_single_neighbour_warn(self):
        diag = _parse_fixture(0x34)
        codes = {(f.code, f.severity) for f in health_flags(diag)}
        assert ("single_neighbour", "warn") in codes

    def test_0x2e_weak_link_bad_and_unstable_parent(self):
        # weak_link judges the WORSE of avg/last (0x2E: avg -21, last -95) and
        # names which one tripped it — here last_rssi, per the coordinator's
        # follow-up. Neighbour.rssi (display) stays last-preferred separately.
        diag = _parse_fixture(0x2E)
        flags = health_flags(diag)
        codes = {(f.code, f.severity) for f in flags}
        assert ("weak_link", "bad") in codes
        assert ("unstable_parent", "warn") in codes
        weak_link = next(f for f in flags if f.code == "weak_link")
        assert "-95" in weak_link.message
        assert "lastRssi" in weak_link.message

    def test_weak_link_names_avg_when_avg_is_the_worse_reading(self):
        # Synthetic: avg worse than last — the flag must name "avgRssi", proving
        # the check picks the worse field rather than always preferring last.
        attrs = {
            "0/53/1": 2,  # SleepyEndDevice
            "0/53/7": [{
                "0": 1, "1": 1, "2": 4096, "3": 1, "4": 1, "5": 1,
                "6": -95, "7": -21, "8": 0, "9": 0, "10": True, "11": True, "13": False,
            }],
        }
        diag = parse_node_diag(50, "synthetic sed", True, attrs)
        assert diag is not None
        flags = health_flags(diag)
        weak_link = next(f for f in flags if f.code == "weak_link")
        assert weak_link.severity == "bad"
        assert "-95" in weak_link.message
        assert "avgRssi" in weak_link.message

    def test_0x27_far_from_leader(self):
        diag = _parse_fixture(0x27)
        codes = {(f.code, f.severity) for f in health_flags(diag)}
        assert ("far_from_leader", "warn") in codes

    def test_0x3f_not_far_from_leader(self):
        diag = _parse_fixture(0x3F)
        codes = {f.code for f in health_flags(diag) if f.severity != "info"}
        assert "far_from_leader" not in codes

    def test_sleepy_cached_nodes_get_info_stale_note(self):
        diag = _parse_fixture(0x2F)  # role SleepyEndDevice, source defaults "cache"
        flags = health_flags(diag)
        assert any(f.code == "stale" and f.severity == "info" for f in flags)

    def test_live_source_gets_no_stale_note(self):
        diag = _parse_fixture(0x2F)
        diag.source = "live"
        flags = health_flags(diag)
        assert not any(f.code == "stale" for f in flags)

    def test_health_flags_node_key_matches_mesh_key(self):
        diag = _parse_fixture(0x2E)
        flags = health_flags(diag)
        assert all(f.node_key == "node:0x2E" for f in flags)


# ---------------------------------------------------------------------------
# summary_states — exactly the four ADR-0008 keys
# ---------------------------------------------------------------------------

class TestSummaryStates:
    def test_keys_are_exactly_the_four_matternode_states(self):
        diag = _parse_fixture(0x27)
        assert set(summary_states(diag).keys()) == {
            "threadRole", "threadNeighbourCount", "threadLinkRssi", "threadHealth",
        }

    def test_values_for_a_healthy_router(self):
        diag = _parse_fixture(0x3F)
        states = summary_states(diag)
        assert states["threadRole"] == "Router"
        assert states["threadNeighbourCount"] == 3
        assert isinstance(states["threadLinkRssi"], int)
        assert states["threadHealth"] in ("ok", "warn", "bad")

    def test_bad_flag_makes_health_bad(self):
        diag = _parse_fixture(0x2E)
        assert summary_states(diag)["threadHealth"] == "bad"

    def test_unknown_rssi_is_not_written_at_all(self):
        # B2.9: a -128 sentinel used to be written for "no rssi reading" and
        # trips numeric triggers exactly like a real bad reading would — the
        # fix is to omit the key, leaving the Indigo state untouched, not to
        # pick a different fake number.
        diag = NodeDiag(
            node_id=1, name="lonely router", available=True, role=5, channel=None,
            network_name="", pan_id=None, ext_pan_id=None, partition_id=None,
            leader_router_id=None, neighbours=[], routes=[], counters={},
        )
        states = summary_states(diag)
        assert "threadLinkRssi" not in states
        # the router still gets health/neighbour states — only the rssi is unknown
        assert states["threadNeighbourCount"] == 0
        assert "threadHealth" in states


# ---------------------------------------------------------------------------
# build_mesh
# ---------------------------------------------------------------------------

class TestBuildMesh:
    def test_owned_nodes_all_present(self):
        mesh = build_mesh(_all_diags())
        owned_keys = {n.key for n in mesh.nodes if not n.foreign and n.node_id is not None}
        expected = {f"node:0x{nid:X}" for nid in (0x27, 0x2E, 0x2F, 0x34, 0x38, 0x3F, 0x40)}
        assert expected <= owned_keys

    def test_unmatched_child_of_0x3f_becomes_anonymous_node(self):
        mesh = build_mesh(_all_diags())
        anonymous = [n for n in mesh.nodes if n.key == "child:0x5C04"]
        assert len(anonymous) == 1
        assert anonymous[0].node_id is None
        link = next(link for link in mesh.links if link.a == "child:0x5C04")
        assert link.kind == "child"
        assert link.b == "node:0x3F"

    def test_identified_parent_does_not_also_spawn_an_anonymous_child(self):
        # 0x27 is not the source of any anonymous "child:" node: its two neighbours
        # are both peer routers (is_child False), so there is nothing to spawn.
        mesh = build_mesh(_all_diags())
        assert not any(link.kind == "child" and link.b == "node:0x27" for link in mesh.links)

    def test_foreign_routers_are_exactly_12_14_49(self):
        # Follow-up correction: router 62 is owned (node:0x34), so it is no longer
        # in the foreign set — only 12 (0x3000), 14 (leader, 0x3800) and 49
        # (0xC400) remain foreign anywhere in the fixture.
        mesh = build_mesh(_all_diags())
        foreign_ids = {n.router_id for n in mesh.nodes if n.foreign}
        assert foreign_ids == {12, 14, 49}
        foreign_by_id = {n.router_id: n for n in mesh.nodes if n.foreign}
        assert foreign_by_id[14].rloc16 == 0x3800
        assert foreign_by_id[14].is_leader is True
        assert foreign_by_id[12].rloc16 == 0x3000
        assert foreign_by_id[49].rloc16 == 0xC400

    def test_0x3f_neighbour_router_62_links_to_owned_node_0x34(self):
        mesh = build_mesh(_all_diags())
        link = next(
            link for link in mesh.links
            if link.kind == "router" and link.a == "node:0x3F" and link.b == "node:0x34"
        )
        assert link.avg_rssi == -77
        assert link.frame_error_pct == 58

    def test_flags_aggregate_across_all_nodes(self):
        mesh = build_mesh(_all_diags())
        codes = {f.code for f in mesh.flags}
        assert {"single_neighbour", "weak_link", "unstable_parent", "far_from_leader"} <= codes

    def test_empty_input_degrades_cleanly(self):
        mesh = build_mesh([])
        assert mesh.nodes == []
        assert mesh.links == []
        assert mesh.flags == []
        assert mesh.disagreements == []


# ---------------------------------------------------------------------------
# ExtAddress rendering (precision-lossy above 2**53)
# ---------------------------------------------------------------------------

class TestExtAddressFormatting:
    def test_lossy_value_gets_approx_marker(self):
        diag = _parse_fixture(0x2E)
        neighbour = diag.neighbours[0]
        assert neighbour.ext_address >= 2 ** 53
        assert format_ext_address(neighbour.ext_address).startswith("≈")

    def test_small_value_gets_no_marker(self):
        assert not format_ext_address(0x1234).startswith("≈")
        assert format_ext_address(0x1234) == "0x0000000000001234"

    def test_none_is_unknown(self):
        assert format_ext_address(None) == "unknown"


# ---------------------------------------------------------------------------
# Adversarial: never raise, degrade to flags/None instead
# ---------------------------------------------------------------------------

class TestDegradation:
    def test_missing_routing_role_returns_none(self):
        assert parse_node_diag(1, "ghost", True, {}) is None

    def test_missing_route_table_rev2_node(self):
        attrs = {
            "0/53/1": 5,  # Router, but no RouteTable at all (rev-2 IKEA shape)
            "0/53/0": 25,
        }
        diag = parse_node_diag(2, "rev2 router", True, attrs)
        assert diag is not None
        assert diag.routes == []
        assert diag.router_id is None
        # B2.1: NeighborTable was never cached either (absent, not empty) — this
        # must NOT be read as "0 neighbours" (which used to fire a false BAD
        # single_neighbour flag); it degrades to an info "not yet available"
        # flag instead, and single_neighbour must not fire on unknown data.
        assert diag.neighbours_known is False
        codes = {f.code for f in health_flags(diag)}
        assert "single_neighbour" not in codes
        assert "neighbours_unknown" in codes

    def test_empty_neighbor_table(self):
        attrs = {"0/53/1": 2, "0/53/7": []}
        diag = parse_node_diag(3, "no neighbours", True, attrs)
        assert diag is not None
        assert diag.neighbours == []
        assert diag.parent is None
        assert diag.best_rssi is None

    def test_unavailable_node_flags_detached(self):
        attrs = {"0/53/1": 2}
        diag = parse_node_diag(4, "offline sensor", False, attrs)
        assert diag is not None
        assert diag.available is False
        codes = {(f.code, f.severity) for f in health_flags(diag)}
        assert ("detached", "bad") in codes

    def test_unassigned_role_flags_detached(self):
        attrs = {"0/53/1": 1}  # Unassigned
        diag = parse_node_diag(5, "unassigned", True, attrs)
        assert diag is not None
        codes = {(f.code, f.severity) for f in health_flags(diag)}
        assert ("detached", "bad") in codes

    def test_malformed_neighbour_struct_is_skipped_and_warned(self):
        attrs = {
            "0/53/1": 5,
            "0/53/7": [
                {"0": 1, "1": 2, "2": 3},  # missing required keys -> malformed
                {  # well-formed, must survive
                    "0": 999, "1": 1, "2": 4096, "3": 1, "4": 1, "5": 2,
                    "6": -50, "7": -50, "8": 0, "9": 0, "10": True, "11": True, "13": False,
                },
            ],
        }
        diag = parse_node_diag(6, "half-malformed", True, attrs)
        assert diag is not None
        assert len(diag.neighbours) == 1
        assert diag.neighbours[0].ext_address == 999
        # Plus a "RouteTable absent" warning (B2.1 extended the same absent-vs-
        # malformed distinction to RouteTable) — this attrs dict has no "0/53/8"
        # key at all, which is unrelated to the neighbour-struct warning below.
        assert any("neighbour[0]" in w for w in diag.parse_warnings)

    def test_malformed_route_struct_is_skipped_and_warned(self):
        attrs = {
            "0/53/1": 5,
            "0/53/8": [{"0": 1}],  # missing everything else
        }
        diag = parse_node_diag(7, "bad route", True, attrs)
        assert diag is not None
        assert diag.routes == []
        assert any("route[0]" in warning for warning in diag.parse_warnings)

    def test_non_list_neighbor_table_does_not_raise(self):
        attrs = {"0/53/1": 2, "0/53/7": "not-a-list"}
        diag = parse_node_diag(8, "weird server", True, attrs)
        assert diag is not None
        assert diag.neighbours == []

    def test_build_mesh_with_a_node_missing_everything(self):
        attrs = {"0/53/1": 5}  # router with no routes/neighbours at all
        diag = parse_node_diag(9, "bare router", True, attrs)
        mesh = build_mesh([diag])
        assert len(mesh.nodes) == 1
        assert mesh.nodes[0].router_id is None
        assert mesh.nodes[0].foreign is False


# ---------------------------------------------------------------------------
# render_report — smoke coverage
# ---------------------------------------------------------------------------

class TestRenderReport:
    def test_report_mentions_every_owned_node_and_the_foreign_leader(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        lines = render_report(mesh, diags)
        text = "\n".join(lines)
        for diag in diags:
            assert diag.name in text
        assert "Router 14" in text
        assert "leader" in text.lower()

    def test_report_lists_flags_and_disagreements(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        lines = render_report(mesh, diags)
        text = "\n".join(lines)
        assert "Flags" in text
        assert "single_neighbour" in text
        assert "Partition disagreements" in text
        assert "604415567" in text

    def test_report_lines_are_bounded_width(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        for line in render_report(mesh, diags):
            assert len(line) <= 120

    def test_empty_mesh_does_not_raise(self):
        mesh = build_mesh([])
        lines = render_report(mesh, [])
        assert isinstance(lines, list)
        assert any("Flags" in line for line in lines)

    def test_a_page_url_appends_a_visual_map_footer(self):
        """#339: discoverability — the report links the mesh page when the
        caller supplies one."""
        diags = _all_diags()
        mesh = build_mesh(diags)
        lines = render_report(mesh, diags, page_url="http://jarvis.local:8176/message/ours/thread/")
        text = "\n".join(lines)
        assert "Visual map: http://jarvis.local:8176/message/ours/thread/" in text

    def test_no_page_url_means_no_footer(self):
        """This module stays pure — it never resolves a URL itself, and a
        caller with nothing to pass (or the IWS page, which never calls this
        function at all) must not get a broken "Visual map: None" line."""
        diags = _all_diags()
        mesh = build_mesh(diags)
        assert "Visual map" not in "\n".join(render_report(mesh, diags))
        assert "Visual map" not in "\n".join(render_report(mesh, diags, page_url=None))


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

def test_invalid_router_id_is_63():
    assert INVALID_ROUTER_ID == 63


def test_cluster_and_attribute_ids_match_ground_truth():
    assert CLUSTER_THREAD_DIAG == 0x0035
    assert ATTR_ROUTING_ROLE == 1
    assert ATTR_NETWORK_NAME == 2
    assert ATTR_NEIGHBOR_TABLE == 7
    assert ATTR_ROUTE_TABLE == 8
    assert ATTR_PARTITION_ID == 9
    assert ATTR_LEADER_ROUTER_ID == 13


@pytest.mark.parametrize("node_id", [0x27, 0x2E, 0x2F, 0x34, 0x38, 0x3F, 0x40])
def test_every_fixture_node_parses_without_raising(node_id):
    diag = _parse_fixture(node_id)
    assert diag.node_id == node_id
    # HealthFlag/dataclass smoke — never raises for any real fixture node.
    health_flags(diag)
    summary_states(diag)


# ---------------------------------------------------------------------------
# #334 post-review — B2.1: neighbours_known ("absent" is not "empty")
# ---------------------------------------------------------------------------

class TestNeighboursKnown:
    def test_none_neighbor_table_is_absent_not_empty(self):
        attrs = {"0/53/1": 5}  # RoutingRole only — NeighborTable never cached
        diag = parse_node_diag(10, "router", True, attrs)
        assert diag is not None
        assert diag.neighbours == []
        assert diag.neighbours_known is False
        assert any("NeighborTable absent" in w for w in diag.parse_warnings)

    def test_non_list_neighbor_table_is_malformed_not_empty(self):
        attrs = {"0/53/1": 5, "0/53/7": "garbage"}
        diag = parse_node_diag(11, "router", True, attrs)
        assert diag is not None
        assert diag.neighbours_known is False
        assert any("NeighborTable malformed" in w for w in diag.parse_warnings)

    def test_empty_list_neighbor_table_is_known(self):
        attrs = {"0/53/1": 5, "0/53/7": []}
        diag = parse_node_diag(12, "router", True, attrs)
        assert diag is not None
        assert diag.neighbours_known is True

    def test_unknown_table_suppresses_single_neighbour_and_weak_link(self):
        # Adversarial: "when could this return a false BAD and be wrong?" — a
        # router whose NeighborTable was never cached used to read exactly like
        # one with zero neighbours (bad single_neighbour) before this fix.
        attrs = {"0/53/1": 5}
        diag = parse_node_diag(13, "router", True, attrs)
        flags = health_flags(diag)
        codes = {f.code for f in flags}
        assert "single_neighbour" not in codes
        assert "weak_link" not in codes
        info_flag = next(f for f in flags if f.code == "neighbours_unknown")
        assert info_flag.severity == "info"

    def test_unknown_table_writes_only_thread_role(self):
        attrs = {"0/53/1": 5}
        diag = parse_node_diag(14, "router", True, attrs)
        assert set(summary_states(diag)) == {"threadRole"}


# ---------------------------------------------------------------------------
# #334 post-review — B5.1 CRITICAL: non-router with no parent must be "bad"
# ---------------------------------------------------------------------------

class TestNoParent:
    def test_non_router_with_known_empty_neighbour_table_is_bad_not_ok(self):
        # Adversarial: "when could this return ok and be wrong?" — a SED with a
        # KNOWN-empty NeighborTable (not absent — an actual empty list) has no
        # parent and cannot reach the mesh at all. Before this fix the
        # zero-neighbour check lived only inside `if diag.is_router`, so a
        # detached non-router reported clean.
        attrs = {"0/53/1": 2, "0/53/7": []}  # SleepyEndDevice, known-empty table
        diag = parse_node_diag(15, "orphan sed", True, attrs)
        assert diag is not None
        assert diag.neighbours_known is True
        assert diag.parent is None
        codes = {(f.code, f.severity) for f in health_flags(diag)}
        assert ("no_parent", "bad") in codes

    def test_router_never_gets_no_parent(self):
        attrs = {"0/53/1": 5, "0/53/7": []}
        diag = parse_node_diag(16, "lonely router", True, attrs)
        codes = {f.code for f in health_flags(diag)}
        assert "no_parent" not in codes
        assert "single_neighbour" in codes


# ---------------------------------------------------------------------------
# #334 post-review — B1.2/B2.2: router_id_unknown
# ---------------------------------------------------------------------------

class TestRouterIdUnknown:
    def test_ambiguous_self_entry_gets_router_id_unknown_warning(self):
        attrs = {
            "0/53/1": 5,
            "0/53/8": [
                {"0": 1, "1": 1024, "2": 1, "3": 63, "4": 0, "5": 0, "6": 0, "7": 0,
                 "8": True, "9": False},
                {"0": 2, "1": 2048, "2": 2, "3": 63, "4": 0, "5": 0, "6": 0, "7": 0,
                 "8": True, "9": False},
            ],
        }
        diag = parse_node_diag(20, "ambiguous router", True, attrs)
        codes = {(f.code, f.severity) for f in health_flags(diag)}
        assert ("router_id_unknown", "warn") in codes

    def test_two_owned_nodes_one_with_unresolved_router_id_documents_the_duplicate(self):
        # Pin the ACCEPTED consequence (B1.2) rather than hide it: a router
        # whose own router_id cannot be resolved is absent from
        # owned_key_by_router_id, so a sibling node's reference to that router
        # id creates a phantom foreign "rid:N" box even though it is really
        # this device — the flag is what makes that non-silent.
        ambiguous_attrs = {
            "0/53/1": 5,
            "0/53/8": [
                {"0": 1, "1": 1024, "2": 30, "3": 63, "4": 0, "5": 0, "6": 0, "7": 0,
                 "8": True, "9": False},
                {"0": 2, "1": 2048, "2": 31, "3": 63, "4": 0, "5": 0, "6": 0, "7": 0,
                 "8": True, "9": False},
            ],
        }
        sed_attrs = {
            "0/53/1": 2,
            "0/53/7": [{"0": 1, "1": 5, "2": 30 << 10, "5": 3, "6": -50, "7": -50,
                        "8": 0, "9": 0, "10": True, "11": True, "13": False}],
        }
        ambiguous = parse_node_diag(21, "ambiguous router", True, ambiguous_attrs)
        sed = parse_node_diag(22, "sed pointing at router 30", True, sed_attrs)
        mesh = build_mesh([ambiguous, sed])
        assert any(n.foreign and n.router_id == 30 for n in mesh.nodes), "the accepted phantom duplicate"
        assert any(f.code == "router_id_unknown" for f in mesh.flags)


# ---------------------------------------------------------------------------
# #334 post-review — B2.3: an unparseable RoutingRole must not drop the node
# ---------------------------------------------------------------------------

class TestUnreadableRoutingRole:
    def test_unparseable_routing_role_with_other_cluster_keys_still_appears(self):
        # Ground truth 2026-09-01: a real fixture-shaped node whose RoutingRole
        # came back as the STRING "Router" (matter-server's own serialisation
        # garbling it) must not vanish the way a genuine Wi-Fi node correctly
        # does — it still has other cluster-0x35 keys, so it is a Thread node.
        attrs = {"0/53/1": "Router", "0/53/0": 25}
        diag = parse_node_diag(30, "garbled role", True, attrs)
        assert diag is not None
        assert diag.role == ROLE_UNSPECIFIED
        assert diag.read_error == "RoutingRole unreadable"
        mesh = build_mesh([diag])
        assert (30, "RoutingRole unreadable") in mesh.unreadable

    def test_wifi_node_with_no_thread_cluster_at_all_still_returns_none(self):
        attrs = {"0/40/1": "Some Vendor"}  # BasicInformation only — no 0x35 key at all
        assert parse_node_diag(31, "wifi plug", True, attrs) is None


# ---------------------------------------------------------------------------
# #334 post-review — B3.5a / B3.4: majority computation
# ---------------------------------------------------------------------------

def _bare_diag(node_id, *, partition_id, source="cache", role=5, leader_router_id=None):
    return NodeDiag(
        node_id=node_id, name=f"n{node_id}", available=True, role=role, channel=None,
        network_name="", pan_id=None, ext_pan_id=None, partition_id=partition_id,
        leader_router_id=leader_router_id, neighbours=[], routes=[], counters={}, source=source,
    )


class TestMajorityFromLivePool:
    def test_majority_partition_uses_only_the_live_pool_when_any_diag_is_live(self):
        # Two CACHED diags agree on partition 1, one LIVE diag alone reports
        # partition 2 — if the majority pool included the cached diags too, 1
        # would win 2-to-1; it must not, because a live read is exactly what
        # disproved a stale-cache "majority" on 2026-09-01.
        cached_a = _bare_diag(1, partition_id=1, source="cache")
        cached_b = _bare_diag(2, partition_id=1, source="cache")
        live_c = _bare_diag(3, partition_id=2, source="live", role=2)
        mesh = build_mesh([cached_a, cached_b, live_c])
        assert mesh.majority_partition == 2


class TestPartitionTie:
    def test_two_node_tie_reversed_order_has_no_majority(self):
        diag_a = _bare_diag(1, partition_id=100, source="live")
        diag_b = _bare_diag(2, partition_id=200, source="live")
        mesh_forward = build_mesh([diag_a, diag_b])
        mesh_reversed = build_mesh([diag_b, diag_a])
        assert mesh_forward.majority_partition is None
        assert mesh_reversed.majority_partition is None
        assert len(mesh_forward.disagreements) == 2
        assert len(mesh_reversed.disagreements) == 2

    def test_tie_header_says_no_majority_and_split(self):
        diag_a = _bare_diag(1, partition_id=100, source="live")
        diag_b = _bare_diag(2, partition_id=200, source="live")
        mesh = build_mesh([diag_a, diag_b])
        text = partition_header_text(mesh, [diag_a, diag_b])
        assert "no majority" in text
        assert "100" in text and "200" in text
        assert "split" in text


# ---------------------------------------------------------------------------
# #334 post-review — A1: partition header presentation
# ---------------------------------------------------------------------------

class TestPartitionHeaderText:
    def test_majority_with_disagreement_matches_the_documented_example(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        text = partition_header_text(mesh, diags)
        assert text == (
            'Partition 1388375940 (leader router 14) — 2 cached node(s) report a '
            'different partition, see "Unreadable / stale"'
        )

    def test_no_disagreement_omits_the_trailing_clause(self):
        diag = _bare_diag(1, partition_id=42, leader_router_id=7)
        mesh = build_mesh([diag])
        text = partition_header_text(mesh, [diag])
        assert text == "Partition 42 (leader router 7)"
        assert "cached node" not in text

    def test_two_plus_live_partitions_says_split_even_with_a_majority(self):
        live_a = _bare_diag(1, partition_id=1, source="live")
        live_b = _bare_diag(2, partition_id=1, source="live")
        live_c = _bare_diag(3, partition_id=2, source="live", role=2)
        mesh = build_mesh([live_a, live_b, live_c])
        assert mesh.majority_partition == 1  # a real majority exists...
        text = partition_header_text(mesh, [live_a, live_b, live_c])
        assert "split" in text  # ...but 2+ LIVE nodes still disagree, so say so

    def test_report_render_uses_the_same_text(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        lines = render_report(mesh, diags)
        assert lines[1] == partition_header_text(mesh, diags)


# ---------------------------------------------------------------------------
# #334 post-review — A2: RLOC -> name resolution
# ---------------------------------------------------------------------------

class TestRlocResolution:
    def test_high_frame_error_message_names_the_target(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        flag = next(f for f in mesh.flags if f.code == "high_frame_error")
        assert re.search(r"to .+ \(0x[0-9A-Fa-f]{4}\)", flag.message)

    def test_report_neighbour_lines_name_the_target(self):
        diags = _all_diags()
        mesh = build_mesh(diags)
        text = "\n".join(render_report(mesh, diags))
        # 0x3F's neighbour router 62 is owned node 0x34, named "GRILLPLATS Plug".
        assert "GRILLPLATS Plug (0xF800)" in text

    def test_unresolved_rloc_falls_back_to_none(self):
        assert rloc_label(build_mesh([]), 0x1234) is None


# ---------------------------------------------------------------------------
# #334 post-review — A3/B4.2/B5.7: stale wording and REED coverage
# ---------------------------------------------------------------------------

class TestStaleWording:
    def test_reed_source_cache_gets_stale_info_flag(self):
        # B5.7: "sleepy/live-read candidate" is defined once (NodeDiag.is_router)
        # — a REED must get the same staleness note a SED/ED does; it used to be
        # silently skipped.
        diag = _parse_fixture(0x40)  # REED, source defaults "cache"
        assert diag.role_name == "Reed"
        assert any(f.code == "stale" and f.severity == "info" for f in health_flags(diag))

    def test_matter_server_cache_wording(self):
        diag = _parse_fixture(0x2F)
        stale = next(f for f in health_flags(diag) if f.code == "stale")
        assert "matter-server's cache" in stale.message
        assert "Thread's own cache" not in stale.message


# ---------------------------------------------------------------------------
# #334 post-review — B3.12: synthetic cases the fixture alone can't cover
# ---------------------------------------------------------------------------

class TestSyntheticCases:
    def test_owned_leader_role_6_is_leader_and_not_foreign(self):
        attrs = {
            "0/53/1": 6,  # Leader
            "0/53/13": 5,
            "0/53/8": [{"0": 1, "1": 5 << 10, "2": 5, "3": 63, "4": 0, "5": 0, "6": 0,
                        "7": 0, "8": True, "9": False}],
        }
        diag = parse_node_diag(50, "owned leader", True, attrs)
        assert diag is not None
        assert diag.role_name == "Leader"
        mesh = build_mesh([diag])
        node = mesh.nodes[0]
        assert node.is_leader is True
        assert node.foreign is False

    def test_parent_prefers_non_child_then_best_average_rssi(self):
        attrs = {
            "0/53/1": 2,  # SED
            "0/53/7": [
                # shaped like a candidate parent but IS our child — must be skipped
                {"0": 1, "1": 5, "2": 1024, "5": 3, "6": -10, "7": -10,
                 "8": 0, "9": 0, "10": True, "11": True, "13": True},
                # two genuine non-child candidates — the better average wins
                {"0": 2, "1": 5, "2": 2048, "5": 3, "6": -70, "7": -70,
                 "8": 0, "9": 0, "10": True, "11": True, "13": False},
                {"0": 3, "1": 5, "2": 3072, "5": 3, "6": -30, "7": -30,
                 "8": 0, "9": 0, "10": True, "11": True, "13": False},
            ],
        }
        diag = parse_node_diag(51, "picky sed", True, attrs)
        assert diag is not None
        parent = diag.parent
        assert parent is not None
        assert parent.rloc16 == 3072  # the non-child with the best (-30) average
        assert not parent.is_child

    def test_two_owned_seds_fully_identify_a_routers_children_zero_anonymous(self):
        router_attrs = {
            "0/53/1": 5,
            "0/53/8": [{"0": 1, "1": 5 << 10, "2": 5, "3": 63, "4": 0, "5": 0, "6": 0,
                        "7": 0, "8": True, "9": False}],
            "0/53/7": [
                {"0": 10, "1": 5, "2": 5 << 10 | 1, "5": 3, "6": -50, "7": -50,
                 "8": 0, "9": 0, "10": True, "11": True, "13": True},
                {"0": 11, "1": 5, "2": 5 << 10 | 2, "5": 3, "6": -50, "7": -50,
                 "8": 0, "9": 0, "10": True, "11": True, "13": True},
            ],
        }
        router = parse_node_diag(52, "router with 2 children", True, router_attrs)

        def _sed(node_id, parent_rloc16):
            return parse_node_diag(node_id, f"sed {node_id}", True, {
                "0/53/1": 2,
                "0/53/7": [{"0": node_id, "1": 5, "2": parent_rloc16, "5": 3, "6": -50, "7": -50,
                            "8": 0, "9": 0, "10": True, "11": True, "13": False}],
            })

        sed_a = _sed(60, 5 << 10 | 1)
        sed_b = _sed(61, 5 << 10 | 2)
        mesh = build_mesh([router, sed_a, sed_b])
        assert not any(n.key.startswith("child:") for n in mesh.nodes)

    def test_far_from_leader_boundary(self):
        def _diag_with_path_cost(cost):
            return NodeDiag(
                node_id=1, name="x", available=True, role=5, channel=None,
                network_name="", pan_id=None, ext_pan_id=None, partition_id=None,
                leader_router_id=9, neighbours=[],
                routes=[Route(ext_address=1, rloc16=9 << 10, router_id=9, next_hop=1,
                               path_cost=cost, lqi_in=0, lqi_out=0, age_s=0,
                               allocated=True, link_established=True)],
                counters={},
            )
        assert not any(f.code == "far_from_leader" for f in health_flags(_diag_with_path_cost(2)))
        assert any(f.code == "far_from_leader" for f in health_flags(_diag_with_path_cost(3)))


# ---------------------------------------------------------------------------
# #334 post-review — B3.7: high_frame_error / RSSI boundary edges
# ---------------------------------------------------------------------------

def _diag_with_neighbour(*, frame_error_pct=0, avg_rssi=None, last_rssi=None, role=5):
    return NodeDiag(
        node_id=1, name="x", available=True, role=role, channel=None,
        network_name="", pan_id=None, ext_pan_id=None, partition_id=None,
        leader_router_id=None,
        neighbours=[Neighbour(
            ext_address=1, age_s=0, rloc16=1024, lqi=0, avg_rssi=avg_rssi, last_rssi=last_rssi,
            frame_error_pct=frame_error_pct, message_error_pct=0,
            rx_on_when_idle=True, full_thread_device=True, is_child=False,
        )],
        routes=[], counters={},
    )


class TestFrameErrorAndRssiBoundaries:
    def test_frame_error_20_is_no_flag_21_is_warn(self):
        assert not any(f.code == "high_frame_error" for f in health_flags(_diag_with_neighbour(frame_error_pct=20)))
        assert any(f.code == "high_frame_error" for f in health_flags(_diag_with_neighbour(frame_error_pct=21)))

    @pytest.mark.parametrize("rssi,expected", [
        (-80, None), (-81, "warn"), (-90, "warn"), (-91, "bad"),
    ])
    def test_rssi_band_edges(self, rssi, expected):
        flags = health_flags(_diag_with_neighbour(avg_rssi=rssi, last_rssi=rssi))
        weak_link = next((f for f in flags if f.code == "weak_link"), None)
        if expected is None:
            assert weak_link is None
        else:
            assert weak_link is not None and weak_link.severity == expected


# ---------------------------------------------------------------------------
# #334 post-review — B2.5 / B5.2: Mesh.warnings
# ---------------------------------------------------------------------------

class TestMeshWarnings:
    def test_parse_warnings_aggregate_into_mesh_warnings(self):
        attrs = {
            "0/53/1": 5,
            "0/53/7": [{"0": 1, "1": 2, "2": 3}],  # malformed neighbour entry
        }
        diag = parse_node_diag(70, "half-malformed", True, attrs)
        mesh = build_mesh([diag])
        assert (70, diag.parse_warnings[0]) in mesh.warnings

    def test_unattributed_children_note_is_order_independent(self):
        def _mesh_for(child_order):
            router_attrs = {
                "0/53/1": 5,
                "0/53/8": [{"0": 1, "1": 5 << 10, "2": 5, "3": 63, "4": 0, "5": 0, "6": 0,
                            "7": 0, "8": True, "9": False}],
                "0/53/7": child_order,
            }
            router = parse_node_diag(71, "router", True, router_attrs)
            sed = parse_node_diag(72, "sed", True, {
                "0/53/1": 2,
                "0/53/7": [{"0": 72, "1": 5, "2": 5 << 10 | 1, "5": 3, "6": -50, "7": -50,
                            "8": 0, "9": 0, "10": True, "11": True, "13": False}],
            })
            return build_mesh([router, sed])

        child1 = {"0": 10, "1": 5, "2": 5 << 10 | 1, "5": 3, "6": -50, "7": -50,
                  "8": 0, "9": 0, "10": True, "11": True, "13": True}
        child2 = {"0": 11, "1": 5, "2": 5 << 10 | 2, "5": 3, "6": -60, "7": -60,
                  "8": 0, "9": 0, "10": True, "11": True, "13": True}
        mesh_a = _mesh_for([child1, child2])
        mesh_b = _mesh_for([child2, child1])

        for mesh in (mesh_a, mesh_b):
            unattributed = [n for n in mesh.nodes if n.key.endswith("#unattributed")]
            assert len(unattributed) == 1
            assert unattributed[0].name == "1 unattributed child(ren)"
            assert any("could not be matched to an Indigo device" in msg for _nid, msg in mesh.warnings)

        assert set(mesh_a.nodes) == set(mesh_b.nodes)
        assert set(mesh_a.links) == set(mesh_b.links)

    def test_identified_zero_keeps_individual_rloc_labels(self):
        # When NONE of a router's children are attributed yet, individual
        # "child:0x.." labels are still safe (nothing to collide with).
        router_attrs = {
            "0/53/1": 5,
            "0/53/8": [{"0": 1, "1": 5 << 10, "2": 5, "3": 63, "4": 0, "5": 0, "6": 0,
                        "7": 0, "8": True, "9": False}],
            "0/53/7": [{"0": 10, "1": 5, "2": 5 << 10 | 1, "5": 3, "6": -50, "7": -50,
                        "8": 0, "9": 0, "10": True, "11": True, "13": True}],
        }
        router = parse_node_diag(73, "router", True, router_attrs)
        mesh = build_mesh([router])
        assert any(n.key == "child:0x1401" for n in mesh.nodes)
        assert not any(n.key.endswith("#unattributed") for n in mesh.nodes)


# ---------------------------------------------------------------------------
# #334 post-review — B1.4 / B1.5: key constructors, Literal typing, frozen Mesh
# ---------------------------------------------------------------------------

def test_router_and_child_key_constructors():
    from thread_mesh import _child_key, _router_key  # pylint: disable=import-outside-toplevel
    assert _router_key(14) == "rid:14"
    assert _child_key(0xF804) == "child:0xF804"


def test_mesh_is_frozen():
    mesh = build_mesh([])
    with pytest.raises(dataclasses.FrozenInstanceError):
        mesh.channel = 99


# ---------------------------------------------------------------------------
# #334 post-review — B4.14: counter 20 (BetterPartitionAttachAttemptCount)
# ---------------------------------------------------------------------------

def test_better_partition_attach_attempt_count_is_captured():
    # BILRESA (0x2E) is rev-3 firmware and its AttributeList carries attribute
    # 20 (verified in the fixture) — the counter must actually be captured now
    # that it is in _COUNTER_ATTRS, not silently dropped.
    diag = _parse_fixture(0x2E)
    assert diag.counters.get("better_partition_attach_attempts") == 0


# ---------------------------------------------------------------------------
# #344 — humanise_age
# ---------------------------------------------------------------------------

class TestHumaniseAge:
    def test_under_a_minute_is_just_now(self):
        assert humanise_age(30) == "just now"

    def test_negative_is_just_now(self):
        # Clock skew must never render as a fake age.
        assert humanise_age(-5) == "just now"

    def test_minutes(self):
        assert humanise_age(45 * 60) == "45m"

    def test_hours(self):
        assert humanise_age(2 * 3600) == "2h"

    def test_days(self):
        assert humanise_age(6 * 86400) == "6d"

    def test_largest_single_unit_not_compound(self):
        # 25 hours is "1d", never "1d 1h" — a diagnostic glance, not a
        # duration formatter (see the function's own docstring).
        assert humanise_age(25 * 3600) == "1d"


# ---------------------------------------------------------------------------
# #344 — freshness_text
# ---------------------------------------------------------------------------

def _diag_with_freshness(*, last_interview=None, last_event_seen=None):
    return NodeDiag(
        node_id=1, name="x", available=True, role=5, channel=None,
        network_name="", pan_id=None, ext_pan_id=None, partition_id=None,
        leader_router_id=None, neighbours=[], routes=[], counters={},
        last_interview=last_interview, last_event_seen=last_event_seen,
    )


class TestFreshnessText:
    NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)

    def test_both_known(self):
        diag = _diag_with_freshness(
            last_interview=self.NOW - timedelta(days=6),
            last_event_seen=(self.NOW - timedelta(hours=2)).timestamp(),
        )
        assert freshness_text(diag, self.NOW) == "interviewed 6d ago · last 0x35 update 2h ago"

    def test_interview_unknown(self):
        diag = _diag_with_freshness(last_event_seen=(self.NOW - timedelta(hours=2)).timestamp())
        assert freshness_text(diag, self.NOW) == "interviewed — · last 0x35 update 2h ago"

    def test_event_unknown(self):
        diag = _diag_with_freshness(last_interview=self.NOW - timedelta(days=6))
        assert freshness_text(diag, self.NOW) == "interviewed 6d ago · last 0x35 update —"

    def test_both_unknown(self):
        diag = _diag_with_freshness()
        assert freshness_text(diag, self.NOW) == "interviewed — · last 0x35 update —"


# ---------------------------------------------------------------------------
# #344 — the stale flag's message gains an age when the interview is known
# ---------------------------------------------------------------------------

class TestStaleFlagWithInterviewAge:
    NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)

    def test_stale_message_includes_the_age_when_known(self):
        diag = _parse_fixture(0x2F)  # cached SED, gets the "stale" info flag
        diag.last_interview = self.NOW - timedelta(days=6)
        stale = next(f for f in health_flags(diag, self.NOW) if f.code == "stale")
        assert "baseline interview 6d ago" in stale.message
        assert "matter-server's cache can be hours stale" in stale.message

    def test_stale_message_keeps_the_old_wording_when_interview_is_unknown(self):
        diag = _parse_fixture(0x2F)
        stale = next(f for f in health_flags(diag, self.NOW) if f.code == "stale")
        assert "baseline interview" not in stale.message
        assert stale.message == f"{diag.name}: cached — matter-server's cache can be hours stale for sleepy devices"

    def test_now_defaults_to_the_real_clock_without_raising(self):
        # health_flags(diag) with no `now` at all must still work — every
        # existing caller/test in this file relies on that default.
        diag = _parse_fixture(0x2F)
        diag.last_interview = self.NOW - timedelta(days=1)
        stale = next(f for f in health_flags(diag) if f.code == "stale")
        assert "baseline interview" in stale.message


# ---------------------------------------------------------------------------
# #344 — render_report's per-node cache-age line
# ---------------------------------------------------------------------------

class TestRenderReportCacheLine:
    NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)

    def test_node_block_contains_the_cache_freshness_line(self):
        diags = _all_diags()
        mesh = build_mesh(diags, self.NOW)
        lines = render_report(mesh, diags, now=self.NOW)
        assert any(line.strip().startswith("cache: interviewed") for line in lines)
        for line in lines:
            assert len(line) <= 120

    def test_freshness_line_reflects_a_known_interview_age(self):
        diags = _all_diags()
        diags[0].last_interview = self.NOW - timedelta(days=6)
        mesh = build_mesh(diags, self.NOW)
        lines = render_report(mesh, diags, now=self.NOW)
        text = "\n".join(lines)
        assert "cache: interviewed 6d ago" in text
