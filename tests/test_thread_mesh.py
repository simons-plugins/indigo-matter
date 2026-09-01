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

import json
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
    HealthFlag,
    Mesh,
    MeshLink,
    MeshNode,
    Neighbour,
    NodeDiag,
    Route,
    build_mesh,
    format_ext_address,
    health_flags,
    parse_node_diag,
    render_report,
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
        assert 1388375940 in mesh.partitions.values()
        leader = next(n for n in mesh.nodes if n.is_leader)
        assert leader.foreign is True
        assert leader.router_id == 14
        assert leader.rloc16 == 0x3800
        assert "leader" in leader.name.lower()

    def test_0x3f_is_router_51_wait_router_23_with_one_child_and_three_neighbours(self):
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

    def test_unknown_rssi_reports_minus_128(self):
        diag = NodeDiag(
            node_id=1, name="lonely router", available=True, role=5, channel=None,
            network_name="", pan_id=None, ext_pan_id=None, partition_id=None,
            leader_router_id=None, neighbours=[], routes=[], counters={},
        )
        assert summary_states(diag)["threadLinkRssi"] == -128


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
        # Still degrades to a flag, not a crash: 0 neighbours -> bad single_neighbour.
        codes = {f.code for f in health_flags(diag)}
        assert "single_neighbour" in codes

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
        assert len(diag.parse_warnings) == 1
        assert "neighbour[0]" in diag.parse_warnings[0]

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
