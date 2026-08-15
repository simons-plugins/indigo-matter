"""ServerMenuMixin's "Share a Matter device with another ecosystem…" (issue
#210) — the reverse of the share model: Indigo already holds admin 1 on this
node, and this opens a commissioning window so a SECOND ecosystem can join.

Composes ``ServerMenuMixin`` with the two sibling mixins it reuses via MRO
(``DiagnosticsMenuMixin._fetch_node``, ``PairingMenuMixin._window_duration``)
— the same composition ``Plugin`` itself does — rather than mocking those
methods away, so the cross-mixin reuse is actually exercised, not merely
assumed. NOT test_pairing_menu.py's whole-``Plugin`` shape: no bridge node,
no export machinery, none of this touches.

Every assertion that matters is against ``obj.matter`` (the client mock) —
the E5 lesson test_pairing_menu.py's own header records: a confirm/refusal
gate that only ever inspects the ``errors`` dict cannot tell a real call from
one two branches away that happened to fill the same key.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import protocol
from fakes import COMMISSIONING_WINDOW_RESULT
from ws_json_client import LateResponse

SERVER_PLUGIN = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
                 / "Contents" / "Server Plugin")

NODE_ID = 0x34  # 52 decimal

#: A real node-details payload (IMPLEMENTATION.md §1.3 shape), carrying the
#: Operational Credentials fabric-count attributes (0/62/2, 0/62/3) the fabric
#: warning line reads. Plentiful on purpose (4 of 5 free) — a fixture whose
#: default reading itself warns would have every OTHER test in this file
#: silently trip that warning as a side effect (mutation-analysis finding).
RAW_NODE = {
    "node_id": NODE_ID,
    "available": True,
    "attributes": {
        "0/40/1": "IKEA",
        "0/40/3": "GRILLPLATS Plug",
        "0/62/2": 5,   # SupportedFabrics
        "0/62/3": 1,   # CommissionedFabrics — 4 free, plenty
        "1/6/0": True,
    },
}

RAW_NODE_OFFLINE = {**RAW_NODE, "available": False}
#: 1 of 5 free — under the Apple-two-slots threshold, so this one MUST warn.
RAW_NODE_TIGHT_FABRICS = {
    **RAW_NODE,
    "attributes": {**RAW_NODE["attributes"], "0/62/2": 5, "0/62/3": 4},
}
#: Exactly 2 free — the boundary the "< 2" check must NOT warn on.
RAW_NODE_BOUNDARY_FABRICS = {
    **RAW_NODE,
    "attributes": {**RAW_NODE["attributes"], "0/62/2": 5, "0/62/3": 3},
}
#: No Operational Credentials attributes at all — older firmware, or a
#: partial interview snapshot. Unknown must never be treated as zero.
RAW_NODE_NO_FABRIC_INFO = {
    "node_id": NODE_ID,
    "available": True,
    "attributes": {"0/40/1": "IKEA", "0/40/3": "GRILLPLATS Plug"},
}

#: Sentinels standing in for "the coroutine self.matter.<method>() returned" —
#: self.matter is a Mock, so calling it never produces a real coroutine; these
#: let the fake runtime.submit tell the two calls in _share_node apart.
_GET_NODE_CORO = object()
_OPEN_WINDOW_CORO = object()


class _Future:
    """A concurrent.futures stand-in: ``submit(...).result(timeout=…)``."""

    def __init__(self, value=None, raises=None):
        self._value, self._raises = value, raises
        #: The ``timeout`` argument of the last ``.result()`` call — lets a
        #: test pin WHICH of the two nested deadlines (SURVEY_READ_TIMEOUT for
        #: the fetch, SHARE_WINDOW_TIMEOUT for the window open) a given call
        #: actually used, not just that *some* call happened.
        self.result_timeout = None

    def result(self, timeout=None):
        self.result_timeout = timeout
        if self._raises is not None:
            raise self._raises
        return self._value


def _submitter(get_node_result=None, get_node_raises=None,
              open_window_result=None, open_window_raises=None, futures=None):
    """``futures``, if given, records every ``_Future`` created, in call
    order, so a test can inspect ``.result_timeout`` after the fact."""
    def _submit(coro):
        if coro is _GET_NODE_CORO:
            fut = _Future(get_node_result, get_node_raises)
        elif coro is _OPEN_WINDOW_CORO:
            fut = _Future(open_window_result, open_window_raises)
        else:
            fut = _Future(None)
        if futures is not None:
            futures.append(fut)
        return fut
    return _submit


@pytest.fixture
def mixin(mock_indigo_base, mock_logger):  # noqa: ARG001
    """A ServerMenuMixin composed with the two sibling mixins it borrows
    methods from via MRO — mirroring Plugin's own composition, not a mock of
    the borrowed methods."""
    for name in ("server_menu_mixin", "diagnostics_menu_mixin", "pairing_menu_mixin"):
        sys.modules.pop(name, None)
    server_menu_mixin = importlib.import_module("server_menu_mixin")
    diagnostics_menu_mixin = importlib.import_module("diagnostics_menu_mixin")
    pairing_menu_mixin = importlib.import_module("pairing_menu_mixin")

    class _Composed(server_menu_mixin.ServerMenuMixin,
                    diagnostics_menu_mixin.DiagnosticsMenuMixin,
                    pairing_menu_mixin.PairingMenuMixin):
        pass

    obj = _Composed()
    obj.logger = mock_logger
    obj.pluginPrefs = {}
    obj.runtime = Mock()
    obj.matter = Mock()
    obj.matter.get_node.return_value = _GET_NODE_CORO
    obj.matter.open_commissioning_window.return_value = _OPEN_WINDOW_CORO
    obj.device_sync = Mock()
    obj.device_sync.list_nodes.return_value = [(NODE_ID, ["Grillplats socket"])]
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_result=COMMISSIONING_WINDOW_RESULT)
    return server_menu_mixin, obj


# ---------------------------------------------------------------------------
# getShareableNodes — the picker
# ---------------------------------------------------------------------------

def test_picker_leads_with_a_select_a_device_row(mixin):
    """Execute opens a live commissioning window on whatever ends up
    selected, so Indigo's row-one preselection must not land on a real node."""
    _module, obj = mixin
    options = obj.getShareableNodes()
    assert options[0][0] == "0"
    assert "select a device" in options[0][1]
    assert options[1] == (str(NODE_ID), "Grillplats socket — node 0x34")


def test_picker_labels_a_node_with_no_indigo_devices(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(0x50, [])]
    options = obj.getShareableNodes()
    assert "(no Indigo devices)" in options[1][1]


def test_picker_says_so_when_nothing_is_commissioned(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = []
    options = obj.getShareableNodes()
    assert options == [("0", "(no Matter devices — none commissioned yet)")]


def test_picker_says_plugin_still_starting_when_device_sync_is_none(mixin):
    """Distinct from the empty-list case above: before startup finishes there
    is no way to know whether anything is commissioned, so claiming "none
    commissioned yet" would be the wrong cause."""
    _module, obj = mixin
    obj.device_sync = None
    assert obj.getShareableNodes() == [("0", "(plugin still starting)")]


def test_picker_degrades_instead_of_killing_the_dialog(mixin):
    _module, obj = mixin
    obj.device_sync.list_nodes.side_effect = RuntimeError("boom")
    options = obj.getShareableNodes()
    assert options[0][1].startswith("(error building list")


def test_picker_degrades_when_a_row_is_malformed_not_just_when_list_nodes_raises(mixin):
    """The row-building loop used to run OUTSIDE the try — a single bad
    node id (node_id_to_str choking on it) took the whole dialog down with
    it instead of degrading like every other picker in this file."""
    _module, obj = mixin
    obj.device_sync.list_nodes.return_value = [(object(), ["bad id"])]
    options = obj.getShareableNodes()
    assert options[0][1].startswith("(error building list")


# ---------------------------------------------------------------------------
# menuShareMatterNode — every refusal row in the §5 table
# ---------------------------------------------------------------------------

def test_refuses_before_the_plugin_has_finished_starting(mixin):
    _module, obj = mixin
    obj.runtime = None
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "starting" in errors["node"]
    obj.matter.open_commissioning_window.assert_not_called()


def test_refuses_with_no_selection(mixin):
    _module, obj = mixin
    ok, values, errors = obj.menuShareMatterNode({"node": "", "duration": "900"})
    assert ok is False
    assert "Select a device" in errors["node"]


def test_refuses_on_the_sentinel_selection(mixin):
    """Zero commissioned nodes: the picker's own unpickable row carries
    NO_SELECTION_ID, so Execute lands here too."""
    _module, obj = mixin
    ok, values, errors = obj.menuShareMatterNode({"node": "0", "duration": "900"})
    assert ok is False
    assert "Select a device" in errors["node"]


def test_duration_out_of_band_reuses_the_pairing_validator(mixin):
    """Pins the cross-mixin MRO reuse of PairingMenuMixin._window_duration:
    the reused validator writes errors["duration"], so the dialog field this
    menu declares MUST be named "duration"."""
    _module, obj = mixin
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "60"})
    assert ok is False
    assert "180" in errors["duration"] and "900" in errors["duration"]
    obj.matter.open_commissioning_window.assert_not_called()


def test_node_unknown_when_get_node_returns_nothing(mixin):
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(get_node_result=None)
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "does not know this node" in errors["node"]
    obj.matter.open_commissioning_window.assert_not_called()


def test_node_unknown_when_the_fetch_itself_fails(mixin):
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(get_node_raises=ConnectionError("no socket"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "did not answer" in errors["node"]
    obj.matter.open_commissioning_window.assert_not_called()
    # _fetch_node wraps this in its OWN MatterUnavailable — ordinary
    # connectivity, not a code bug, so only a trail at debug, never error.
    assert obj.logger.debug.called
    assert not obj.logger.error.called


def test_fetch_node_unexpected_exception_type_logs_error_with_traceback(mixin):
    """Anything escaping _fetch_node that is NOT its own MatterUnavailable
    would be a code bug there, mislabeled as connectivity if it only got a
    debug line — this proves the differentiated branch actually fires."""
    _module, obj = mixin
    obj._fetch_node = Mock(side_effect=RuntimeError("boom"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert obj.logger.error.called
    assert obj.logger.exception.called


def test_offline_node_refuses_fast_before_opening_a_window(mixin):
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(get_node_result=RAW_NODE_OFFLINE)
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "currently reports this device as unreachable" in errors["node"]
    assert "in a moment" in errors["node"]  # acknowledges the flag can be stale
    assert "Nothing was changed" in errors["node"]
    obj.matter.open_commissioning_window.assert_not_called()


@pytest.mark.parametrize("code", [3, 4, "3", "4", "03", "04"])
def test_offline_reported_at_window_open_time_too(mixin, code):
    """codes 3 (NodeNotReady) / 4 (NodeNotResolving) from the open call itself
    read the same as the available=False fast-path — including when the wire
    sends them as a zero-padded string (int(x, 0) rejects "03" outright)."""
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_raises=protocol.ProtocolError(code, "not ready"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "currently reports this device as unreachable" in errors["node"]
    # Every refusal branch leaves a record — "see the log" must point at something.
    warned = str(obj.logger.warning.call_args)
    assert "0x34" in warned and str(code) in warned and "not ready" in warned


def test_node_not_exists_at_window_open_time(mixin):
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE,
        open_window_raises=protocol.ProtocolError(protocol.ERR_NODE_NOT_EXISTS, "gone"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "does not know this node" in errors["node"]
    assert obj.logger.warning.called


def test_window_already_open_is_reported_honestly(mixin):
    """codes 7 (SDKStackError) / 0 (UnknownError) are not reliably
    distinguishable from a window already being open — the message says so
    and names the code-cannot-be-recovered fact rather than guessing."""
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_raises=protocol.ProtocolError(7, "SDK error"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "already be open" in errors["node"]
    assert "cannot be recovered" in errors["node"]
    warned = str(obj.logger.warning.call_args)
    assert "0x34" in warned and "7" in warned and "SDK error" in warned


def test_unnamed_protocol_error_codes_get_the_same_hedge(mixin):
    """protocol.py's own error table lists ~10 codes (InvalidArguments 8,
    InvalidCommand 9, IcdMultiAdmin 100, …) that all land in the fallthrough
    — the message must not assert a single cause for that many possibilities."""
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_raises=protocol.ProtocolError(8, "bad args"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "error 8" in errors["node"]
    assert "may already be open" in errors["node"]
    assert "cannot be recovered" in errors["node"]


def test_rpc_timeout_says_the_window_may_still_have_opened(mixin):
    from concurrent.futures import TimeoutError as FuturesTimeoutError
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_raises=FuturesTimeoutError())
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "may still have opened" in errors["node"]
    assert "code is lost" in errors["node"]
    warned = str(obj.logger.warning.call_args)
    assert "0x34" in warned


def test_disconnected_from_matter_server(mixin):
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_raises=ConnectionError("socket gone"))
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "Not connected to matter-server" in errors["node"]
    warned = str(obj.logger.warning.call_args)
    assert "0x34" in warned and "socket gone" in warned


def test_unparseable_result_logs_the_raw_payload_and_warns_the_code_is_lost(mixin):
    _module, obj = mixin
    bad_result = {"setup_qr_code": "MT:-24J0AFN00KA0648G00"}  # no manual code
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_result=bad_result)
    ok, values, errors = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is False
    assert "unexpected shape" in errors["node"]
    assert "code is lost" in errors["node"]
    logged = str(obj.logger.error.call_args)
    assert "setup_qr_code" in logged  # the raw payload, for diagnosis


def test_fabric_slots_short_warns_but_still_proceeds(mixin):
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE_TIGHT_FABRICS, open_window_result=COMMISSIONING_WINDOW_RESULT)
    ok, values = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is True  # diagnostic only, never blocking
    warned = str(obj.logger.warning.call_args)
    assert "fabric slot" in warned and "Apple" in warned


def test_fabric_slots_plentiful_does_not_warn(mixin):
    _module, obj = mixin
    obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})  # RAW_NODE is plentiful
    obj.logger.warning.assert_not_called()


def test_fabric_slots_boundary_of_two_does_not_warn(mixin):
    """Exactly 2 free is the "still fits Apple's two slots" boundary — the
    warning only fires BELOW it."""
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE_BOUNDARY_FABRICS, open_window_result=COMMISSIONING_WINDOW_RESULT)
    obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    obj.logger.warning.assert_not_called()


def test_fabric_slots_unknown_proceeds_silently(mixin):
    """No Operational Credentials attributes at all (older firmware, partial
    interview) — unknown must never be treated as zero, and the share still
    goes through: the fabric line becomes informational text in the success
    log, not a refusal."""
    _module, obj = mixin
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE_NO_FABRIC_INFO, open_window_result=COMMISSIONING_WINDOW_RESULT)
    ok, values = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "900"})
    assert ok is True
    obj.logger.warning.assert_not_called()
    logged = str(obj.logger.info.call_args)
    assert "fabric slot count unavailable" in logged


# ---------------------------------------------------------------------------
# Happy path — asserted against the CLIENT MOCK, not merely the return value
# ---------------------------------------------------------------------------

def test_happy_path_opens_the_window_with_the_chosen_duration_and_logs_the_codes(mixin):
    module, obj = mixin
    futures: list = []
    obj.runtime.submit.side_effect = _submitter(
        get_node_result=RAW_NODE, open_window_result=COMMISSIONING_WINDOW_RESULT, futures=futures)
    ok, values = obj.menuShareMatterNode({"node": str(NODE_ID), "duration": "300"})
    assert ok is True
    # The window-open call used the OUTER deadline (SHARE_WINDOW_TIMEOUT) —
    # a mutation swapping which of the two nested timeouts reaches .result()
    # would pass every other assertion here.
    from plugin_constants import SHARE_WINDOW_TIMEOUT
    assert futures[-1].result_timeout == SHARE_WINDOW_TIMEOUT
    # duration is passed by KEYWORD — a mutation that reorders
    # open_commissioning_window's parameters (duration before context, say)
    # would pass just as happily with a positional call; this pins the seam.
    # context is our own ShareWindowRequest — the correlation key a LATE
    # answer (issue #210) is matched back against. Built from the fixture's
    # OWN freshly-imported module, not a top-level import of this test file
    # — the fixture reloads server_menu_mixin per test, and a dataclass's
    # generated __eq__ compares __class__ identity, not just field values.
    assert obj.matter.open_commissioning_window.call_args == (
        (NODE_ID,), {"duration": 300, "context": module.ShareWindowRequest(NODE_ID, 300)})
    logged = str(obj.logger.info.call_args)
    assert COMMISSIONING_WINDOW_RESULT["setup_manual_code"] in logged
    assert COMMISSIONING_WINDOW_RESULT["setup_qr_code"] in logged
    assert "approximately" in logged
    assert "device you own" in logged


def test_happy_path_defaults_duration_to_900_when_left_blank(mixin):
    _module, obj = mixin
    obj.menuShareMatterNode({"node": str(NODE_ID), "duration": ""})
    call = obj.matter.open_commissioning_window.call_args
    assert call.args == (NODE_ID,)
    assert call.kwargs["duration"] == 900


# ---------------------------------------------------------------------------
# _note_late_share_response — matter-server answers after this plugin gave
# up waiting (issue #210, mirroring commission_jobs' #23 late-response path)
# ---------------------------------------------------------------------------

def test_late_success_logs_the_codes_at_warning_with_the_late_prefix(mixin):
    module, obj = mixin
    late = LateResponse(
        message_id="42", context=module.ShareWindowRequest(NODE_ID, 300),
        error=None, result=COMMISSIONING_WINDOW_RESULT)
    obj._note_late_share_response(late)
    logged = str(obj.logger.warning.call_args)
    assert "answered late" in logged
    assert "DID open after all" in logged
    assert COMMISSIONING_WINDOW_RESULT["setup_manual_code"] in logged
    obj.logger.info.assert_not_called()


def test_late_error_logs_at_info_naming_no_window_opened(mixin):
    module, obj = mixin
    late = LateResponse(
        message_id="42", context=module.ShareWindowRequest(NODE_ID, 300),
        error=(7, "SDK error"), result=None)
    obj._note_late_share_response(late)
    logged = str(obj.logger.info.call_args)
    assert "no window opened" in logged
    assert "7" in logged and "SDK error" in logged
    obj.logger.warning.assert_not_called()


def test_late_unparseable_result_logs_the_raw_payload(mixin):
    module, obj = mixin
    late = LateResponse(
        message_id="42", context=module.ShareWindowRequest(NODE_ID, 300),
        error=None, result={"setup_qr_code": "MT:XYZ"})  # no manual code
    obj._note_late_share_response(late)
    logged = str(obj.logger.error.call_args)
    assert "setup_qr_code" in logged
    obj.logger.warning.assert_not_called()


def test_late_response_with_a_foreign_context_is_ignored(mixin):
    """The mechanism commission_jobs.CommissionJobs.note_late_response
    already relies on: several senders share one on_late_response hook, and
    isinstance is how each claims only its own."""
    _module, obj = mixin
    late = LateResponse(message_id="1", context="some other request's context",
                        error=None, result={"node_id": 1})
    obj._note_late_share_response(late)
    obj.logger.warning.assert_not_called()
    obj.logger.info.assert_not_called()
    obj.logger.error.assert_not_called()


# ---------------------------------------------------------------------------
# The SHARE_WINDOW_TIMEOUT / SHARE_WINDOW_RPC_TIMEOUT invariant (§6)
# ---------------------------------------------------------------------------

def test_outer_share_timeout_is_strictly_above_the_inner_rpc_timeout():
    from matter_client import SHARE_WINDOW_RPC_TIMEOUT
    from plugin_constants import SHARE_WINDOW_TIMEOUT
    assert SHARE_WINDOW_TIMEOUT > SHARE_WINDOW_RPC_TIMEOUT
