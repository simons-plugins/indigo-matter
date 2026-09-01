"""Tests for the shared Thread mesh survey coroutine — #334.

``thread_survey`` is deliberately the ONE place that decides how to combine
matter-server's cache with a live refresh; both the menu diagnostic
(``tests/test_diagnostics_menu.py``) and the IWS page (``tests/test_thread_page.py``)
reuse it unchanged. This file owns the coroutine itself: concurrency, per-node
timeout handling, router exclusion, naming, and the "a get_nodes() failure raises"
contract, plus (post-review, #334) the :class:`~thread_survey.Survey` result shape,
the live-refresh merge's own failure handling, and the sync bridge's timeout cancel.

Fixture: ``tests/fixtures/thread_mesh/nodes.json`` — the same 7 real nodes
``test_thread_mesh.py`` parses. Routers (RoutingRole 5/Router): 0x27, 0x34, 0x3F.
Non-routers (SED/REED, the only nodes ever live-read): 0x2E, 0x2F, 0x38, 0x40.
"""
from __future__ import annotations

import asyncio
import json
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from pathlib import Path

import pytest

from thread_mesh import ATTR_PARTITION_ID, build_mesh, parse_node_diag
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=False))
    assert {d.node_id for d in survey.diags} == ROUTER_IDS | NON_ROUTER_IDS
    assert all(d.source == "cache" for d in survey.diags)
    assert matter.read_calls == [], "liveSleepy=False must never call read()"


# ---------------------------------------------------------------------------
# Live refresh (liveSleepy=True)
# ---------------------------------------------------------------------------

def test_live_read_only_ever_touches_non_router_nodes():
    matter = FakeMatter(forbidden_nodes=ROUTER_IDS)
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
    assert set(matter.read_calls) <= NON_ROUTER_IDS
    assert set(matter.read_calls) == NON_ROUTER_IDS, "every non-router should have been live-read"
    routers = {d.node_id: d for d in survey.diags if d.node_id in ROUTER_IDS}
    assert all(d.source == "cache" and d.read_error is None for d in routers.values())


def test_a_successful_live_read_flips_the_diag_to_live_and_keeps_its_data():
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
    live = next(d for d in survey.diags if d.node_id == 0x2E)
    assert live.source == "live"
    assert len(live.routes) == 1
    assert live.routes[0].router_id == 51


def _direct_parse(node_id: int):
    raw = next(n for n in _fixture_nodes() if n["node_id"] == node_id)
    return parse_node_diag(node_id, "", raw["available"], raw["attributes"])


def test_a_timed_out_live_read_keeps_the_cached_diag_and_names_the_reason():
    matter = FakeMatter(timeout_nodes={0x2E})
    survey = _run(survey_thread_nodes(matter, live_sleepy=True, per_node_timeout=5.0))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
    diag = next(d for d in survey.diags if d.node_id == 0x38)
    assert diag.source == "cache"
    assert diag.read_error is not None and "socket closed" in diag.read_error


def test_one_nodes_live_read_failure_never_drops_it_from_the_result():
    """A degraded node is still reported, per the module contract — never
    silently thinned out (root CLAUDE.md degradation-path convention)."""
    matter = FakeMatter(timeout_nodes={0x2E, 0x2F, 0x38, 0x40})
    survey = _run(survey_thread_nodes(matter, live_sleepy=True, per_node_timeout=5.0))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    assert diag.source == "cache"
    assert diag.read_error == "live read returned no NeighborTable"


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
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
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
        _run(survey_thread_nodes(matter, live_sleepy=False))


# ---------------------------------------------------------------------------
# #334 post-review — B2.4: Survey.raw_count / Survey.skipped
# ---------------------------------------------------------------------------

class TestSurveyRawCountAndSkipped:
    def test_raw_count_matches_the_fixture(self):
        matter = FakeMatter()
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
        assert isinstance(survey, Survey)
        assert survey.raw_count == 7
        assert survey.skipped == []

    def test_zero_raw_nodes(self):
        matter = FakeMatter(nodes=[])
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
        assert survey.raw_count == 0
        assert survey.diags == []
        assert survey.skipped == []

    def test_non_mapping_raw_node_is_skipped_with_a_reason(self):
        matter = FakeMatter(nodes=["not-a-mapping", {"node_id": 1, "attributes": {"0/53/1": 5}}])
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
        assert survey.raw_count == 2
        assert len(survey.skipped) == 1
        assert "not a mapping" in survey.skipped[0]
        assert {d.node_id for d in survey.diags} == {1}

    def test_raw_node_without_node_id_is_skipped_with_a_reason(self):
        matter = FakeMatter(nodes=[{"attributes": {"0/53/1": 5}}])
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
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
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=True))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=False, node_names={0x34: "Grillplats socket"}))
    by_id = {d.node_id: d for d in survey.diags}
    assert by_id[0x34].name == "Grillplats socket"
    assert by_id[0x3F].name != "Grillplats socket"  # untouched — falls back normally


def test_name_falls_back_to_vendor_and_product_from_basic_information():
    matter = FakeMatter()
    survey = _run(survey_thread_nodes(matter, live_sleepy=False))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=False))
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
    survey = _run(survey_thread_nodes(matter, live_sleepy=False, node_names={0x34: ""}))
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
    survey = run_survey(runtime, matter, live_sleepy=False)
    assert isinstance(survey, Survey)
    assert {d.node_id for d in survey.diags} == ROUTER_IDS | NON_ROUTER_IDS


def test_run_survey_propagates_a_get_nodes_failure():
    runtime = _RecordingRuntime()
    matter = FakeMatter(get_nodes_error=RuntimeError("down"))
    with pytest.raises(RuntimeError, match="down"):
        run_survey(runtime, matter, live_sleepy=False)


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
        run_survey(runtime, matter, live_sleepy=False, per_node_timeout=0.0)
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
        matter = FakeMatter()
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
        assert survey.diags  # sanity: the fixture actually parsed
        for diag in survey.diags:
            assert diag.date_commissioned is None
            assert diag.last_interview is None
            assert diag.last_event_seen is None

    def test_raw_date_commissioned_and_last_interview_land_on_the_diag(self):
        nodes = _fixture_nodes()
        for raw in nodes:
            if raw["node_id"] == 0x34:
                raw["date_commissioned"] = "2026-08-01T00:00:00Z"
                raw["last_interview"] = 1_800_000_000
        matter = FakeMatter(nodes=nodes)
        survey = _run(survey_thread_nodes(matter, live_sleepy=False))
        diag = next(d for d in survey.diags if d.node_id == 0x34)
        assert diag.date_commissioned is not None and diag.date_commissioned.year == 2026
        assert diag.last_interview is not None
        # A sibling node with neither raw key present is unaffected.
        other = next(d for d in survey.diags if d.node_id == 0x3F)
        assert other.date_commissioned is None and other.last_interview is None

    def test_event_stamps_mapping_is_applied_missing_node_id_is_none(self):
        matter = FakeMatter()
        survey = _run(survey_thread_nodes(matter, live_sleepy=False, event_stamps={0x27: 123.0}))
        by_id = {d.node_id: d for d in survey.diags}
        assert by_id[0x27].last_event_seen == 123.0
        assert by_id[0x2E].last_event_seen is None

    def test_run_survey_forwards_event_stamps(self):
        runtime = _RecordingRuntime()
        matter = FakeMatter()
        survey = run_survey(runtime, matter, live_sleepy=False, event_stamps={0x27: 42.0})
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
        matter, live_sleepy=True, event_stamps={0x2E: 1_800_000_000.0}))
    diag = next(d for d in survey.diags if d.node_id == 0x2E)
    assert diag.source == "live", "sanity: the live refresh must actually have succeeded"
    assert diag.last_interview is not None and diag.last_interview.year == 2026
    assert diag.last_event_seen == 1_800_000_000.0
