"""The Matter menu's read-only diagnostics — issue #191 (deliverables A and B),
plus the Thread mesh report added by #334 (deliverable C).

``settings_report`` decides WHAT is reported and is tested next door; this pins
the Indigo-facing half: the pickers, the address the explorer resolves, and the
refusals. The refusals matter more than they look — every one of them is the
difference between a diagnostic and a guess. The Thread mesh report reuses
``thread_survey``/``thread_mesh`` (tested on their own next door) for the actual
model; this pins the menu-facing glue and the partial-failure convention.

The invariant behind the whole file: **nothing here writes to a device.** The
last test asserts that against the source rather than against behaviour, because
the point is that no future edit adds one — and, since #334, it covers
``thread_survey.py`` too (ADR-0004 names ``diagnostics_menu_mixin.py`` only, but
the same read-only guarantee has to hold for the module it now calls).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from concurrent.futures import Future, TimeoutError as FuturesTimeoutError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SERVER_PLUGIN = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
                 / "Contents" / "Server Plugin")

THREAD_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "thread_mesh" / "nodes.json"
THREAD_ROUTER_IDS = {0x27, 0x34, 0x3F}
THREAD_NON_ROUTER_IDS = {0x2E, 0x2F, 0x38, 0x40}

RELAY_NODE = {
    "node_id": 0x34,
    "available": True,
    "attributes": {
        "0/40/1": "IKEA",
        "0/40/3": "GRILLPLATS Plug",
        "0/40/10": "1.0.9",
        "1/29/0": [{"0": 266}],
        "1/6/0": True,
        "1/6/16387": 1,
        "1/6/65531": [0, 16385, 16386, 16387, 65531],
    },
}


class _Future:
    """A concurrent.futures stand-in: ``submit(...).result(timeout=…)``."""

    def __init__(self, value=None, raises=None):
        self._value, self._raises = value, raises

    def result(self, timeout=None):  # noqa: ARG002
        if self._raises is not None:
            raise self._raises
        return self._value

    def cancel(self):  # run_survey calls this on a timeout (#334 post-review, B2.7)
        return True


@pytest.fixture
def mixin(mock_indigo_base, mock_logger):
    """A bare DiagnosticsMenuMixin with the collaborators it reaches for."""
    for name in ("settings_report", "diagnostics_menu_mixin"):
        sys.modules.pop(name, None)
    module = importlib.import_module("diagnostics_menu_mixin")
    obj = module.DiagnosticsMenuMixin()
    obj.logger = mock_logger
    obj.pluginPrefs = {}
    obj.runtime = Mock()
    obj.matter = Mock()
    obj.device_sync = Mock()
    obj.device_sync.list_nodes.return_value = [(0x34, ["Grillplats socket"])]
    obj.device_sync.lookup.return_value = 0
    obj.runtime.submit.return_value = _Future(RELAY_NODE)
    return module, obj


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------

def test_the_report_picker_offers_every_device_first(mixin):
    _module, obj = mixin
    options = obj.getSurveyNodes()
    assert options[0][0] == "all"
    assert options[1][1] == "Grillplats socket — node 0x34"


def test_the_explorer_picker_has_no_every_device_row(mixin):
    """A full dump of every node at once is thousands of log lines and answers
    no question anyone actually has."""
    _module, obj = mixin
    assert all(value != "all" for value, _label in obj.getExploreNodes())


def test_the_explorer_picker_says_so_when_nothing_is_commissioned(mixin):
    """An empty popup is indistinguishable from a broken one."""
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = []
    assert "no Matter devices" in obj.getExploreNodes()[0][1]


def test_a_node_with_no_indigo_devices_is_still_listed(mixin):
    """An empty bridge is the node most in need of a survey and produces no
    Indigo device at all — the decommission picker learned this the hard way."""
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(0x50, [])]
    assert "(no Indigo devices)" in obj.getSurveyNodes()[1][1]


def test_a_failing_picker_degrades_instead_of_killing_the_dialog(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.side_effect = RuntimeError("boom")
    assert obj.getSurveyNodes()[1][1].startswith("(error building list")


def test_endpoint_and_cluster_pickers_show_only_what_the_device_reports(mixin):
    """Offering all 135 clusters the model knows would bury the two this device
    has."""
    _module, obj = mixin
    values = {"node": str(0x34)}
    endpoints = obj.getExploreEndpoints(valuesDict=values)
    assert [v for v, _l in endpoints] == ["all", "0", "1"]
    clusters = obj.getExploreClusters(valuesDict=values)
    labels = dict(clusters)
    assert labels["all"] == "All clusters"
    assert "OnOff (0x0006)" in labels.values()
    assert "LevelControl (0x0008)" not in labels.values()


def test_the_cluster_picker_narrows_to_the_chosen_endpoint(mixin):
    _module, obj = mixin
    labels = dict(obj.getExploreClusters(valuesDict={"node": "52", "endpoint": "1"}))
    assert "BasicInformation (0x0028)" not in labels.values()
    assert "OnOff (0x0006)" in labels.values()


def test_pickers_keep_their_wildcard_row_when_matter_server_is_down(mixin):
    """These run while the dialog is being drawn; one that throws takes the
    dialog with it."""
    _module, obj = mixin
    obj.runtime.submit.return_value = _Future(raises=ConnectionError("no socket"))
    assert obj.getExploreEndpoints(valuesDict={"node": "52"}) == [("all", "All endpoints")]
    assert obj.getExploreClusters(valuesDict={"node": "52"}) == [("all", "All clusters")]


def test_the_endpoint_picker_names_endpoints_by_their_indigo_device(mixin):
    """An endpoint NUMBER means nothing to a user."""
    _module, obj = mixin
    obj.device_sync.lookup.side_effect = lambda _n, ep: 991 if ep == 1 else 0
    sys.modules["indigo"].devices = {991: SimpleNamespace(name="Kitchen Plug")}
    labels = dict(obj.getExploreEndpoints(valuesDict={"node": "52"}))
    assert labels["1"] == "Kitchen Plug (endpoint 1)"
    assert labels["0"] == "Endpoint 0"


# ---------------------------------------------------------------------------
# Deliverable B — the on-demand report
# ---------------------------------------------------------------------------

def test_report_requires_a_selection(mixin):
    _module, obj = mixin
    ok, _values, errors = obj.menuReportSettings({"node": ""})
    assert not ok and "node" in errors


def test_report_forces_so_an_already_seen_device_answers(mixin):
    """The menu exists because the automatic report is once-per-device, which
    means a device commissioned before this shipped would otherwise never
    produce one."""
    _module, obj = mixin
    assert obj.menuReportSettings({"node": "52"})[0]
    node = obj.device_sync.report_settable_attributes.call_args.args[0]
    assert node.node_id == 0x34
    assert obj.device_sync.report_settable_attributes.call_args.kwargs["force"] is True


def test_report_of_every_device_walks_them_all(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(0x34, ["a"]), (0x35, ["b"])]
    assert obj.menuReportSettings({"node": "all"})[0]
    assert obj.device_sync.report_settable_attributes.call_count == 2


def test_report_of_every_device_refuses_when_there_are_none(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = []
    ok, _values, errors = obj.menuReportSettings({"node": "all"})
    assert not ok and "No Matter devices" in errors["node"]


def test_a_partial_answer_is_reported_as_an_error_not_as_success(mixin):
    """A diagnostic that looks complete when it is not is worse than one that
    fails — the whole value is that the user trusts what it printed."""
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(0x34, ["a"]), (0x35, ["b"])]
    calls = {"n": 0}

    def flaky(_node_id):
        calls["n"] += 1
        return _Future(RELAY_NODE) if calls["n"] == 1 else _Future(raises=OSError("down"))

    obj.runtime.submit.side_effect = lambda coro: flaky(coro)
    ok, _values, errors = obj.menuReportSettings({"node": "all"})
    assert not ok and "1 device(s) could not be read" in errors["node"]
    assert obj.device_sync.report_settable_attributes.call_count == 1, "the rest still ran"


def test_a_node_matter_server_does_not_know_is_named(mixin):
    _module, obj = mixin
    obj.runtime.submit.return_value = _Future(None)
    ok, _values, errors = obj.menuReportSettings({"node": "52"})
    assert not ok and "could not be read" in errors["node"]


# ---------------------------------------------------------------------------
# Deliverable A — the explorer
# ---------------------------------------------------------------------------

def test_explore_dumps_to_the_event_log(mixin):
    """Output goes to the log because a menu dialog physically cannot show a
    result — ConfigUI has no per-field change callback."""
    _module, obj = mixin
    assert obj.menuExploreAttributes({"node": "52", "endpoint": "all", "cluster": "all"})[0]
    body = "\n".join(str(c) for c in obj.logger.info.call_args_list)
    assert "StartUpOnOff (0x4003)" in body


def test_explore_can_be_pointed_at_endpoint_zero(mixin):
    """Endpoint 0 is the Matter root, where BasicInformation lives. The
    no-selection sentinel is the string "0", so folding it into the wildcard
    would make the root the one address the explorer could not reach."""
    _module, obj = mixin
    assert obj.menuExploreAttributes({"node": "52", "endpoint": "0", "cluster": "all"})[0]
    body = "\n".join(str(c) for c in obj.logger.info.call_args_list)
    assert "VendorName (0x0001)" in body
    assert "StartUpOnOff" not in body, "endpoint 0 only — this lives on endpoint 1"


def test_explore_refuses_the_all_devices_sentinel(mixin):
    _module, obj = mixin
    ok, _values, errors = obj.menuExploreAttributes({"node": "all"})
    assert not ok and "node" in errors


def test_a_live_read_needs_an_exact_address(mixin):
    """There is no wildcard form of a live read, and fanning one out into a read
    storm against a sleepy device is not a reasonable interpretation."""
    _module, obj = mixin
    ok, _v, errors = obj.menuExploreAttributes(
        {"node": "52", "endpoint": "all", "cluster": "6", "attribute": "0x4003"})
    assert not ok and "endpoint" in errors
    ok, _v, errors = obj.menuExploreAttributes(
        {"node": "52", "endpoint": "1", "cluster": "all", "attribute": "0x4003"})
    assert not ok and "cluster" in errors


def test_a_live_read_accepts_hex_and_decimal(mixin):
    _module, obj = mixin
    for text in ("0x4003", "16387"):
        obj.matter.read.reset_mock()
        obj.runtime.submit.return_value = _Future(1)
        ok, *_ = obj.menuExploreAttributes(
            {"node": "52", "endpoint": "1", "cluster": "6", "attribute": text})
        assert ok
        assert obj.matter.read.call_args.args == (0x34, 1, 6, 0x4003)


def test_a_live_read_rejects_nonsense(mixin):
    _module, obj = mixin
    ok, _v, errors = obj.menuExploreAttributes(
        {"node": "52", "endpoint": "1", "cluster": "6", "attribute": "the blue one"})
    assert not ok and "attribute" in errors


def test_a_sleepy_device_is_not_reported_as_missing_the_attribute(mixin):
    """A Thread device that missed its poll window looks exactly like one that
    does not implement the attribute, and saying so would be a guess dressed as
    a measurement."""
    _module, obj = mixin
    obj.runtime.submit.return_value = _Future(raises=FuturesTimeoutError())
    ok, _v, errors = obj.menuExploreAttributes(
        {"node": "52", "endpoint": "1", "cluster": "6", "attribute": "0x4003"})
    assert not ok
    assert "asleep" in errors["attribute"] and "try again" in errors["attribute"]


def test_the_explorer_works_before_the_connection_is_up(mixin):
    _module, obj = mixin
    obj.matter = None
    ok, _v, errors = obj.menuExploreAttributes(
        {"node": "52", "endpoint": "1", "cluster": "6", "attribute": "0x4003"})
    assert not ok and "not connected" in errors["attribute"]


# ---------------------------------------------------------------------------
# Deliverable C — the Thread mesh report (#334)
# ---------------------------------------------------------------------------

class _ThreadFakeMatter:
    """``MatterClient`` stand-in for the Thread mesh report: ``get_nodes``
    returns the fixture, ``read`` answers per-node (or times out/errors/raises
    "must never be touched" for a caller-chosen set), and records every node id
    it was asked to read."""

    def __init__(self, nodes, *, timeout_nodes=(), forbidden_nodes=(), get_nodes_error=None):
        self.nodes = nodes
        self.connected = True  # B5.6 — menuReportThreadMesh now checks this
        self.read_calls: list[int] = []
        self._timeout_nodes = set(timeout_nodes)
        self._forbidden_nodes = set(forbidden_nodes)
        self._get_nodes_error = get_nodes_error

    async def get_nodes(self):
        if self._get_nodes_error is not None:
            raise self._get_nodes_error
        return self.nodes

    async def read(self, node_id, endpoint, cluster, attribute):
        self.read_calls.append(node_id)
        if node_id in self._forbidden_nodes:
            # "Make it fatal" (root CLAUDE.md): if a future edit ever calls
            # read() here — with liveReadSleepy off, or on a router — this
            # raises instead of quietly succeeding.
            raise AssertionError(f"read() must never touch node 0x{node_id:X}")
        if node_id in self._timeout_nodes:
            raise asyncio.TimeoutError()
        raw = next(n for n in self.nodes if n["node_id"] == node_id)
        return raw["attributes"].get(f"{endpoint}/{cluster}/{attribute}")


class _RecordingRuntime:
    """Runs a submitted coroutine to completion now, on a fresh loop — the
    minimal ``AsyncRuntime.submit`` stand-in the Thread mesh report needs to
    exercise its real ``thread_survey``/``thread_mesh`` path end to end (the
    generic ``_Future``-based mock above ignores the coroutine entirely, which
    would not exercise anything here)."""

    def submit(self, coro):
        future: Future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except Exception as exc:  # pylint: disable=broad-except
            future.set_exception(exc)
        return future


def _thread_mesh_mixin(mixin, matter):
    module, obj = mixin
    obj.matter = matter
    obj.runtime = _RecordingRuntime()
    return module, obj


def _log_body(obj) -> str:
    # Same convention as the explorer tests above: stringify the raw call
    # objects rather than reproduce %-formatting, since a substring check does
    # not care about the quoting.
    return "\n".join(str(call) for call in obj.logger.info.call_args_list)


def test_the_report_prints_the_fixtures_health_flags(mixin):
    nodes = json.loads(THREAD_FIXTURE_PATH.read_text(encoding="utf-8"))
    _module, obj = _thread_mesh_mixin(mixin, _ThreadFakeMatter(nodes))
    ok, _values = obj.menuReportThreadMesh({"liveReadSleepy": False})[:2]
    assert ok
    body = _log_body(obj)
    # 0x34 (router_id 62) has exactly one neighbour; 0x27 is 3+ hops from the
    # leader — both real facts from the SPEC's "ground truth" section.
    assert "single_neighbour:" in body
    assert "far_from_leader:" in body


def test_a_timed_out_live_read_is_reported_in_the_dialog_AND_the_node_still_appears_cached(mixin):
    nodes = json.loads(THREAD_FIXTURE_PATH.read_text(encoding="utf-8"))
    matter = _ThreadFakeMatter(nodes, timeout_nodes={0x2E})
    _module, obj = _thread_mesh_mixin(mixin, matter)
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": True})
    assert not ok
    assert "1 node(s) could not be live-read" in errors["liveReadSleepy"]
    assert "cached values" in errors["liveReadSleepy"]
    body = _log_body(obj)
    assert "BILRESA scroll wheel" in body, "the timed-out node must still be printed, from cache"


@pytest.mark.parametrize("off_value", [False, "false", "False", 0])
def test_live_read_sleepy_off_performs_no_read_calls(mixin, off_value):
    """"Make it fatal" (root CLAUDE.md): every node id is in the forbidden set,
    so any read() call — a broken liveReadSleepy guard — fails loudly.

    #334 post-review, B3.1: Indigo delivers a checkbox as the STRING
    "true"/"false" — ``bool("false")`` is ``True``, so before the ``_truthy``
    fix every one of these OFF values except the literal ``False`` still
    live-read (this parametrisation is what caught it).
    """
    nodes = json.loads(THREAD_FIXTURE_PATH.read_text(encoding="utf-8"))
    matter = _ThreadFakeMatter(nodes, forbidden_nodes=THREAD_ROUTER_IDS | THREAD_NON_ROUTER_IDS)
    _module, obj = _thread_mesh_mixin(mixin, matter)
    ok, _values = obj.menuReportThreadMesh({"liveReadSleepy": off_value})[:2]
    assert ok
    assert matter.read_calls == []


def test_routers_are_never_live_read(mixin):
    nodes = json.loads(THREAD_FIXTURE_PATH.read_text(encoding="utf-8"))
    matter = _ThreadFakeMatter(nodes, forbidden_nodes=THREAD_ROUTER_IDS)
    _module, obj = _thread_mesh_mixin(mixin, matter)
    ok, _values = obj.menuReportThreadMesh({"liveReadSleepy": True})[:2]
    assert ok
    assert set(matter.read_calls) == THREAD_NON_ROUTER_IDS
    assert not (set(matter.read_calls) & THREAD_ROUTER_IDS)


def test_wifi_only_fabric_is_a_friendly_success_not_an_error(mixin):
    # #334 post-review, B2.4: raw_count > 0 (matter-server DID report nodes),
    # diags is empty, and skipped is empty (every raw node parsed fine, none
    # of them is a Thread node) — the friendly answer, not an error.
    wifi_only = [{"node_id": 1, "available": True, "attributes": {"0/40/1": "Vendor"}}]
    _module, obj = _thread_mesh_mixin(mixin, _ThreadFakeMatter(wifi_only))
    ok, _values = obj.menuReportThreadMesh({"liveReadSleepy": True})[:2]
    assert ok
    assert "no Thread devices" in _log_body(obj)


def test_zero_raw_nodes_is_an_error_not_a_friendly_empty_mesh(mixin):
    # #334 post-review, B2.4: raw_count == 0 — matter-server itself reports no
    # commissioned nodes at all, which is a different (and worth flagging)
    # situation from "some nodes exist and none of them are Thread".
    _module, obj = _thread_mesh_mixin(mixin, _ThreadFakeMatter([]))
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": True})
    assert not ok
    assert "no commissioned nodes at all" in errors["liveReadSleepy"]


def test_raw_nodes_all_unaddressable_is_an_error_listing_the_skip_reasons(mixin):
    # #334 post-review, B2.4: raw_count > 0, diags empty, skipped non-empty —
    # every raw node matter-server returned was something this module could
    # not even address, which is a real failure, not an empty mesh.
    unaddressable = [{"attributes": {"0/53/1": 5}}]  # no node_id at all
    _module, obj = _thread_mesh_mixin(mixin, _ThreadFakeMatter(unaddressable))
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": True})
    assert not ok
    assert "could not be read" in errors["liveReadSleepy"]
    assert "no node_id" in errors["liveReadSleepy"]


def test_matter_server_unavailable_is_reported_not_printed_as_an_empty_mesh(mixin):
    """A get_nodes() failure is a failed call, not an empty mesh (root CLAUDE.md
    degradation-path convention) — must not read as "no Thread devices"."""
    matter = _ThreadFakeMatter([], get_nodes_error=ConnectionError("matter-server down"))
    _module, obj = _thread_mesh_mixin(mixin, matter)
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": False})
    assert not ok
    assert "matter-server could not be read" in errors["liveReadSleepy"]
    assert "no Thread devices" not in _log_body(obj)


def test_the_report_refuses_before_the_connection_is_up(mixin):
    _module, obj = mixin
    obj.matter = None
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": True})
    assert not ok and "not connected" in errors["liveReadSleepy"]


def test_the_report_refuses_when_matter_is_not_connected(mixin):
    # #334 post-review, B5.6: same guard, same wording, as
    # HttpApiMixin._thread_mesh_snapshot — obj.matter EXISTS here but is not
    # actually talking to matter-server yet.
    _module, obj = mixin
    obj.matter = Mock(connected=False)
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": True})
    assert not ok and "not connected to matter-server" in errors["liveReadSleepy"]


def test_menu_passes_the_shared_node_names_to_the_survey(mixin):
    # #334 post-review, B5.4: the menu used to omit node_names= entirely,
    # so a live-refreshed diag's name could disagree with the page's.
    nodes = json.loads(THREAD_FIXTURE_PATH.read_text(encoding="utf-8"))
    _module, obj = _thread_mesh_mixin(mixin, _ThreadFakeMatter(nodes))
    obj.device_sync.list_nodes.return_value = [(0x34, ["Grillplats socket"])]
    obj.device_sync.lookup.return_value = None  # no matterNode device yet — falls back
    ok, _values = obj.menuReportThreadMesh({"liveReadSleepy": False})[:2]
    assert ok
    assert "Grillplats socket" in _log_body(obj)


def test_prefers_the_matternode_devices_own_name_over_the_endpoint_join(mixin):
    # #334 post-review, B5.4: a multi-endpoint node's matterNode device name
    # wins over device_sync.list_nodes()'s comma-joined endpoint device names.
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(0x34, ["FP300 Motion", "FP300 Illuminance"])]
    obj.device_sync.lookup.return_value = 991
    sys.modules["indigo"].devices = {991: SimpleNamespace(name="FP300")}
    names = obj._thread_node_names()  # pylint: disable=protected-access
    assert names[0x34] == "FP300"


def test_falls_back_to_endpoint_join_with_no_matternode_device(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(0x34, ["FP300 Motion", "FP300 Illuminance"])]
    obj.device_sync.lookup.return_value = None
    names = obj._thread_node_names()  # pylint: disable=protected-access
    assert names[0x34] == "FP300 Motion, FP300 Illuminance"


def test_a_timeout_logs_before_reporting_it_in_the_dialog(mixin):
    # #334 post-review, B2.7: the dialog told the user to "see the Event Log"
    # for detail that was never actually written there.
    nodes = json.loads(THREAD_FIXTURE_PATH.read_text(encoding="utf-8"))
    _module, obj = mixin
    obj.matter = Mock(connected=True)
    obj.runtime = Mock()
    # close() the real coroutine run_survey builds — Mock().submit() never runs
    # it, and an unawaited coroutine otherwise warns at garbage-collection time.
    obj.runtime.submit.side_effect = lambda coro: (coro.close(), _Future(raises=FuturesTimeoutError()))[1]
    ok, _values, errors = obj.menuReportThreadMesh({"liveReadSleepy": True})
    assert not ok
    assert "see the Event Log" in errors["liveReadSleepy"]
    warn_msgs = [str(c) for c in obj.logger.warning.call_args_list]
    assert any("timed out" in m for m in warn_msgs)


# ---------------------------------------------------------------------------
# Prefs
# ---------------------------------------------------------------------------

def test_saving_the_log_commits_through_indigo(mixin):
    """Without savePluginPrefs the value survives only until the plugin stops,
    so every restart would re-report every device."""
    module, obj = mixin
    obj._save_survey_log('{"52": "fp"}')
    assert obj.pluginPrefs[module.SURVEY_LOG_PREF] == '{"52": "fp"}'
    assert sys.modules["indigo"].server.savePluginPrefs.called


def test_a_failed_save_is_logged_not_swallowed(mixin):
    """Issue #308: ``SurveyLog._persist`` is deliberately silent on the
    assumption that its caller logs — this pins that the caller actually
    does, so the documented contract is not just a comment nobody honours.
    """
    _module, obj = mixin
    sys.modules["indigo"].server.savePluginPrefs.side_effect = OSError("prefs are gone")

    obj._save_survey_log('{"52": "fp"}')  # must not raise

    assert obj.logger.warning.called
    warning_args = obj.logger.warning.call_args[0]
    assert "survey log" in warning_args[0]


def test_a_failed_save_logs_exactly_once(mixin):
    """To assert the swallow in ``SurveyLog._persist`` is never what does the
    logging, make the *caller's* own logging the only thing capable of firing:
    if this fired twice, something upstream (``_persist`` itself) would have
    to have logged too — which the #308 fix explicitly says it must not."""
    _module, obj = mixin
    sys.modules["indigo"].server.savePluginPrefs.side_effect = OSError("prefs are gone")

    obj._save_survey_log('{"52": "fp"}')

    assert obj.logger.warning.call_count == 1


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

def test_the_diagnostics_never_write_to_a_device():
    """Read-only, permanently — decided 2026-08-11, not left open.

    Asserted against the SOURCE rather than against behaviour: the point is not
    that today's code happens not to write, it is that no future edit quietly
    adds one. A generic writer is unvalidated by construction (nothing declares
    its bounds), device-global on multi-admin devices, and adds no capability
    over declaring another DeviceSetting — which is bounds-checked and verified
    by read-back. If this test ever fails, the fix is a declaration in
    matter_handlers/settings.py, not a change here.
    """
    for module_name in ("diagnostics_menu_mixin.py", "thread_survey.py"):
        source = (SERVER_PLUGIN / module_name).read_text(encoding="utf-8")
        for forbidden in ("MatterWrite", ".write(", "send_command", "MatterCommand"):
            assert forbidden not in source, (
                f"{module_name} contains {forbidden!r} — these diagnostics are "
                f"permanently read-only; add a DeviceSetting declaration instead"
            )
