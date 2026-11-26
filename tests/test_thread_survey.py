"""Tests for the shared Thread mesh survey coroutine — #334.

``thread_survey`` is deliberately the ONE place that decides how to combine
matter-server's cache with a live refresh; both the menu diagnostic
(``tests/test_diagnostics_menu.py``) and the IWS page (``tests/test_thread_page.py``)
reuse it unchanged. This file owns the coroutine itself: concurrency, per-node
timeout handling, router live-reads, naming, and the "a get_nodes() failure raises"
contract, plus (post-review, #334) the :class:`~thread_survey.Survey` result shape,
the live-refresh merge's own failure handling, and the sync bridge's timeout cancel.

Fixture: ``tests/fixtures/thread_mesh/nodes.json`` — the same 7 real nodes
``test_thread_mesh.py`` parses. Routers (RoutingRole 5/Router): 0x27, 0x34, 0x3F.
Non-routers (SED/REED): 0x2E, 0x2F, 0x38, 0x40. The live pass reads all seven (#348).
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from thread_mesh import (ATTR_LEADER_ROUTER_ID, ATTR_NEIGHBOR_TABLE, ATTR_PARTITION_ID, ATTR_ROUTE_TABLE,
                         ATTR_ROUTING_ROLE, build_mesh, health_flags, parse_node_diag,
                         render_report)
from thread_survey import Survey, _parse_timestamp, run_survey, survey_thread_nodes

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "thread_mesh" / "nodes.json"
ROUTER_IDS = {0x27, 0x34, 0x3F}
NON_ROUTER_IDS = {0x2E, 0x2F, 0x38, 0x40}


def _fixture_nodes() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class FakeMatter:
    """A ``MatterClient`` stand-in: ``get_nodes`` returns canned raw nodes,
    ``read`` answers (or fails) per node id, and records every node it was asked
    to read — the evidence for "routers are never touched"."""

    def __init__(self, nodes=None, *, timeout_nodes=(), error_nodes=(), forbidden_nodes=(),
                 get_nodes_error=None, overrides=None):
        self.nodes = _fixture_nodes() if nodes is None else nodes
        #: ``{(node_id, attribute): value}`` — what a LIVE read answers instead of
        #: the cached value (the thing a test needs to differ between cache and live).
        self._overrides = dict(overrides or {})
        self.read_calls: list[int] = []
        self._timeout_nodes = set(timeout_nodes)
        self._error_nodes = dict(error_nodes) if isinstance(error_nodes, dict) else \
            {n: RuntimeError("boom") for n in error_nodes}
        self._forbidden_nodes = set(forbidden_nodes)
        self._get_nodes_error = get_nodes_error

    async def get_nodes(self):
        if self._get_nodes_error is not None:
            raise self._get_nodes_error
        return self.nodes

    async def read(self, node_id, endpoint, cluster, attribute):
        self.read_calls.append(node_id)
        if node_id in self._forbidden_nodes:
            # "Make it fatal" (root CLAUDE.md): a live read reaching a node that
            # must never be touched raises rather than silently succeeding, so a
            # regression shows up as a read_error instead of passing quietly.
            raise AssertionError(f"read() must never touch node 0x{node_id:X}")
        if node_id in self._timeout_nodes:
            # Raised directly rather than actually sleeping past the timeout —
            # this is exactly what asyncio.wait_for raises on a real timeout, so
            # the handling code cannot tell the difference, and the test does
            # not have to wait out a real per_node_timeout.
            raise asyncio.TimeoutError()
        if node_id in self._error_nodes:
            raise self._error_nodes[node_id]
        if (node_id, attribute) in self._overrides:
            value = self._overrides[(node_id, attribute)]
            if isinstance(value, BaseException):
                raise value  # one attribute's read failing while its siblings answer
            return value
        raw = next(n for n in self.nodes if n["node_id"] == node_id)
        return raw["attributes"].get(f"{endpoint}/{cluster}/{attribute}")

    def __getattr__(self, name):
        # "Make it fatal" (ADR-0004, read-only diagnostics): only get_nodes()/read()
        # exist. Any write_attribute/send_command/anything else the survey might
        # reach for raises instead of quietly succeeding.
        raise AssertionError(f"the survey must only read; it touched matter.{name}")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Cache-only (liveSleepy=False)
# ---------------------------------------------------------------------------

def test_without_live_read_every_fixture_node_is_returned_from_cache():
    matter = FakeMatter(forbidden_nodes=ROUTER_IDS | NON_ROUTER_IDS)  # any read() call fails the test
    survey = _run(survey_thread_nodes(matter, live_read=False))
    assert {d.node_id for d in survey.diags} == ROUTER_IDS | NON_ROUTER_IDS
    assert all(d.source == "cache" for d in survey.diags)
    assert matter.read_calls == [], "liveSleepy=False must never call read()"


# ---------------------------------------------------------------------------
# Live refresh (liveSleepy=True)
# ---------------------------------------------------------------------------

def test_live_read_touches_routers_too():
    # #348: a router's NeighborTable/RouteTable are changes-omitted, so a subscription
    # never refreshes them and its cached tables go stale — the live pass must read
    # routers exactly like everything else.
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_read=True))
    assert set(matter.read_calls) == ROUTER_IDS | NON_ROUTER_IDS, "every node should have been live-read"
    assert all(d.source == "live" and d.read_error is None for d in survey.diags)


def test_a_successful_live_read_flips_the_diag_to_live_and_keeps_its_data():
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_read=True))
    by_id = {d.node_id: d for d in survey.diags}
    live = by_id[0x2E]  # BILRESA scroll wheel — SED, gets live-read
    assert live.source == "live"
    assert live.read_error is None
    cached = _direct_parse(0x2E)
    assert live.role == cached.role
    assert [n.rloc16 for n in live.neighbours] == [n.rloc16 for n in cached.neighbours]


def test_route_table_is_now_live_read_for_non_routers():
    # #334 post-review, B4.5: RouteTable is NOT a router-only structure — every
    # SED carries its own single-row RouteTable — so it is now part of the live
    # refresh; a live-refreshed non-router's routes come from the SAME live
    # read as everything else, not a stale cached RouteTable paired with a
    # fresh LeaderRouterId.
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_read=True))
    live = next(d for d in survey.diags if d.node_id == 0x2E)
    assert live.source == "live"
    assert len(live.routes) == 1
    assert live.routes[0].router_id == 51


def _direct_parse(node_id: int):
    raw = next(n for n in _fixture_nodes() if n["node_id"] == node_id)
    return parse_node_diag(node_id, "", raw["available"], raw["attributes"])


def test_a_timed_out_live_read_keeps_the_cached_diag_and_names_the_reason():
    matter = FakeMatter(timeout_nodes={0x2E})
    survey = _run(survey_thread_nodes(matter, live_read=True, per_node_timeout=5.0))
    by_id = {d.node_id: d for d in survey.diags}
    timed_out = by_id[0x2E]
    assert timed_out.source == "cache", "a failed live read must not discard the cached data"
    assert timed_out.read_error is not None and "timed out" in timed_out.read_error
    cached = _direct_parse(0x2E)
    assert timed_out.role == cached.role
    # a node whose live read succeeded is unaffected by a sibling's timeout
    assert by_id[0x2F].source == "live" and by_id[0x2F].read_error is None


def test_an_erroring_live_read_keeps_the_cached_diag_and_names_the_reason():
    matter = FakeMatter(error_nodes={0x38: ConnectionError("socket closed")})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x38)
    assert diag.source == "cache"
    assert diag.read_error is not None and "socket closed" in diag.read_error


def test_one_nodes_live_read_failure_never_drops_it_from_the_result():
    """A degraded node is still reported, per the module contract — never
    silently thinned out (root CLAUDE.md degradation-path convention)."""
    matter = FakeMatter(timeout_nodes={0x2E, 0x2F, 0x38, 0x40})
    survey = _run(survey_thread_nodes(matter, live_read=True, per_node_timeout=5.0))
    assert {d.node_id for d in survey.diags} == ROUTER_IDS | NON_ROUTER_IDS
    assert all(d.read_error for d in survey.diags if d.node_id in NON_ROUTER_IDS)


# ---------------------------------------------------------------------------
# #334 post-review — B3.3: a live NeighborTable of None/garbage must not
# become "live / 0 neighbours / ok"
# ---------------------------------------------------------------------------

def test_live_read_returning_none_for_neighbor_table_keeps_the_cached_diag():
    matter = FakeMatter()
    orig_read = matter.read

    async def _read(node_id, endpoint, cluster, attribute):
        from thread_mesh import ATTR_NEIGHBOR_TABLE  # pylint: disable=import-outside-toplevel
        if node_id == 0x2E and attribute == ATTR_NEIGHBOR_TABLE:
            return None
        return await orig_read(node_id, endpoint, cluster, attribute)

    matter.read = _read
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    # Adversarial: "when could this return live/0-neighbours/ok and be wrong?"
    # — exactly here, if the None answer were treated as "no neighbours".
    assert diag.source == "cache"
    assert diag.read_error == "live read returned no NeighborTable"
    cached = _direct_parse(0x2E)
    assert len(diag.neighbours) == len(cached.neighbours)  # untouched, still the cached table


def test_live_read_returning_a_non_list_for_neighbor_table_keeps_the_cached_diag():
    matter = FakeMatter()
    orig_read = matter.read

    async def _read(node_id, endpoint, cluster, attribute):
        from thread_mesh import ATTR_NEIGHBOR_TABLE  # pylint: disable=import-outside-toplevel
        if node_id == 0x2E and attribute == ATTR_NEIGHBOR_TABLE:
            return "garbage"
        return await orig_read(node_id, endpoint, cluster, attribute)

    matter.read = _read
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    assert diag.source == "cache"
    assert diag.read_error == "live read returned no NeighborTable"


# ---------------------------------------------------------------------------
# #348 — routers are live-read too: departed children, failed reads, degradation
# ---------------------------------------------------------------------------

DEPARTED_RLOC = 0x5C04  # 0x3F's cached child in the fixture


def _cached_neighbor_table(node_id):
    raw = next(n for n in _fixture_nodes() if n["node_id"] == node_id)
    return raw["attributes"][f"0/53/{ATTR_NEIGHBOR_TABLE}"]


def _no_departure_warning(diag):
    return not any("absent from this router's live neighbour table" in w for w in diag.parse_warnings)


def _mesh_names(survey):
    return [n.name for n in build_mesh(survey.diags).nodes]


def test_a_cached_child_absent_from_the_routers_live_table_draws_no_child_node():
    live_table = [e for e in _cached_neighbor_table(0x3F) if e["2"] != DEPARTED_RLOC]
    assert len(live_table) == 2, "sanity: the fixture's one child entry was removed"
    # Control: from cache alone the child IS drawn, so the assertion below can fail.
    cache_only = _run(survey_thread_nodes(FakeMatter(), live_read=False))
    assert "Child 0x5C04" in _mesh_names(cache_only)

    matter = FakeMatter(overrides={(0x3F, ATTR_NEIGHBOR_TABLE): live_table})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    router = next(d for d in survey.diags if d.node_id == 0x3F)
    assert router.source == "live" and router.read_error is None
    assert "Child 0x5C04" not in _mesh_names(survey)
    # Said, not silent: the departure is reported, with the same wording both surfaces use.
    assert any("0x5C04" in warning and "absent from this router's live neighbour table" in warning
               for warning in router.parse_warnings)
    mesh = build_mesh(survey.diags)
    assert any(node_id == 0x3F and "0x5C04" in text for node_id, text in mesh.warnings)


def test_a_cached_child_still_in_the_live_table_is_kept_and_not_called_departed():
    matter = FakeMatter()  # live table == cached table
    survey = _run(survey_thread_nodes(matter, live_read=True))
    router = next(d for d in survey.diags if d.node_id == 0x3F)
    assert [c.rloc16 for c in router.children] == [DEPARTED_RLOC]
    assert _no_departure_warning(router)
    assert "Child 0x5C04" in _mesh_names(survey)


def test_a_router_whose_live_read_times_out_is_unreadable_stale_and_keeps_its_cached_children():
    matter = FakeMatter(timeout_nodes={0x3F})
    survey = _run(survey_thread_nodes(matter, live_read=True, per_node_timeout=5.0))
    by_id = {d.node_id: d for d in survey.diags}
    router = by_id[0x3F]
    assert router.source == "cache" and "timed out" in router.read_error
    # A failed read is "we don't know", never "no children": the cached child stays,
    # and nothing claims it departed.
    assert [c.rloc16 for c in router.children] == [DEPARTED_RLOC]
    assert _no_departure_warning(router)
    assert "Child 0x5C04" in _mesh_names(survey)

    mesh = build_mesh(survey.diags)
    assert (0x3F, router.read_error) in mesh.unreadable
    stale = [f for f in mesh.flags if f.code == "stale" and f.node_key == "node:0x3F"]
    assert len(stale) == 1, "a failed-read router must carry the stale note (#348)"
    assert "live read" in stale[0].message
    report = "\n".join(render_report(mesh, survey.diags))
    assert "freshness: cached" in report
    assert "0x3F: live read timed out" in report

    # One router failing must not touch another router's (or a leaf's) result.
    for node_id in (0x27, 0x34, 0x2E, 0x40):
        assert by_id[node_id].source == "live" and by_id[node_id].read_error is None


def test_two_routers_failing_differently_each_get_their_own_reason():
    matter = FakeMatter(timeout_nodes={0x3F}, error_nodes={0x27: ConnectionError("socket closed")})
    survey = _run(survey_thread_nodes(matter, live_read=True, per_node_timeout=5.0))
    by_id = {d.node_id: d for d in survey.diags}
    assert "timed out" in by_id[0x3F].read_error
    assert "socket closed" in by_id[0x27].read_error
    assert by_id[0x34].source == "live" and by_id[0x34].read_error is None


def test_a_live_neighbor_table_of_none_on_a_router_keeps_the_refusal_not_zero_neighbours():
    matter = FakeMatter(overrides={(0x3F, ATTR_NEIGHBOR_TABLE): None})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    router = next(d for d in survey.diags if d.node_id == 0x3F)
    assert router.source == "cache"
    assert router.read_error == "live read returned no NeighborTable"
    assert len(router.neighbours) == 3 and router.neighbours_known
    assert [c.rloc16 for c in router.children] == [DEPARTED_RLOC], "None is not 'no children'"
    assert _no_departure_warning(router), "a refused table must never be reported as departures"
    assert "Child 0x5C04" in _mesh_names(survey)
    flags = {(f.code, f.severity) for f in health_flags(router)}
    assert ("single_neighbour", "bad") not in flags, "must not become '0 neighbours'"
    assert "stale" in {f.code for f in health_flags(router)}


def test_a_garbage_live_neighbor_table_on_a_router_reports_no_departures():
    matter = FakeMatter(overrides={(0x3F, ATTR_NEIGHBOR_TABLE): "garbage"})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    router = _assert_failed_read_kept_cache(survey, expect_in_error="live read returned no NeighborTable")
    assert router.read_error == "live read returned no NeighborTable"


def test_the_live_pass_with_zero_routers_still_reads_every_node():
    nodes = [n for n in _fixture_nodes() if n["node_id"] in NON_ROUTER_IDS]
    matter = FakeMatter(nodes=nodes, forbidden_nodes=ROUTER_IDS)
    survey = _run(survey_thread_nodes(matter, live_read=True))
    assert {d.node_id for d in survey.diags} == NON_ROUTER_IDS
    assert set(matter.read_calls) == NON_ROUTER_IDS
    assert all(d.source == "live" for d in survey.diags)


def test_a_live_pass_that_fails_everywhere_does_not_produce_a_text_report_that_looks_live():
    # The text report; the page has its own (tests/test_thread_page.py).
    # Controller down mid-survey: get_nodes() answered (from cache) but every read fails.
    matter = FakeMatter(error_nodes={n: ConnectionError("matter-server went away")
                                     for n in ROUTER_IDS | NON_ROUTER_IDS})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    assert len(survey.diags) == 7
    assert all(d.source == "cache" and "went away" in d.read_error for d in survey.diags)
    mesh = build_mesh(survey.diags)
    assert {node_id for node_id, _ in mesh.unreadable} == ROUTER_IDS | NON_ROUTER_IDS
    stale_keys = {f.node_key for f in mesh.flags if f.code == "stale"}
    assert stale_keys == {f"node:0x{n:X}" for n in ROUTER_IDS | NON_ROUTER_IDS}, "routers included"
    report = "\n".join(render_report(mesh, survey.diags))
    assert "freshness: live" not in report
    assert report.count("freshness: cached") == 7
    assert not any("live neighbour table" in w for _, w in mesh.warnings)


# ---------------------------------------------------------------------------
# #348 review — the governing rule: a live answer replaces the cache only when it
# is USABLE; anything else is a failed read (cached diag untouched, read_error says
# why, source stays "cache"), and read_error is never set while source == "live".
# ---------------------------------------------------------------------------

def _assert_failed_read_kept_cache(survey, node_id=0x3F, *, expect_in_error):
    diag = next(d for d in survey.diags if d.node_id == node_id)
    cached = _direct_parse(node_id)
    assert diag.source == "cache", "an unusable live answer must not flip the node to live"
    assert diag.read_error is not None and expect_in_error in diag.read_error
    assert diag.role == cached.role and diag.is_router
    assert diag.router_id == cached.router_id and diag.partition_id == cached.partition_id
    assert diag.leader_router_id == cached.leader_router_id
    assert len(diag.neighbours) == len(cached.neighbours) and len(diag.routes) == len(cached.routes)
    assert [c.rloc16 for c in diag.children] == [DEPARTED_RLOC]
    assert _no_departure_warning(diag)
    mesh = build_mesh(survey.diags)
    assert "Child 0x5C04" in [n.name for n in mesh.nodes], "cached children stay drawn"
    assert any(node_id == 0x3F for node_id, _ in mesh.unreadable)
    assert any(f.code == "stale" and f.node_key == "node:0x3F" for f in mesh.flags)
    return diag


@pytest.mark.parametrize("bad_role", [None, "Router", {}, [], True])
def test_an_unusable_live_routing_role_is_a_failed_read_not_an_unspecified_node(bad_role):
    matter = FakeMatter(overrides={(0x3F, ATTR_ROUTING_ROLE): bad_role})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = _assert_failed_read_kept_cache(survey, expect_in_error="live read returned no usable RoutingRole")
    assert diag.read_error == "live read returned no usable RoutingRole"
    assert diag.role_name == "Router"


def test_the_unparseable_role_path_never_leaves_read_error_on_a_live_diag():
    matter = FakeMatter(overrides={(0x3F, ATTR_ROUTING_ROLE): None})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    assert not [d for d in survey.diags if d.source == "live" and d.read_error]


def test_an_empty_live_neighbour_table_from_an_unchanged_attached_router_is_a_failed_read():
    # Same role live as cached (Router), cache had neighbours, live says "none": far
    # likelier a failed read than "everyone left" — so the cached table is shown.
    matter = FakeMatter(overrides={(0x3F, ATTR_NEIGHBOR_TABLE): []})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = _assert_failed_read_kept_cache(survey, expect_in_error="live neighbour table came back empty")
    assert "cached" in diag.read_error and "none could be parsed" not in diag.read_error
    assert ("single_neighbour", "bad") not in {(f.code, f.severity) for f in health_flags(diag)}


@pytest.mark.parametrize("role", [1, 2, 3, 4, 5, 6])
@pytest.mark.parametrize("rows", [[{"0": 1}], [{"bad": "row"}, "garbage", None]])
def test_an_all_malformed_live_neighbour_table_is_a_failed_read_under_every_role(role, rows):
    # Rows existed but none parsed: unparseable, not empty — whatever role is claimed.
    matter = FakeMatter(overrides={(0x3F, ATTR_NEIGHBOR_TABLE): rows, (0x3F, ATTR_ROUTING_ROLE): role})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x3F)
    assert diag.source == "cache" and diag.role_name == "Router"
    assert "none could be parsed" in diag.read_error and "came back empty" not in diag.read_error
    assert [c.rloc16 for c in diag.children] == [DEPARTED_RLOC] and _no_departure_warning(diag)


@pytest.mark.parametrize("node_id", [0x2E, 0x27])
def test_a_node_that_really_detached_is_taken_live_and_flagged_detached(node_id):
    # Live Unassigned, empty tables, null partition/leader: a CORRECT live answer that
    # shows exactly what this diagnostic exists to find. Never replaced by the cached
    # "attached" view.
    matter = FakeMatter(overrides={
        (node_id, ATTR_ROUTING_ROLE): 1, (node_id, ATTR_NEIGHBOR_TABLE): [], (node_id, ATTR_ROUTE_TABLE): [],
        (node_id, ATTR_PARTITION_ID): None, (node_id, ATTR_LEADER_ROUTER_ID): None})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == node_id)
    assert diag.source == "live" and diag.read_error is None
    assert diag.role_name == "Unassigned" and diag.neighbours == [] and diag.partition_id is None
    flags = {f.code for f in health_flags(diag)}
    assert "detached" in flags
    assert node_id not in {n for n, _ in build_mesh(survey.diags).unreadable}


def test_a_router_that_split_off_as_a_one_node_leader_is_taken_live():
    matter = FakeMatter(overrides={(0x27, ATTR_ROUTING_ROLE): 6, (0x27, ATTR_NEIGHBOR_TABLE): []})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x27)
    assert diag.source == "live" and diag.read_error is None and diag.role_name == "Leader"
    assert ("single_neighbour", "bad") in {(f.code, f.severity) for f in health_flags(diag)}  # "no neighbours"


def test_a_changed_role_with_an_empty_table_is_taken_live():
    # Router -> End Device with no neighbours: the role CHANGED, which shows the node
    # really answered; the guard only distrusts "unchanged attached role + nothing".
    matter = FakeMatter(overrides={(0x3F, ATTR_ROUTING_ROLE): 3, (0x3F, ATTR_NEIGHBOR_TABLE): []})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x3F)
    assert diag.source == "live" and diag.read_error is None and diag.role_name == "EndDevice"
    assert "no_parent" in {f.code for f in health_flags(diag)}


@pytest.mark.parametrize("cached_role_node", [0x2E, 0x27])
def test_an_unchanged_attached_role_with_an_empty_table_fails_for_sleepy_and_router_alike(cached_role_node):
    matter = FakeMatter(overrides={(cached_role_node, ATTR_NEIGHBOR_TABLE): []})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == cached_role_node)
    assert diag.source == "cache" and "came back empty" in diag.read_error


def test_a_live_role_of_unspecified_with_null_partition_is_taken_live_and_flagged_role_unknown():
    matter = FakeMatter(overrides={
        (0x27, ATTR_ROUTING_ROLE): 0, (0x27, ATTR_NEIGHBOR_TABLE): [], (0x27, ATTR_ROUTE_TABLE): [],
        (0x27, ATTR_PARTITION_ID): None, (0x27, ATTR_LEADER_ROUTER_ID): None})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x27)
    assert diag.source == "live" and diag.read_error is None
    assert "role_unknown" in {f.code for f in health_flags(diag)}


def test_an_empty_live_neighbour_table_is_accepted_when_the_cache_was_empty_too():
    nodes = _fixture_nodes()
    raw = next(n for n in nodes if n["node_id"] == 0x3F)
    raw["attributes"][f"0/53/{ATTR_NEIGHBOR_TABLE}"] = []
    matter = FakeMatter(nodes=nodes, overrides={(0x3F, ATTR_NEIGHBOR_TABLE): []})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x3F)
    assert diag.source == "live" and diag.read_error is None and diag.neighbours == []


def test_a_live_table_with_a_skipped_malformed_row_skips_the_departed_check():
    # One good non-child row plus one malformed row: the malformed one might have
    # been the child, so absence proves nothing and nothing is called departed.
    cached = _cached_neighbor_table(0x3F)
    good_router = next(e for e in cached if not e["13"])
    matter = FakeMatter(overrides={(0x3F, ATTR_NEIGHBOR_TABLE): [good_router, {"0": 1}]})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x3F)
    assert diag.source == "live" and diag.read_error is None
    assert len(diag.neighbours) == 1
    assert _no_departure_warning(diag)
    assert any("malformed" in w for w in diag.parse_warnings), "the skipped row is still reported"


@pytest.mark.parametrize("attr", [ATTR_ROUTE_TABLE, ATTR_PARTITION_ID, ATTR_LEADER_ROUTER_ID])
@pytest.mark.parametrize("bad", [None, {}, "x"])
def test_an_unusable_live_route_table_partition_or_leader_is_a_failed_read(attr, bad):
    matter = FakeMatter(overrides={(0x3F, attr): bad})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = _assert_failed_read_kept_cache(survey, expect_in_error="live read returned no usable")
    # Nothing partially merged: the attributes that DID answer fine are not applied.
    names = {ATTR_ROUTE_TABLE: "RouteTable", ATTR_PARTITION_ID: "PartitionId",
             ATTR_LEADER_ROUTER_ID: "LeaderRouterId"}
    assert names[attr] in diag.read_error


def test_a_detached_unassigned_node_may_legitimately_have_no_partition_or_leader():
    # A node live-reporting role Unassigned has no partition/leader to report; that
    # null is the true answer, not an unusable one.
    matter = FakeMatter(overrides={(0x2E, ATTR_ROUTING_ROLE): 1, (0x2E, ATTR_PARTITION_ID): None,
                                   (0x2E, ATTR_LEADER_ROUTER_ID): None})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    assert diag.source == "live" and diag.read_error is None and diag.role_name == "Unassigned"
    assert diag.partition_id is None


def test_a_single_failing_attribute_read_is_named_and_nothing_is_merged():
    matter = FakeMatter(overrides={(0x3F, ATTR_ROUTE_TABLE): ConnectionError("route read boom"),
                                   (0x3F, ATTR_PARTITION_ID): 777})  # the sibling answered; must NOT apply
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = _assert_failed_read_kept_cache(survey, expect_in_error="RouteTable")
    assert "route read boom" in diag.read_error
    assert diag.partition_id != 777


def test_two_failing_attribute_reads_are_both_named():
    matter = FakeMatter(overrides={(0x3F, ATTR_ROUTE_TABLE): ConnectionError("a"),
                                   (0x3F, ATTR_LEADER_ROUTER_ID): ConnectionError("b")})
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x3F)
    assert "RouteTable" in diag.read_error and "LeaderRouterId" in diag.read_error


def test_departure_detection_on_one_router_still_works_while_another_router_fails():
    live_table = [e for e in _cached_neighbor_table(0x3F) if e["2"] != DEPARTED_RLOC]
    matter = FakeMatter(timeout_nodes={0x27}, overrides={(0x3F, ATTR_NEIGHBOR_TABLE): live_table})
    survey = _run(survey_thread_nodes(matter, live_read=True, per_node_timeout=5.0))
    by_id = {d.node_id: d for d in survey.diags}
    assert by_id[0x27].read_error and by_id[0x27].source == "cache"
    assert by_id[0x3F].source == "live" and not _no_departure_warning(by_id[0x3F])
    assert "Child 0x5C04" not in _mesh_names(survey)


# ---------------------------------------------------------------------------
# #334 post-review — B2.10: a fatal fake proves the merge/re-parse step's own
# failures are stamped, not silently dropped by gather(return_exceptions=True)
# ---------------------------------------------------------------------------

def test_a_bug_in_the_merge_reparse_step_is_stamped_not_swallowed(monkeypatch):
    # "Make it fatal" (root CLAUDE.md): parse_node_diag raising for exactly one
    # node proves the exception from asyncio.gather's return_exceptions=True
    # actually reaches the diag as a read_error, rather than being silently
    # dropped because nobody looked at gather's return value.
    import thread_survey  # pylint: disable=import-outside-toplevel

    real_parse_node_diag = thread_survey.parse_node_diag
    calls_for_0x2e = {"n": 0}

    def _fatal_on_second_call_for_0x2e(node_id, name, available, attributes):
        # The first call is the initial cache parse (must succeed, or there is
        # no diag to live-refresh at all); the SECOND is the live-refresh
        # merge/re-parse this test targets.
        if node_id == 0x2E:
            calls_for_0x2e["n"] += 1
            if calls_for_0x2e["n"] == 2:
                raise RuntimeError("boom — unexpected bug in the merge/re-parse step")
        return real_parse_node_diag(node_id, name, available, attributes)

    monkeypatch.setattr(thread_survey, "parse_node_diag", _fatal_on_second_call_for_0x2e)
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_read=True))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    assert diag.source == "cache", "a merge/re-parse failure must not corrupt the cached diag"
    assert diag.read_error is not None
    assert "live refresh failed" in diag.read_error
    assert "boom" in diag.read_error
    # sibling nodes sharing the same gather() call are unaffected
    other = next(d for d in survey.diags if d.node_id == 0x2F)
    assert other.source == "live" and other.read_error is None


# ---------------------------------------------------------------------------
# get_nodes() failure is a failed call, not an empty mesh
# ---------------------------------------------------------------------------

def test_a_get_nodes_failure_raises_rather_than_returning_an_empty_list():
    matter = FakeMatter(get_nodes_error=RuntimeError("matter-server unreachable"))
    with pytest.raises(RuntimeError, match="matter-server unreachable"):
        _run(survey_thread_nodes(matter, live_read=False))


# ---------------------------------------------------------------------------
# #334 post-review — B2.4: Survey.raw_count / Survey.skipped
# ---------------------------------------------------------------------------

class TestSurveyRawCountAndSkipped:
    def test_raw_count_matches_the_fixture(self):
        matter = FakeMatter()
        survey = _run(survey_thread_nodes(matter, live_read=False))
        assert isinstance(survey, Survey)
        assert survey.raw_count == 7
        assert survey.skipped == []

    def test_zero_raw_nodes(self):
        matter = FakeMatter(nodes=[])
        survey = _run(survey_thread_nodes(matter, live_read=False))
        assert survey.raw_count == 0
        assert survey.diags == []
        assert survey.skipped == []

    def test_non_mapping_raw_node_is_skipped_with_a_reason(self):
        matter = FakeMatter(nodes=["not-a-mapping", {"node_id": 1, "attributes": {"0/53/1": 5}}])
        survey = _run(survey_thread_nodes(matter, live_read=False))
        assert survey.raw_count == 2
        assert len(survey.skipped) == 1
        assert "not a mapping" in survey.skipped[0]
        assert {d.node_id for d in survey.diags} == {1}

    def test_raw_node_without_node_id_is_skipped_with_a_reason(self):
        matter = FakeMatter(nodes=[{"attributes": {"0/53/1": 5}}])
        survey = _run(survey_thread_nodes(matter, live_read=False))
        assert survey.raw_count == 1
        assert survey.diags == []
        assert len(survey.skipped) == 1
        assert "no node_id" in survey.skipped[0]

    def test_a_wifi_only_node_is_not_a_skip(self):
        # Adversarial: "when could this report a failure and be wrong?" — a
        # raw node this module successfully addressed but which simply is not a
        # Thread node (no cluster-0x35 key) must NOT land in `skipped` — that
        # would misreport a healthy Wi-Fi-only fabric as a parse failure.
        matter = FakeMatter(nodes=[{"node_id": 5, "attributes": {"0/40/1": "Vendor"}}])
        survey = _run(survey_thread_nodes(matter, live_read=False))
        assert survey.raw_count == 1
        assert survey.diags == []
        assert survey.skipped == []


# ---------------------------------------------------------------------------
# #334 post-review — B3.5b: a live PartitionId that genuinely differs from
# cache wins, and the disagreement text says so
# ---------------------------------------------------------------------------

def test_live_partition_id_differing_from_cache_wins_and_is_labelled_live():
    matter = FakeMatter()
    orig_read = matter.read

    async def _read(node_id, endpoint, cluster, attribute):
        if node_id == 0x2E and attribute == ATTR_PARTITION_ID:
            return 999999
        return await orig_read(node_id, endpoint, cluster, attribute)

    matter.read = _read
    survey = _run(survey_thread_nodes(matter, live_read=True))
    live_2e = next(d for d in survey.diags if d.node_id == 0x2E)
    assert live_2e.source == "live"
    assert live_2e.partition_id == 999999

    mesh = build_mesh(survey.diags)
    joined = " | ".join(mesh.disagreements)
    assert "0x2E" in joined
    assert "999999" in joined
    assert "(live)" in joined


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def test_node_names_override_wins_when_given():
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_read=False, node_names={0x34: "Grillplats socket"}))
    by_id = {d.node_id: d for d in survey.diags}
    assert by_id[0x34].name == "Grillplats socket"
    assert by_id[0x3F].name != "Grillplats socket"  # untouched — falls back normally


def test_name_falls_back_to_vendor_and_product_from_basic_information():
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_read=False))
    by_id = {d.node_id: d for d in survey.diags}
    assert by_id[0x34].name == "IKEA of Sweden GRILLPLATS Plug"
    assert by_id[0x38].name == "Aqara Presence Multi-Sensor FP300"


def test_name_falls_back_to_the_hex_node_id_with_no_basic_information():
    bare_node = {
        "node_id": 0x99,
        "available": True,
        "attributes": {"0/53/1": 3},  # RoutingRole only — no BasicInformation at all
    }
    matter = FakeMatter(nodes=[bare_node])
    survey = _run(survey_thread_nodes(matter, live_read=False))
    assert survey.diags[0].name == "node 0x99"


def test_name_with_an_empty_string_falls_through_to_the_vendor_product_fallback():
    # B3.12's node_names case: a caller-supplied name that is an empty string
    # must not win over the "" the .get() would otherwise happily return — it
    # has to fall through exactly like "no override at all" would.
    node = {
        "node_id": 0x34,
        "available": True,
        "attributes": {"0/53/1": 5, "0/40/1": "IKEA of Sweden", "0/40/3": "GRILLPLATS Plug"},
    }
    matter = FakeMatter(nodes=[node])
    survey = _run(survey_thread_nodes(matter, live_read=False, node_names={0x34: ""}))
    assert survey.diags[0].name == "IKEA of Sweden GRILLPLATS Plug"


# ---------------------------------------------------------------------------
# run_survey — the Indigo-thread sync bridge
# ---------------------------------------------------------------------------

class _RecordingRuntime:
    """Runs a submitted coroutine to completion NOW, on a fresh loop — the
    minimal ``AsyncRuntime.submit`` stand-in this module needs (tests/fakes.py's
    ``RecordingRuntime`` does the same for the export side)."""

    def submit(self, coro):
        from concurrent.futures import Future  # pylint: disable=import-outside-toplevel
        future: Future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:  # pylint: disable=broad-except
            future.set_exception(exc)
        return future


def test_run_survey_bridges_the_coroutine_through_a_concurrent_future():
    runtime = _RecordingRuntime()
    matter = FakeMatter(forbidden_nodes=ROUTER_IDS | NON_ROUTER_IDS)
    survey = run_survey(runtime, matter, live_read=False)
    assert isinstance(survey, Survey)
    assert {d.node_id for d in survey.diags} == ROUTER_IDS | NON_ROUTER_IDS


def test_run_survey_propagates_a_get_nodes_failure():
    runtime = _RecordingRuntime()
    matter = FakeMatter(get_nodes_error=RuntimeError("down"))
    with pytest.raises(RuntimeError, match="down"):
        run_survey(runtime, matter, live_read=False)


# ---------------------------------------------------------------------------
# #334 post-review — B2.7 / B3.6: a timeout cancels the underlying future
# ---------------------------------------------------------------------------

class _NeverCompletingFuture:
    """A concurrent.futures.Future stand-in that never resolves — ``result()``
    always raises the real timeout error, and ``cancel()`` records that it was
    asked to stop, which is exactly what B2.7 needs proven."""

    def __init__(self):
        self.cancelled = False

    def result(self, timeout=None):  # noqa: ARG002
        raise FuturesTimeoutError()

    def cancel(self):
        self.cancelled = True
        return True


class _NeverCompletingRuntime:
    def __init__(self):
        self.future = _NeverCompletingFuture()

    def submit(self, coro):
        coro.close()  # avoid a "coroutine was never awaited" warning — never run here
        return self.future


def test_run_survey_cancels_the_underlying_future_on_timeout(monkeypatch):
    monkeypatch.setattr("thread_survey.SURVEY_READ_TIMEOUT", 0.05)
    runtime = _NeverCompletingRuntime()
    matter = FakeMatter()
    with pytest.raises(FuturesTimeoutError):
        run_survey(runtime, matter, live_read=False, per_node_timeout=0.0)
    assert runtime.future.cancelled is True


# ---------------------------------------------------------------------------
# #344 — _parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:
    def test_iso_with_trailing_z(self):
        parsed = _parse_timestamp("2026-08-27T10:00:00Z")
        assert parsed is not None
        assert parsed.year == 2026 and parsed.month == 8 and parsed.day == 27
        assert parsed.tzinfo is not None

    def test_iso_naive_assumes_utc(self):
        parsed = _parse_timestamp("2026-08-27T10:00:00")
        assert parsed is not None
        assert parsed.tzinfo == timezone.utc

    def test_epoch_int(self):
        parsed = _parse_timestamp(1_800_000_000)
        assert parsed is not None
        assert parsed.tzinfo is not None

    def test_epoch_float(self):
        parsed = _parse_timestamp(1_800_000_000.5)
        assert parsed is not None

    def test_datetime_passthrough_naive_assumes_utc(self):
        naive = datetime(2026, 1, 1)
        parsed = _parse_timestamp(naive)
        assert parsed == naive.replace(tzinfo=timezone.utc)

    def test_datetime_passthrough_aware_is_untouched(self):
        aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _parse_timestamp(aware) is aware

    def test_garbage_string_is_none(self):
        assert _parse_timestamp("not a date") is None

    def test_none_is_none(self):
        assert _parse_timestamp(None) is None

    def test_dict_is_none(self):
        # Adversarial: "when could this raise and be wrong?" — matter-server
        # handing back an unexpected shape for these fields must degrade to
        # None, never crash the whole survey (root workspace CLAUDE.md
        # degradation-path convention).
        assert _parse_timestamp({"not": "a timestamp"}) is None

    def test_never_raises_for_any_of_the_above(self):
        for value in ("2026-08-27T10:00:00Z", "2026-08-27T10:00:00", 1_800_000_000,
                      1_800_000_000.5, datetime(2026, 1, 1), "garbage", None, {"a": 1}, [], object()):
            _parse_timestamp(value)  # must not raise


# ---------------------------------------------------------------------------
# #344 — raw date_commissioned/last_interview land on the diag; absence is None
# ---------------------------------------------------------------------------

class TestCacheAgeFields:
    def test_fixture_nodes_carry_no_timestamps_and_diags_get_none(self):
        # The shipped fixture has neither key at all — absence must be the
        # boring case, proven by running it through completely unchanged.
        # #344 review, A1: absence must ALSO stay silent — no parse_warning.
        matter = FakeMatter()
        survey = _run(survey_thread_nodes(matter, live_read=False))
        assert survey.diags  # sanity: the fixture actually parsed
        for diag in survey.diags:
            assert diag.date_commissioned is None
            assert diag.last_interview is None
            assert diag.last_event_seen is None
            assert not any("unparseable" in w for w in diag.parse_warnings)

    def test_a_present_but_unparseable_last_interview_records_a_parse_warning(self):
        # #344 review, A1 (silent-failure M1): a PRESENT raw value that
        # _parse_timestamp cannot make sense of — matter-server's wire shape
        # changing — must be visible, unlike the boring "absent" case above.
        nodes = _fixture_nodes()
        for raw in nodes:
            if raw["node_id"] == 0x34:
                raw["last_interview"] = "garbage"
        matter = FakeMatter(nodes=nodes)
        survey = _run(survey_thread_nodes(matter, live_read=False))
        diag = next(d for d in survey.diags if d.node_id == 0x34)
        assert diag.last_interview is None
        assert any("last_interview unparseable: 'garbage'" in w for w in diag.parse_warnings)
        # And it must actually reach an operator via build_mesh's warnings —
        # not just sit unused on the diag.
        mesh = build_mesh(survey.diags)
        assert any(
            node_id == 0x34 and "last_interview unparseable" in msg for node_id, msg in mesh.warnings
        )

    def test_a_present_but_unparseable_date_commissioned_records_a_parse_warning(self):
        nodes = _fixture_nodes()
        for raw in nodes:
            if raw["node_id"] == 0x34:
                raw["date_commissioned"] = {"not": "a timestamp"}
        matter = FakeMatter(nodes=nodes)
        survey = _run(survey_thread_nodes(matter, live_read=False))
        diag = next(d for d in survey.diags if d.node_id == 0x34)
        assert diag.date_commissioned is None
        assert any("date_commissioned unparseable" in w for w in diag.parse_warnings)

    def test_raw_date_commissioned_and_last_interview_land_on_the_diag(self):
        nodes = _fixture_nodes()
        for raw in nodes:
            if raw["node_id"] == 0x34:
                raw["date_commissioned"] = "2026-08-01T00:00:00Z"
                raw["last_interview"] = 1_800_000_000
        matter = FakeMatter(nodes=nodes)
        survey = _run(survey_thread_nodes(matter, live_read=False))
        diag = next(d for d in survey.diags if d.node_id == 0x34)
        assert diag.date_commissioned is not None and diag.date_commissioned.year == 2026
        assert diag.last_interview is not None
        # A sibling node with neither raw key present is unaffected.
        other = next(d for d in survey.diags if d.node_id == 0x3F)
        assert other.date_commissioned is None and other.last_interview is None

    def test_event_stamps_mapping_is_applied_missing_node_id_is_none(self):
        matter = FakeMatter()
        survey = _run(survey_thread_nodes(matter, live_read=False, event_stamps={0x27: 123.0}))
        by_id = {d.node_id: d for d in survey.diags}
        assert by_id[0x27].last_event_seen == 123.0
        assert by_id[0x2E].last_event_seen is None

    def test_run_survey_forwards_event_stamps(self):
        runtime = _RecordingRuntime()
        matter = FakeMatter()
        survey = run_survey(runtime, matter, live_read=False, event_stamps={0x27: 42.0})
        by_id = {d.node_id: d for d in survey.diags}
        assert by_id[0x27].last_event_seen == 42.0


# ---------------------------------------------------------------------------
# #344 decision 2 — the trap: a live refresh's per-field copy from a freshly-
# parsed live diag (which never sets these three fields itself) must not wipe
# out an interview/event stamp applied BEFORE the live-refresh block runs.
# survey_thread_nodes applies them AFTER instead — this pins that ordering.
# ---------------------------------------------------------------------------

def test_live_refresh_success_does_not_wipe_the_interview_and_event_stamps():
    nodes = _fixture_nodes()
    for raw in nodes:
        if raw["node_id"] == 0x2E:  # BILRESA — non-router, gets live-read
            raw["last_interview"] = "2026-08-27T10:00:00Z"
    matter = FakeMatter(nodes=nodes)
    survey = _run(survey_thread_nodes(
        matter, live_read=True, event_stamps={0x2E: 1_800_000_000.0}))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    assert diag.source == "live", "sanity: the live refresh must actually have succeeded"
    assert diag.last_interview is not None and diag.last_interview.year == 2026
    assert diag.last_event_seen == 1_800_000_000.0
