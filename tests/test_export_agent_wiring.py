"""E6/E7: the export engine's agent lifecycle, its §5 pairing events, and §5.5's switch.

Three things land here that `test_export_bridge.py` predates:

* **the LaunchAgent seams (XG5/XAC1)** — the bridge node is started by the
  empty→non-empty allow-list transition and stopped by the reverse, *after* the
  un-export has landed. A fresh install must touch nothing at all;
* **the §5 pairing events** — ``fabrics_changed``/``commissioned``/
  ``decommissioned``/``window_closed`` were emitted by the node from E5 and
  consumed by nobody, so pairing activity was invisible without polling;
* **PRD §5.5's wholesale switch** — and specifically that turning it OFF is not
  the same statement as emptying the allow-list: it must never un-export.

References to ``§N`` are ``docs/BRIDGE_PROTOCOL.md``.
"""
from __future__ import annotations

import importlib

import pytest

import bridge_protocol
import export_catalog
from export_store import ExportEntry, ExportStore

from fakes import (
    DimmerDevice,
    FakeBridgeClient,
    FakeIndigoDevices,
    InlineExecutor,
    RecordingRuntime,
    RelayDevice,
)

OURS = export_catalog.DEFAULT_PLUGIN_ID


@pytest.fixture
def bridge_mod(mock_indigo_base):
    import export_handlers
    import export_bridge as module
    importlib.reload(export_handlers)
    importlib.reload(module)
    return module


@pytest.fixture
def devices(mock_indigo_base):
    collection = FakeIndigoDevices([
        RelayDevice(101, "Study Plug"),
        DimmerDevice(102, "Hall Dimmer"),
    ])
    mock_indigo_base.devices = collection
    return collection


class AgentHarness:
    """An ExportBridge with the E7 agent seams recorded rather than performed."""

    def __init__(self, module, mock_logger, devices, entries=(), prefs=None):
        self.logger = mock_logger
        self.prefs: dict = dict(prefs or {})
        self.devices = devices
        self.store = ExportStore(lambda: self.prefs, mock_logger)
        for entry in entries:
            self.store.upsert(entry)
        self.runtime = RecordingRuntime()
        self.clients: list[FakeBridgeClient] = []
        #: Ordered agent seam calls — the ORDER is part of the contract.
        self.agent: list[str] = []
        self.diagnosis: str | None = None
        self.start_raises: Exception | None = None
        self.bridge = module.ExportBridge(
            self.store, self.runtime, mock_logger, lambda: self.prefs,
            plugin_version="2026.8.1", plugin_id=OURS,
            device_getter=lambda dev_id: self.devices.get(dev_id),
            client_factory=self._client,
            executor_factory=InlineExecutor,
            agent_start=self._agent_start,
            agent_stop=lambda: self.agent.append("stop"),
            agent_diagnose=lambda: self.diagnosis,
        )

    def _agent_start(self):
        self.agent.append("start")
        if self.start_raises is not None:
            raise self.start_raises

    def _client(self, logger, prefs, **kwargs):
        client = FakeBridgeClient(logger, prefs, **kwargs)
        self.clients.append(client)
        return client

    @property
    def client(self) -> FakeBridgeClient:
        assert self.clients, "no bridge client was created"
        return self.clients[-1]


def _rendered(calls) -> str:
    """Lazily-formatted log calls, as the user would actually read them."""
    return " ".join(str(call.args[0]) % call.args[1:] if len(call.args) > 1
                    else str(call.args[0])
                    for call in calls)


def warnings_of(logger) -> str:
    return _rendered(logger.warning.call_args_list)


def infos_of(logger) -> str:
    return _rendered(logger.info.call_args_list)


def errors_of(logger) -> str:
    return _rendered(logger.error.call_args_list)


# ---------------------------------------------------------------------------
# XAC1 / XG5 — the agent follows the allow-list, nothing else
# ---------------------------------------------------------------------------

class TestAgentLifecycle:
    def test_a_fresh_install_starts_no_agent_and_no_client(self, bridge_mod, mock_logger,
                                                           devices):
        """⊗ XAC1. The whole acceptance criterion in one assertion pair."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [])
        h.bridge.exports_changed()
        assert h.agent == []
        assert h.clients == []
        assert h.bridge.active is False

    def test_the_first_export_starts_the_agent_BEFORE_the_client(self, bridge_mod,
                                                                 mock_logger, devices):
        """Order matters: a client built first spends its whole backoff dialling
        a port nothing is listening on yet."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [])
        h.store.upsert(ExportEntry(101, "onOffLight"))
        h.bridge.exports_changed()
        assert h.agent == ["start"]
        assert len(h.clients) == 1
        assert h.client.ran is True

    def test_a_second_export_does_not_restart_the_agent(self, bridge_mod, mock_logger,
                                                        devices):
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.store.upsert(ExportEntry(102, "dimmableLight"))
        h.bridge.exports_changed()
        assert h.agent == ["start"], "start() is idempotent, so the seam must be too"

    def test_emptying_the_allow_list_stops_the_agent_AFTER_the_un_export(
            self, bridge_mod, mock_logger, devices):
        """⊗ The ordering XG5's other half depends on.

        Stopping first would take the node down with the §3.1 removal request
        still unsent, leaving the accessories in every paired ecosystem and the
        debt pointing at a bridge the plugin has just switched off.
        """
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        client = h.client
        h.store.remove(101)
        h.bridge.exports_changed()
        # The un-export went out as the deliberate replace_all attach…
        assert ("attach", [], True) in client.calls
        assert client.closed is True
        # …and only then was the agent stopped.
        assert h.agent == ["start", "stop"]

    def test_the_agent_is_still_stopped_when_the_un_export_attach_FAILED(
            self, bridge_mod, mock_logger, devices):
        """The socket is closed and the list is empty either way; the DEBT is what
        carries the un-export forward, and it survives in prefs."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.client.fail["attach"] = RuntimeError("node went away")
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.agent == ["start", "stop"]
        assert h.prefs.get(bridge_mod.PREF_PENDING_REPLACE_ALL) == 1

    def test_a_re_export_during_the_un_export_keeps_the_agent_up(self, bridge_mod,
                                                                 mock_logger, devices):
        """The deferred start wins over the stop — there is something to serve again.

        The race is real: ``_replace_all_then_stop`` sets ``client = None`` the
        instant it fires, so a user who empties the list and immediately re-adds
        a device lands inside the un-export's own coroutine. Stopping the agent
        there would take the node down underneath the client that is about to be
        rebuilt.
        """
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.agent.clear()
        client = h.client

        # Re-export from inside the un-export attach, which is where a user who
        # is quick with the dialog actually lands.
        original_attach = client.attach

        async def _attach_then_readd(endpoints=None, *, replace_all=False, timeout=None):
            result = await original_attach(endpoints, replace_all=replace_all, timeout=timeout)
            h.store.upsert(ExportEntry(102, "dimmableLight"))
            h.bridge.start()
            return result
        client.attach = _attach_then_readd

        h.store.remove(101)
        h.bridge.exports_changed()

        assert "stop" not in h.agent, "something is exported again — do not stop the node"
        assert len(h.clients) == 2, "the deferred start must actually happen"

    def test_an_agent_that_will_not_start_still_builds_the_client(self, bridge_mod,
                                                                  mock_logger, devices):
        """A launchd fault must not also remove the diagnosis.

        The client's unreachable path is what reports the outage WITH the node's
        own error log attached; refusing to build it would replace one diagnosis
        with none.
        """
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start_raises = OSError("launchctl exploded")
        h.bridge.exports_changed()
        assert h.clients, "the client must still exist"
        said = errors_of(mock_logger)
        assert "LaunchAgent" in said
        # And the fault must be scoped to export.
        assert "inbound Matter control are unaffected" in said

    def test_an_agent_that_will_not_stop_is_a_warning_not_a_failure(self, bridge_mod,
                                                                    mock_logger, devices):
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.bridge._agent_stop = lambda: (_ for _ in ()).throw(OSError("nope"))
        h.store.remove(101)
        h.bridge.exports_changed()
        assert "could not stop" in warnings_of(mock_logger)
        assert "pairings are untouched" in warnings_of(mock_logger)

    def test_a_DEBT_discharged_on_reconnect_also_stops_the_agent(self, bridge_mod,
                                                                 mock_logger, devices):
        """⊗ XAC7's recovery path, and the only caller of `_stop_agent_off_loop`.

        Deleting that fire left the whole suite green: the debt is paid, the
        store is empty, the client is closed — and the agent runs forever, with
        an unpaired bridge node serving nothing. This is the state a user reaches
        by emptying the allow-list while the node is unreachable and then
        reloading the plugin, which is exactly when an un-export fails.
        """
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.client.fail["attach"] = RuntimeError("node went away")
        h.store.remove(101)
        h.bridge.exports_changed()                       # records the debt, stops the agent
        assert h.prefs.get(bridge_mod.PREF_PENDING_REPLACE_ALL) == 1

        # A later session brings the client back to discharge the debt (XG5's one
        # exception), the attach lands, and there is still nothing to export.
        h.agent.clear()
        h.bridge.exports_changed()
        assert h.agent == ["start"], "the debt path is the one that starts with an empty list"
        status = bridge_protocol.parse_status({
            "commissioned": True, "endpointCount": 0, "endpoints": [], "drift": [],
            "driftChecked": True, "warnings": [], "fabrics": [],
        })
        h.bridge._on_attached(status, carried_replace_all=True)
        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs, "the debt must be discharged"
        assert h.bridge.active is False, "nothing is exported — the socket goes"
        assert h.agent == ["start", "stop"], \
            "the agent must go too, or an unpaired node serves nothing forever"

    def test_a_debt_discharged_alongside_REAL_endpoints_keeps_the_agent(self, bridge_mod,
                                                                        mock_logger, devices):
        """The other half: an attach that also carried endpoints is an export to
        keep serving, not one to hang up on."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.bridge._record_pending_replace_all(1)
        h.agent.clear()
        status = bridge_protocol.parse_status({
            "commissioned": True, "endpointCount": 1, "endpoints": [], "drift": [],
            "driftChecked": True, "warnings": [], "fabrics": [],
        })
        h.bridge._on_attached(status, carried_replace_all=True)
        assert "stop" not in h.agent

    def test_a_start_that_RAISES_still_leaves_the_agent_stoppable(self, bridge_mod,
                                                                  mock_logger, devices):
        """⊗ The `_agent_started` latch is set BEFORE the attempt, deliberately.

        A start that raised may still have written a plist and bootstrapped a
        job, so "we never touched launchd" is a claim we cannot make afterwards —
        and the latch is what gates the stop that would clean it up. Moving the
        assignment after `_agent_start()` survives every other test in this file.
        """
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.start_raises = OSError("launchctl exploded halfway")
        h.bridge.exports_changed()
        assert h.agent == ["start"]
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.agent == ["start", "stop"], \
            "a start that raised may still have left a job behind — it must be stoppable"

    def test_note_agent_stopped_clears_the_latch(self, bridge_mod, mock_logger, devices):
        """Something outside the engine (the stop menu) took the agent down."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.bridge.note_agent_stopped()
        h.agent.clear()
        h.store.remove(101)
        h.bridge.exports_changed()
        assert h.agent == [], "this session no longer owns the agent; it must not bootout"

    def test_the_unreachable_report_carries_the_agents_diagnosis(self, bridge_mod,
                                                                 mock_logger, devices):
        """"Connection refused" is what a missing package, a bound Matter port and
        a crash-loop all look like at the socket. The agent's log tells them apart."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.diagnosis = "Recent bridge node errors:\nlisten EADDRINUSE 5540"
        h.bridge._on_unreachable(4)
        assert "EADDRINUSE" in warnings_of(mock_logger)

    def test_a_failing_diagnostic_costs_only_the_extra_sentence(self, bridge_mod,
                                                               mock_logger, devices):
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        h.bridge._agent_diagnose = lambda: (_ for _ in ()).throw(OSError("no log"))
        h.bridge._on_unreachable(4)
        assert "not responding" in warnings_of(mock_logger)


# ---------------------------------------------------------------------------
# PRD §5.5 — the wholesale switch
# ---------------------------------------------------------------------------

class TestExportEnabledSwitch:
    def test_absent_means_ON(self, bridge_mod, mock_logger, devices):
        """⊗ Every install predating E6 has a working allow-list and no key."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        assert h.bridge.enabled is True
        h.bridge.exports_changed()
        assert h.clients and h.agent == ["start"]

    @pytest.mark.parametrize("raw,expected", [
        (True, True), (False, False),
        ("true", True), ("false", False), ("no", False), ("off", False), ("0", False),
        ("YES", True), ("", True), ("nonsense", True), (None, True),
    ])
    def test_the_pref_fails_OPEN(self, raw, expected, bridge_mod, mock_logger, devices):
        """Opposite direction from the controller's attestation flag, deliberately:
        the harm of misreading THIS one is un-running a working export."""
        h = AgentHarness(bridge_mod, mock_logger, devices, [],
                         prefs={bridge_mod.PREF_EXPORT_ENABLED: raw})
        assert h.bridge.enabled is expected

    def test_switched_off_never_starts_anything(self, bridge_mod, mock_logger, devices):
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")],
                         prefs={bridge_mod.PREF_EXPORT_ENABLED: False})
        h.bridge.exports_changed()
        assert h.clients == [] and h.agent == []

    def test_switching_off_disconnects_but_NEVER_un_exports(self, bridge_mod, mock_logger,
                                                            devices):
        """⊗ The one that would delete every accessory from every ecosystem.

        Turning a switch off is not the statement "remove everything" (PRD §7) —
        it must not send the §3.1 replace_all, must not record a debt, and must
        leave the allow-list alone so ticking it back on restores everything.
        """
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        client = h.client
        client.calls.clear()

        h.prefs[bridge_mod.PREF_EXPORT_ENABLED] = False
        h.bridge.exports_changed()

        assert not [call for call in client.calls if call[0] == "attach"], \
            "switching export off must never send a replace_all attach"
        assert bridge_mod.PREF_PENDING_REPLACE_ALL not in h.prefs
        assert len(h.store) == 1, "the allow-list is the user's declaration"
        assert client.closed is True
        assert h.agent == ["start", "stop"]
        assert "LEFT paired" in infos_of(mock_logger)

    def test_switching_back_on_reconnects(self, bridge_mod, mock_logger, devices):
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")],
                         prefs={bridge_mod.PREF_EXPORT_ENABLED: False})
        h.bridge.exports_changed()
        assert h.clients == []
        h.prefs[bridge_mod.PREF_EXPORT_ENABLED] = True
        h.bridge.exports_changed()
        assert len(h.clients) == 1 and h.agent == ["start"]


# ---------------------------------------------------------------------------
# §5 pairing events — emitted since E5, consumed by nobody until E6
# ---------------------------------------------------------------------------

class TestPairingEvents:
    def _started(self, bridge_mod, mock_logger, devices):
        h = AgentHarness(bridge_mod, mock_logger, devices, [ExportEntry(101, "onOffLight")])
        h.bridge.exports_changed()
        return h

    def test_all_four_callbacks_reach_the_client(self, bridge_mod, mock_logger, devices):
        """⊗ The wiring itself: these were declared, documented, carried by golden
        frames and dispatched by the client — and handed to it by nothing."""
        h = self._started(bridge_mod, mock_logger, devices)
        for name in ("on_fabrics_changed", "on_commissioned", "on_decommissioned",
                     "on_window_closed"):
            assert h.client.kwargs.get(name) is not None, f"{name} was not wired"

    def test_fabrics_changed_is_logged_and_remembered(self, bridge_mod, mock_logger,
                                                      devices):
        h = self._started(bridge_mod, mock_logger, devices)
        fabrics = bridge_protocol.parse_fabrics(
            [{"fabricIndex": 1, "label": "", "vendorId": 0x1349}])
        h.bridge._on_fabrics_changed(fabrics, "added")
        assert "Apple" in infos_of(mock_logger)
        assert h.bridge.fabrics == fabrics

    def test_an_attach_seeds_the_fabric_set(self, bridge_mod, mock_logger, devices):
        """fabrics_changed fires on CHANGES, so a long-paired bridge emits nothing
        at all — the attach's own §4.3 report is the only source on a fresh
        connection."""
        h = self._started(bridge_mod, mock_logger, devices)
        status = bridge_protocol.parse_status({
            "commissioned": True, "endpointCount": 1, "endpoints": [], "drift": [],
            "driftChecked": True, "warnings": [],
            "fabrics": [{"fabricIndex": 2, "label": "", "vendorId": 0x6006}],
        })
        h.bridge._on_attached(status, False)
        assert [f.fabric_index for f in h.bridge.fabrics] == [2]

    def test_commissioned_says_what_to_do_next(self, bridge_mod, mock_logger, devices):
        h = self._started(bridge_mod, mock_logger, devices)
        h.bridge._on_commissioned()
        said = infos_of(mock_logger)
        assert "PAIRED for the first time" in said
        assert "no longer works" in said, "the original code is dead — say so"

    def test_decommissioned_is_a_WARNING_and_clears_the_state(self, bridge_mod,
                                                              mock_logger, devices):
        """An ecosystem removing US takes every accessory with it, and this line
        is the only notice the user gets."""
        h = self._started(bridge_mod, mock_logger, devices)
        h.bridge.window_expires_at = "2026-08-05T12:00:00Z"
        h.bridge._on_decommissioned()
        assert "no longer paired with ANY ecosystem" in warnings_of(mock_logger)
        assert h.bridge.fabrics == []
        assert h.bridge.window_expires_at is None

    @pytest.mark.parametrize("reason,expected", [
        ("commissioned", "completed commissioning"),
        ("expired", "expired without an ecosystem"),
    ])
    def test_window_closed_distinguishes_its_two_reasons(self, reason, expected, bridge_mod,
                                                         mock_logger, devices):
        h = self._started(bridge_mod, mock_logger, devices)
        h.bridge.note_window_opened("2026-08-05T12:00:00Z")
        h.bridge._on_window_closed(reason)
        assert expected in infos_of(mock_logger)
        assert h.bridge.window_expires_at is None

    def test_note_window_opened_actually_records_it(self, bridge_mod, mock_logger, devices):
        """⊗ Its body → `pass` survived the whole suite. It is the only thing the
        §5.5 readout has to report a window the pairing menu just opened."""
        h = self._started(bridge_mod, mock_logger, devices)
        h.bridge.note_window_opened("2026-08-05T12:00:00Z")
        assert h.bridge.window_expires_at == "2026-08-05T12:00:00Z"

    def test_an_attach_RE_DERIVES_the_window_from_the_node(self, bridge_mod, mock_logger,
                                                           devices):
        """⊗ The window cache's other failure direction.

        `window_expires_at` is written by the pairing menu and cleared only by
        the §5 `window_closed` event, which the node does NOT send on shutdown.
        So a plugin reload during a real window left it None and the readout HID
        an open window; the node is the only thing that knows, and an attach is
        the moment to ask.
        """
        h = self._started(bridge_mod, mock_logger, devices)
        h.client.pairing = bridge_protocol.PairingReport(
            commissioned=True, window_open=True, window_expires_at="2026-09-01T10:00:00Z",
            manual_pairing_code="1", qr_pairing_code="MT:1", fabrics=[])
        status = bridge_protocol.parse_status({
            "commissioned": True, "endpointCount": 1, "endpoints": [], "drift": [],
            "driftChecked": True, "warnings": [], "fabrics": [],
        })
        h.bridge._on_attached(status, False)
        assert h.bridge.window_expires_at == "2026-09-01T10:00:00Z"

    def test_a_closed_window_on_the_node_clears_a_stale_local_one(self, bridge_mod,
                                                                  mock_logger, devices):
        h = self._started(bridge_mod, mock_logger, devices)
        h.bridge.window_expires_at = "2020-01-01T00:00:00Z"
        h.client.pairing = bridge_protocol.PairingReport(
            commissioned=True, window_open=False, window_expires_at=None,
            manual_pairing_code=None, qr_pairing_code=None, fabrics=[])
        status = bridge_protocol.parse_status({
            "commissioned": True, "endpointCount": 1, "endpoints": [], "drift": [],
            "driftChecked": True, "warnings": [], "fabrics": [],
        })
        h.bridge._on_attached(status, False)
        assert h.bridge.window_expires_at is None


def test_fabrics_are_described_by_vendor_not_by_their_own_label(bridge_mod):
    """A fabric's `label` is whatever the commissioner wrote — for Apple, a
    UUID-ish string that tells nobody anything. The index is always shown
    because that is what §3.9 removes a fabric BY."""
    fabric = bridge_protocol.FabricInfo(fabric_index=1, label="", vendor_id=0x1349)
    assert bridge_mod.describe_fabric(fabric) == "Apple Home (index 1)"
    labelled = bridge_protocol.FabricInfo(fabric_index=3, label="Kitchen Hub",
                                          vendor_id=0x6006)
    assert bridge_mod.describe_fabric(labelled) == "Google — Kitchen Hub (index 3)"
    unknown = bridge_protocol.FabricInfo(fabric_index=9, label="", vendor_id=0x1234)
    assert bridge_mod.describe_fabric(unknown) == "vendor 0x1234 (index 9)"


# ---------------------------------------------------------------------------
# The vendor table — the labels on a DESTRUCTIVE picker
# ---------------------------------------------------------------------------

#: matter.js's own list of ecosystem admin vendors, vendored under bridge-node.
#: The ONLY thing in this repo that is both authoritative about these ids and
#: version-controlled with us: the CSA's ledger is authoritative but is a network
#: call, and a test that mirrored our own table would pass on any value at all —
#: which is exactly how "0x1075 = SmartThings" (not an issued id) and "0x100B =
#: Google" (it is Signify) survived to ship on the unpair picker.
MATTER_JS_VENDOR_SOURCE = (
    "@matter/node/src/behavior/system/icd/IcdMultiAdminError.ts"
)


def _matter_js_vendor_names() -> dict:
    """`{vendor_id: comment}` parsed out of matter.js's TRUSTED_ECOSYSTEM_VENDORS."""
    import re
    from pathlib import Path

    source = (Path(__file__).parent.parent / "bridge-node" / "node_modules"
              / MATTER_JS_VENDOR_SOURCE)
    if not source.exists():
        pytest.skip(f"{source} is absent — run `npm install` in bridge-node/")
    text = source.read_text(encoding="utf-8")
    found = {int(vid, 16): comment.strip()
             for vid, comment in re.findall(
                 r"VendorId\(0x([0-9a-fA-F]+)\)\s*/\*\s*([^*]+?)\s*\*/", text)}
    assert len(found) >= 3, (
        f"parsed only {len(found)} vendor ids out of {source} — the upstream format has "
        "changed and this check has quietly stopped checking anything")
    return found


def test_the_vendor_table_agrees_with_the_vendored_matter_js(bridge_mod):
    """⊗ Every id matter.js names must be in our table, under a matching name.

    Not "a name we like": every word of ours has to appear in matter.js's own
    comment for that id, so a plausible-but-wrong relabel fails here. This is the
    check that would have caught both of the errors this table shipped with.
    """
    upstream = _matter_js_vendor_names()
    for vendor_id, comment in upstream.items():
        ours = bridge_mod.VENDOR_NAMES.get(vendor_id)
        assert ours is not None, (
            f"matter.js names 0x{vendor_id:04X} ({comment}) as an ecosystem admin vendor and "
            "our table does not — the unpair picker would show it as raw hex")
        lowered = comment.lower()
        for word in ours.replace("(", " ").replace(")", " ").split():
            assert word.lower() in lowered, (
                f"we call 0x{vendor_id:04X} {ours!r}; matter.js calls it {comment!r}")


def test_apple_appears_twice_because_apple_creates_two_fabrics(bridge_mod):
    """0x1384 is Apple's SECOND fabric — the one ADR-0005 predicted from the
    observed three-fabric count — and it was missing entirely, so it rendered as
    "vendor 0x1384" beside "Apple Home" on the picker that removes ecosystems."""
    assert "Apple" in bridge_mod.VENDOR_NAMES[0x1349]
    assert "Apple" in bridge_mod.VENDOR_NAMES[0x1384]
    assert bridge_mod.VENDOR_NAMES[0x1349] != bridge_mod.VENDOR_NAMES[0x1384], \
        "both are Apple; a user picking one to unpair must be able to tell them apart"


def test_the_two_wrong_entries_are_gone(bridge_mod):
    """⊗ The regression itself, pinned by value.

    0x1075 was labelled "SmartThings" and is not an issued vendor id at all
    (CSA DCL: not found); Samsung SmartThings is 0x110A. 0x100B was labelled
    "Google"; the DCL says Signify, and Google is 0x6006. On a picker whose
    Execute button removes every exported accessory from the chosen ecosystem, a
    wrong name does not read as wrong — it reads as the right ecosystem.
    """
    assert 0x1075 not in bridge_mod.VENDOR_NAMES
    assert "Google" not in bridge_mod.VENDOR_NAMES.get(0x100B, "")
    assert bridge_mod.VENDOR_NAMES[0x6006] == "Google"
    assert "SmartThings" in bridge_mod.VENDOR_NAMES[0x110A]


def test_an_unknown_vendor_is_rendered_as_hex_never_guessed(bridge_mod):
    """The safe direction: hex is a question the user can look up, a wrong name
    is an answer they will act on."""
    unknown = bridge_protocol.FabricInfo(fabric_index=4, label="", vendor_id=0x1075)
    assert bridge_mod.describe_fabric(unknown) == "vendor 0x1075 (index 4)"
