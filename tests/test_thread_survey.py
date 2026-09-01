"""Tests for the shared Thread mesh survey coroutine — #334.

``thread_survey`` is deliberately the ONE place that decides how to combine
matter-server's cache with a live refresh; both the menu diagnostic
(``tests/test_diagnostics_menu.py``) and the later IWS page reuse it unchanged.
This file owns the coroutine itself: concurrency, per-node timeout handling,
router exclusion, naming, and the "a get_nodes() failure raises" contract.

Fixture: ``tests/fixtures/thread_mesh/nodes.json`` — the same 7 real nodes
``test_thread_mesh.py`` parses. Routers (RoutingRole 5/Router): 0x27, 0x34, 0x3F.
Non-routers (SED/REED, the only nodes ever live-read): 0x2E, 0x2F, 0x38, 0x40.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from thread_mesh import parse_node_diag
from thread_survey import run_survey, survey_thread_nodes

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
                 get_nodes_error=None):
        self.nodes = _fixture_nodes() if nodes is None else nodes
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
        raw = next(n for n in self.nodes if n["node_id"] == node_id)
        return raw["attributes"].get(f"{endpoint}/{cluster}/{attribute}")


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Cache-only (liveSleepy=False)
# ---------------------------------------------------------------------------

def test_without_live_read_every_fixture_node_is_returned_from_cache():
    matter = FakeMatter(forbidden_nodes=ROUTER_IDS | NON_ROUTER_IDS)  # any read() call fails the test
    diags = _run(survey_thread_nodes(matter, live_sleepy=False))
    assert {d.node_id for d in diags} == ROUTER_IDS | NON_ROUTER_IDS
    assert all(d.source == "cache" for d in diags)
    assert matter.read_calls == [], "liveSleepy=False must never call read()"


# ---------------------------------------------------------------------------
# Live refresh (liveSleepy=True)
# ---------------------------------------------------------------------------

def test_live_read_only_ever_touches_non_router_nodes():
    matter = FakeMatter(forbidden_nodes=ROUTER_IDS)
    diags = _run(survey_thread_nodes(matter, live_sleepy=True))
    assert set(matter.read_calls) <= NON_ROUTER_IDS
    assert set(matter.read_calls) == NON_ROUTER_IDS, "every non-router should have been live-read"
    routers = {d.node_id: d for d in diags if d.node_id in ROUTER_IDS}
    assert all(d.source == "cache" and d.read_error is None for d in routers.values())


def test_a_successful_live_read_flips_the_diag_to_live_and_keeps_its_data():
    matter = FakeMatter()
    diags = _run(survey_thread_nodes(matter, live_sleepy=True))
    by_id = {d.node_id: d for d in diags}
    live = by_id[0x2E]  # BILRESA scroll wheel — SED, gets live-read
    assert live.source == "live"
    assert live.read_error is None
    cached = _direct_parse(0x2E)
    assert live.role == cached.role
    assert [n.rloc16 for n in live.neighbours] == [n.rloc16 for n in cached.neighbours]


def _direct_parse(node_id: int):
    raw = next(n for n in _fixture_nodes() if n["node_id"] == node_id)
    return parse_node_diag(node_id, "", raw["available"], raw["attributes"])


def test_a_timed_out_live_read_keeps_the_cached_diag_and_names_the_reason():
    matter = FakeMatter(timeout_nodes={0x2E})
    diags = _run(survey_thread_nodes(matter, live_sleepy=True, per_node_timeout=5.0))
    by_id = {d.node_id: d for d in diags}
    timed_out = by_id[0x2E]
    assert timed_out.source == "cache", "a failed live read must not discard the cached data"
    assert timed_out.read_error is not None and "timed out" in timed_out.read_error
    cached = _direct_parse(0x2E)
    assert timed_out.role == cached.role
    # a node whose live read succeeded is unaffected by a sibling's timeout
    assert by_id[0x2F].source == "live" and by_id[0x2F].read_error is None


def test_an_erroring_live_read_keeps_the_cached_diag_and_names_the_reason():
    matter = FakeMatter(error_nodes={0x38: ConnectionError("socket closed")})
    diags = _run(survey_thread_nodes(matter, live_sleepy=True))
    diag = next(d for d in diags if d.node_id == 0x38)
    assert diag.source == "cache"
    assert diag.read_error is not None and "socket closed" in diag.read_error


def test_one_nodes_live_read_failure_never_drops_it_from_the_result():
    """A degraded node is still reported, per the module contract — never
    silently thinned out (root CLAUDE.md degradation-path convention)."""
    matter = FakeMatter(timeout_nodes={0x2E, 0x2F, 0x38, 0x40})
    diags = _run(survey_thread_nodes(matter, live_sleepy=True, per_node_timeout=5.0))
    assert {d.node_id for d in diags} == ROUTER_IDS | NON_ROUTER_IDS
    assert all(d.read_error for d in diags if d.node_id in NON_ROUTER_IDS)


# ---------------------------------------------------------------------------
# get_nodes() failure is a failed call, not an empty mesh
# ---------------------------------------------------------------------------

def test_a_get_nodes_failure_raises_rather_than_returning_an_empty_list():
    matter = FakeMatter(get_nodes_error=RuntimeError("matter-server unreachable"))
    with pytest.raises(RuntimeError, match="matter-server unreachable"):
        _run(survey_thread_nodes(matter, live_sleepy=False))


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def test_node_names_override_wins_when_given():
    matter = FakeMatter()
    diags = _run(survey_thread_nodes(matter, live_sleepy=False, node_names={0x34: "Grillplats socket"}))
    by_id = {d.node_id: d for d in diags}
    assert by_id[0x34].name == "Grillplats socket"
    assert by_id[0x3F].name != "Grillplats socket"  # untouched — falls back normally


def test_name_falls_back_to_vendor_and_product_from_basic_information():
    matter = FakeMatter()
    diags = _run(survey_thread_nodes(matter, live_sleepy=False))
    by_id = {d.node_id: d for d in diags}
    assert by_id[0x34].name == "IKEA of Sweden GRILLPLATS Plug"
    assert by_id[0x38].name == "Aqara Presence Multi-Sensor FP300"


def test_name_falls_back_to_the_hex_node_id_with_no_basic_information():
    bare_node = {
        "node_id": 0x99,
        "available": True,
        "attributes": {"0/53/1": 3},  # RoutingRole only — no BasicInformation at all
    }
    matter = FakeMatter(nodes=[bare_node])
    diags = _run(survey_thread_nodes(matter, live_sleepy=False))
    assert diags[0].name == "node 0x99"


# ---------------------------------------------------------------------------
# run_survey — the Indigo-thread sync bridge
# ---------------------------------------------------------------------------

class _RecordingRuntime:
    """Runs a submitted coroutine to completion NOW, on a fresh loop — the
    minimal ``AsyncRuntime.submit`` stand-in this module needs (tests/fakes.py's
    ``RecordingRuntime`` does the same for the export side)."""

    def submit(self, coro):
        from concurrent.futures import Future
        future: Future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:  # pylint: disable=broad-except
            future.set_exception(exc)
        return future


def test_run_survey_bridges_the_coroutine_through_a_concurrent_future():
    runtime = _RecordingRuntime()
    matter = FakeMatter(forbidden_nodes=ROUTER_IDS | NON_ROUTER_IDS)
    diags = run_survey(runtime, matter, live_sleepy=False)
    assert {d.node_id for d in diags} == ROUTER_IDS | NON_ROUTER_IDS


def test_run_survey_propagates_a_get_nodes_failure():
    runtime = _RecordingRuntime()
    matter = FakeMatter(get_nodes_error=RuntimeError("down"))
    with pytest.raises(RuntimeError, match="down"):
        run_survey(runtime, matter, live_sleepy=False)
