"""The Matter menu's read-only diagnostics — issue #191, deliverables A and B.

``settings_report`` decides WHAT is reported and is tested next door; this pins
the Indigo-facing half: the pickers, the address the explorer resolves, and the
refusals. The refusals matter more than they look — every one of them is the
difference between a diagnostic and a guess.

The invariant behind the whole file: **nothing here writes to a device.** The
last test asserts that against the source rather than against behaviour, because
the point is that no future edit adds one.
"""
from __future__ import annotations

import importlib
import sys
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

SERVER_PLUGIN = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
                 / "Contents" / "Server Plugin")

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
# Prefs
# ---------------------------------------------------------------------------

def test_saving_the_log_commits_through_indigo(mixin):
    """Without savePluginPrefs the value survives only until the plugin stops,
    so every restart would re-report every device."""
    module, obj = mixin
    obj._save_survey_log('{"52": "fp"}')
    assert obj.pluginPrefs[module.SURVEY_LOG_PREF] == '{"52": "fp"}'
    assert sys.modules["indigo"].server.savePluginPrefs.called


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
    source = (SERVER_PLUGIN / "diagnostics_menu_mixin.py").read_text(encoding="utf-8")
    for forbidden in ("MatterWrite", ".write(", "send_command", "MatterCommand"):
        assert forbidden not in source, (
            f"diagnostics_menu_mixin.py contains {forbidden!r} — these diagnostics are "
            f"permanently read-only; add a DeviceSetting declaration instead"
        )
