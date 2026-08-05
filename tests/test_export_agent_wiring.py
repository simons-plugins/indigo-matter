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
            "fabrics": [{"fabricIndex": 2, "label": "", "vendorId": 0x100B}],
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


def test_fabrics_are_described_by_vendor_not_by_their_own_label(bridge_mod):
    """A fabric's `label` is whatever the commissioner wrote — for Apple, a
    UUID-ish string that tells nobody anything. The index is always shown
    because that is what §3.9 removes a fabric BY."""
    fabric = bridge_protocol.FabricInfo(fabric_index=1, label="", vendor_id=0x1349)
    assert bridge_mod.describe_fabric(fabric) == "Apple (index 1)"
    labelled = bridge_protocol.FabricInfo(fabric_index=3, label="Kitchen Hub",
                                          vendor_id=0x100B)
    assert bridge_mod.describe_fabric(labelled) == "Google — Kitchen Hub (index 3)"
    unknown = bridge_protocol.FabricInfo(fabric_index=9, label="", vendor_id=0x1234)
    assert bridge_mod.describe_fabric(unknown) == "vendor 0x1234 (index 9)"
