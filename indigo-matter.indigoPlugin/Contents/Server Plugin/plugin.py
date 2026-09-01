"""indigo-matter — Matter device support for the Indigo home automation server.

Lifecycle glue and the device/action bridge. All I/O lives on the asyncio loop
owned by :class:`AsyncRuntime`; this class wires the async services in
``startup``, runs a non-I/O watchdog in ``runConcurrentThread``, tears
everything down in ``shutdown``, and bridges Indigo device actions onto the
loop. Everything Indigo reaches only through XML-named callbacks — the IWS
HTTP handlers, the export dialog, pairing/fabric management, and the
matter-server/bridge-node menus — has moved to mixins (issue #146), except
the Set Sensitivity Level custom action (``actionSetSensitivityLevel`` and
its ``getSensitivityLevels`` picker), which stays with the action bridge:
:class:`HttpApiMixin`, :class:`ExportDialogMixin`, :class:`PairingMenuMixin`,
:class:`MatterServerMenuMixin`, :class:`ExportRecoveryMenuMixin`,
:class:`BridgeAgentMenuMixin`, :class:`DiagnosticsMenuMixin` — the last three
split from one former ``server_menu_mixin.py`` (issue #146: mixins are
required so callbacks resolve as flat attributes; nothing requires them to
share a file).
``plugin_constants.py`` holds the shared constants and prefs helpers;
``pairing_page.py`` holds the pairing IWS page template. ``Plugin`` composes all
seven mixins so every callback still resolves as a plain attribute on the
``Plugin`` class, which is how Indigo looks them up.

See ``docs/PRD-indigo-matter-plugin.md``, ``docs/IMPLEMENTATION.md`` (protocol +
scaffold) and ``docs/API.md`` (the Domio contract). matter-server protocol field
names are isolated in ``protocol.py``.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

import indigo  # provided by the Indigo runtime

from async_runtime import AsyncRuntime
from bridge_agent_menu_mixin import BridgeAgentMenuMixin
from commission_jobs import CommissionJobs
import ct_bounds
from ct_calibration import CTCalibrationEngine
import device_settings
from device_sync import DeviceSync, NodeDeviceTombstones
from diagnostics_menu_mixin import DiagnosticsMenuMixin, MatterUnavailable
import bridge_health
import export_bridge
from export_bridge import ExportBridge
import export_dialog_mixin      # noqa: F401 (tests patch EXPORT_PICKER_LIMIT)  # pylint: disable=unused-import
from export_dialog_mixin import ExportDialogMixin
from export_recovery_menu_mixin import ExportRecoveryMenuMixin
from export_store import ExportStore
from http_api_mixin import HttpApiMixin
from http_handlers import HttpApi
from launch_agent import STARTUP_GRACE_SECONDS, VERDICT_CONFLICT_FREE, VERDICT_DEAD
from matter_client import MatterClient
from matter_handlers.registry import HandlerRegistry
from matter_handlers.settings import settings_for_type
import matter_server_menu_mixin  # noqa: F401 (tests patch ServerProcess)  # pylint: disable=unused-import
from matter_server_menu_mixin import MatterServerMenuMixin
from pairing_menu_mixin import PairingMenuMixin
import protocol
from protocol import MatterWrite, Protocol
from server_process import ServerProcess

from plugin_constants import (
    COMMAND_TIMEOUT, MAX_RESUBSCRIBE_ATTEMPTS, NODE_TOMBSTONES_PREF, ON_TIME_RETIRED_PREF,
    PLUGIN_NAME, PORT_CONFLICT_CHECK_INTERVAL, REMOVED_STATE_LOG_PREF, RESUBSCRIBE_TICKS,
    SURVEY_LOG_PREF, sanitize_host, server_location,
)
import settings_report

# Re-exported for the test suite, which reaches into this module's namespace
# (tests/test_*.py do `plugin_mod.<name>`) and for backwards compatibility with
# anything importing these from `plugin`. See issue #146.
# pylint: disable=unused-import
import bridge_agent            # noqa: F401  (tests patch bridge_agent.BridgeProcess)
import export_catalog          # noqa: F401
import export_handlers         # noqa: F401
from pairing_page import _escape, _pairing_html   # noqa: F401
from plugin_constants import (  # noqa: F401
    ALL_OPTION_ID, DECOMMISSION_TIMEOUT, EXCLUDED_OPTION_PREFIX, EXPORT_PICKER_LIMIT,
    FACTORY_RESET_TIMEOUT, LIST_ERROR_OPTION, MENU_MANAGE_EXPORTS,
    MENU_UNPAIR_ECOSYSTEM, NO_MATCH_OPTION, NO_SELECTION_ID, NO_SELECTION_LABEL,
    PAIRING_READ_TIMEOUT, ROW_ERROR_LABEL, SURVEY_READ_TIMEOUT,
    TRUNCATED_OPTION, UNPAIR_TIMEOUT, WINDOW_OPEN_TIMEOUT,
)

#: The sensitivity setting's declaration, resolved once from the registry.
#: Shared by BOTH entry points that can change it — the Edit Device dialog and
#: issue #85's "Set Sensitivity Level" action — so the two cannot drift on the
#: cluster, the attribute, or what counts as a successful write.
_SENSITIVITY_SETTING = next(
    s for s in settings_for_type("matterMotionSensor") if s.key == "sensitivityLevel")


class Plugin(HttpApiMixin, ExportDialogMixin, PairingMenuMixin, MatterServerMenuMixin,
             ExportRecoveryMenuMixin, BridgeAgentMenuMixin,
             DiagnosticsMenuMixin, indigo.PluginBase):
    """Matter plugin entry point."""

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs)
        self.debug = bool(plugin_prefs.get("verboseLogging", False))
        self._version = plugin_version
        self._start_ts = time.monotonic()

        self.runtime: AsyncRuntime | None = None
        self.proto = Protocol()
        self.registry = HandlerRegistry()
        self.device_sync = DeviceSync(self.registry, self.logger)
        self.matter: MatterClient | None = None
        self.jobs: CommissionJobs | None = None
        self.http: HttpApi | None = None
        self.server_process: ServerProcess | None = None
        # Periodic port-conflict check (#182): when it may next run, and the last
        # verdict logged so a standing conflict is reported once, not every pass.
        self._next_port_check: float = 0.0
        self._last_port_conflict: str | None = None
        # Same, for the bridge agent (#187) — a SEPARATE pair of state variables
        # because the two agents start/stop on unrelated triggers (serverLocation
        # vs the export allow-list) and must not silence each other's verdicts.
        self._next_bridge_port_check: float = 0.0
        self._last_bridge_port_conflict: str | None = None
        # The EXPORT bridge node's LaunchAgent (E7). Built lazily and only ever
        # by a path that means to run it: a fresh install must be inert (XAC1),
        # and constructing this in startup would be one `ensure_installed` away
        # from a plist for a bridge nobody has asked for.
        self.bridge_process = None
        # The export allow-list (PRD §5.1). Built in startup, before anything
        # can consult it; None means "the plugin has not started yet", which
        # every export callback checks rather than assuming.
        self.exports: ExportStore | None = None
        # The outbound export engine (PRD-indigo-matter-export §5.4). Built in
        # startup; it owns the bridge client and starts one only when something
        # is actually exported (XG5).
        self.export_bridge: ExportBridge | None = None
        # Issue #293's calibration action — built in startup, once
        # export_bridge (and its ct_learner) exists; the action callback
        # checks for None the same way every export callback checks
        # export_bridge itself.
        self._ct_calibration: CTCalibrationEngine | None = None
        #: The allow-listed device ids, cached as a plain frozenset attribute.
        #: ``deviceUpdated`` fires for EVERY device on the server, so its guard
        #: has to be one attribute load and one hash lookup — no lock, no
        #: rebuild, no allocation. Refreshed only by :meth:`_exports_changed`.
        self._exported_ids: frozenset[int] = frozenset()
        #: Whether ``indigo.devices.subscribeToChanges()`` has been issued.
        self._subscribed_to_devices = False
        #: Whether a ``deviceUpdated`` has arrived since the subscription was
        #: issued — the only observable evidence that it actually took.
        self._device_updates_seen = False
        #: How many watchdog ticks have passed with a subscription and no
        #: ``deviceUpdated``, and how many times we have re-issued it.
        self._no_update_ticks = 0
        self._resubscribe_attempts = 0
        #: Set once the watchdog has said out loud that it is giving up, so the
        #: one line naming the consequence is said once and not every 15s.
        self._resubscribe_gave_up = False
        #: Device ids whose export callback is currently failing. This callback
        #: fires on every change of an exported device, so a stuck failure would
        #: otherwise write one traceback per dimmer-ramp step; cleared on the
        #: first success so a second, later outage is still heard.
        self._export_callback_failed: set[int] = set()
        self._install_thread: threading.Thread | None = None
        self._stopping = False
        # When WE restart matter-server (menu / post-install), the client sees a brief
        # outage — suppress the "appears to be crashing" diagnostic until this deadline
        # (cleared early on a successful reconnect). _restart_notice_shown dedups the
        # "restarting…" info line to once per window.
        self._restart_expected_until = 0.0
        self._restart_notice_shown = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _server_prefs(self) -> dict:
        """Current prefs with local-mode pinning applied — build every ServerProcess here.

        Local mode is turnkey: the plugin runs matter-server on loopback and dials it
        there, so a stale or blank host/port can never be used. That pinning lands in a
        COPY (only ``serverLocation``/``manageLaunchAgent`` are persisted), so anything
        constructing a ServerProcess from raw ``pluginPrefs`` skips it and can write a
        different plist than startup does — which, because ``_apply_plist`` reloads on a
        digest change, would flip-flop the server on every reload. One helper keeps every
        call site honest.
        """
        prefs = dict(self.pluginPrefs)
        location = server_location(prefs)
        if location == "local":
            prefs["matterServerHost"] = "localhost"
            prefs["matterServerPort"] = "5580"
            prefs["matterServerPath"] = "/ws"
            prefs["matterServerListenAddress"] = "127.0.0.1"
        prefs["serverLocation"] = location
        return prefs

    def startup(self) -> None:
        self.debug = bool(self.pluginPrefs.get("verboseLogging", False))
        # One user-facing choice — is matter-server on this Mac? — drives both
        # the connection target and whether the plugin runs the server.
        prefs = self._server_prefs()
        location = prefs["serverLocation"]
        managed = location == "local"
        # Persist the resolved choice so the config UI and later reads agree
        # (also migrates pre-2026.6 prefs that had no serverLocation key).
        self.pluginPrefs["serverLocation"] = location
        self.pluginPrefs["manageLaunchAgent"] = managed

        # Load the export allow-list first: it is pure prefs I/O, and E3's
        # bridge wiring will need it already populated when it starts. An
        # unreadable list must not stop the (inbound) plugin starting, so
        # ExportStore degrades to empty and preserves the blob rather than
        # raising — see export_store._corrupt.
        # prefs are read through a getter, not captured: Indigo can rebind
        # self.pluginPrefs when the user saves the PluginConfig dialog, and a
        # store holding the old mapping would write to an orphan. savePluginPrefs
        # is the flush the store commits through before it trusts a write.
        self.exports = ExportStore(
            lambda: self.pluginPrefs, self.logger,
            save_prefs=self._save_plugin_prefs,
            entry_validator=self._reject_unexportable_entry,
        )
        if len(self.exports):
            self.logger.info("Matter export allow-list: %d device(s) exported", len(self.exports))
        self._reconcile_exports()

        # Which devices have already had their settable-attribute report logged
        # (issue #191). Same prefs discipline as the export store: read through a
        # getter and commit via savePluginPrefs, because Indigo rebinds
        # self.pluginPrefs when the PluginConfig dialog is saved and a captured
        # mapping would be written to an orphan.
        self.survey_log = settings_report.SurveyLog(save=self._save_survey_log)
        self.survey_log.load(self.pluginPrefs.get(SURVEY_LOG_PREF, ""))
        self.device_sync.survey_log = self.survey_log

        # Node-device tombstones (issue #204, ADR-0008). Same prefs discipline
        # as survey_log above — read through a getter, committed via
        # savePluginPrefs, because self.pluginPrefs is rebound when the
        # PluginConfig dialog is saved.
        self.node_tombstones = NodeDeviceTombstones(save=self._save_node_tombstones, logger=self.logger)
        self.node_tombstones.load(self.pluginPrefs.get(NODE_TOMBSTONES_PREF, ""))
        self.device_sync.node_tombstones = self.node_tombstones

        # getDeviceStateList's "last reported removed-set" per device (issue
        # #312) — same prefs discipline as survey_log above.
        self.removed_state_log = device_settings.RemovedStateLog(save=self._save_removed_state_log)
        self.removed_state_log.load(self.pluginPrefs.get(REMOVED_STATE_LOG_PREF, ""))

        self._announce_retired_on_time_state()

        self.runtime = AsyncRuntime(self.logger)
        self.runtime.start()

        if managed:
            try:
                self.server_process = ServerProcess(prefs, self.logger)
                self.server_process.ensure_installed()
            except Exception as exc:  # pylint: disable=broad-except
                # Absorbing: anything ServerProcess's constructor or ensure_installed()
                # raises. Safe because the matter-server agent is optional and gated off by
                # default: startup continues to build the MatterClient either way, and the
                # `self.server_process` guard on the next line is written for exactly this
                # post-failure state. NOTE (#310): this does collapse two outcomes — a
                # ctor failure leaves server_process None, while an ensure_installed()
                # failure leaves it SET but possibly not installed. Nothing distinguishes
                # them today; the traceback is the only evidence.
                self.logger.exception(exc)

        # Only in managed/local mode can we read the server's own error log; in remote
        # mode there is no local log to surface.
        on_repeated_failure = self._on_server_unreachable if self.server_process else None
        self.matter = MatterClient(
            self.proto, self.logger, prefs,
            on_event=self._on_matter_event,
            on_connect=self._resync,
            on_disconnect=self._on_disconnected,
            on_repeated_failure=on_repeated_failure,
            on_late_response=self._on_late_matter_response,
            # #340: the client's per-attempt "connection lost" warnings are the
            # bulk of a restart's log noise, and they are emitted where nothing
            # knows the outage was ordered. Hand it the same window the
            # diagnostic below reads, rather than a second copy of the state.
            outage_expected=self._restart_expected,
        )
        self.jobs = CommissionJobs(
            self.matter, self.device_sync.create_from_raw, self.logger,
            schedule=self.runtime.submit,
            knows_node=self.device_sync.knows_node,
        )
        self.http = HttpApi(
            self.jobs, self.logger,
            status_provider=self._status_body,
            decommission_provider=self._decommission_sync,
            diagnostics_provider=self._diagnostics_sync,
        )

        # Export (outbound) — built unconditionally, started only if the
        # allow-list is non-empty. Both decisions live in _exports_changed.
        self.export_bridge = ExportBridge(
            self.exports, self.runtime, self.logger, lambda: self.pluginPrefs,
            plugin_version=self._version, plugin_id=self._export_plugin_id(),
            # The un-export debt (XAC7) is written to prefs and has to reach
            # disk to be worth writing: the failure it covers is the plugin
            # never getting another chance to say it.
            save_prefs=self._save_plugin_prefs,
            # E7's LaunchAgent seams. Passed unconditionally — the bridge is
            # gated by the ALLOW-LIST, not by startup, so handing them over here
            # installs nothing (XAC1). ExportBridge calls them on the
            # empty↔non-empty transitions and nowhere else.
            agent_start=self._start_bridge_agent,
            agent_stop=self._stop_bridge_agent,
            agent_diagnose=self._bridge_agent_diagnosis,
        )
        self._ct_calibration = CTCalibrationEngine(
            self.exports, self._indigo_device, self.export_bridge.ct_learner, self.logger)
        self._exports_changed()

        run_future = self.runtime.submit(self.matter.run())
        # if the run-loop coroutine ever dies, surface it rather than parking the
        # exception on an unretrieved future.
        run_future.add_done_callback(self._log_run_future)
        # reconcile is driven by on_connect (_resync), so it runs on first connect
        # and again on every reconnect — no separate initial-sync task needed.
        self.logger.info("%s started", PLUGIN_NAME)

    def _log_run_future(self, fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            self.logger.exception(exc)

    def _announce_retired_on_time_state(self) -> None:
        """Say once that the ``onTime`` device SETTING is gone (#197).

        Only the setting is retired. The ``onTime`` *state* is not withdrawn —
        workspace ADR-0007 keeps it, flagged, precisely because a live
        experiment (ADR-0006's Confirmation, 2026-08-11) found that withdrawing
        a state leaves any trigger already bound to it silently orphaned:
        still enabled, nothing in the log, its own dialog quietly repointed at
        a different, real state. Kept instead, a trigger built on the
        ``onTime`` state stays bound to what its author actually built — it
        simply never fires, because ``deviceStartComm``/``_prime_retired_on_time_state``
        pin the state at 0 with ``uiValue="<unused>"`` on every start and
        nothing else writes it. So this notice no longer has a broken binding
        to warn about; it only has to say the setting is gone from the dialog
        and where the state stands now.

        Counted per DEVICE THAT ACTUALLY HAD THE STATE, not per relay. Only relays
        implementing OnOff's Lighting feature ever carried it — ``getDeviceStateList``
        withdrew it from the rest (#190), and on the dev fabric that is two plugs in
        four. Telling the other two that a setting of theirs was retired sends
        them looking for something they never had.

        ``dev.states`` is the right evidence and this is the right MOMENT to read
        it: Indigo calls ``startup`` before ``deviceStartComm``, and it is
        ``deviceStartComm`` that forces ``stateListOrDisplayStateIdChanged`` and
        so migrates the device to the new state list. So the states read here still
        describe the pre-upgrade world, which is exactly the question being asked.

        The flag is written even when nothing is logged, so a user with no affected
        devices is not re-scanned on every startup for the rest of time.

        Never raises. A cosmetic notice must not be able to stop startup. A failure
        deliberately leaves the flag UNSET so the notice retries next launch —
        losing it entirely is the worse direction.
        """
        try:
            if self.pluginPrefs.get(ON_TIME_RETIRED_PREF, False):
                return
            affected = [dev for dev in indigo.devices.iter("self")
                        if dev.deviceTypeId == "matterRelay" and "onTime" in dev.states]
            if affected:
                self.logger.info(
                    "Matter: the 'Auto-off after' device setting has been removed from "
                    "the Edit Device dialog on %d relay device(s) — Matter's OnTime is a "
                    "parameter of a command this plugin does not send, not a stored "
                    "setting, so it never did anything. The device's 'onTime' state — "
                    "shown as 'OnTime (retired)' in the trigger and control page editors "
                    "— stays on the device, so any trigger already built on it stays "
                    "bound, but it will never update again — the device's properties now "
                    "show it as '<unused>'. Use Indigo's own 'Auto-off after X minutes' "
                    "on the device's turn-on action instead. See issue #197.", len(affected))
            self.pluginPrefs[ON_TIME_RETIRED_PREF] = True
            # Through the shared seam rather than a bare savePluginPrefs(): that is
            # the commit, and without it the flag survives only until the plugin
            # stops — so every reload would re-announce (diagnostics_menu_mixin.py
            # records the same trap for the survey log).
            self._save_plugin_prefs()
        except Exception as exc:  # pylint: disable=broad-except
            # a notice must never break startup
            self.logger.exception(exc)

    # ------------------------------------------------------------------
    # Export wiring (PRD-indigo-matter-export §5.4)
    # ------------------------------------------------------------------
    def _exports_changed(self) -> None:
        """THE seam for "the allow-list changed" — call it after every write.

        Refreshes the hot-path id set, subscribes to device changes if this is
        the first export, and lets the bridge start or stop itself (XG5).
        """
        self._exported_ids = self.exports.ids() if self.exports is not None else frozenset()
        if self._exported_ids:
            self._subscribe_to_device_changes()
        if self.export_bridge is not None:
            self.export_bridge.exports_changed()

    def _subscribe_to_device_changes(self) -> None:
        """Ask the server for every device change — once, and only if we need it.

        Three findings settle the shape of this, all from the Indigo docs rather
        than from what was convenient:

        * ``indigo.devices.subscribeToChanges()`` subscribes to **every device
          on the server**, not ours, and the IOM reference is explicit that it
          "causes a significant amount of traffic between IndigoServer and your
          plugin". The default posture of this plugin is an empty allow-list
          (XG5), so subscribing unconditionally would tax every existing user
          who never exports anything — for callbacks that would return on their
          first line every single time.
        * It is a plain request to the server, not a startup-only registration.
          Issuing it from a menu callback the first time a user exports a device
          works exactly as it does from ``startup``, which is what makes the
          conditional subscription safe: the first export in a session turns it
          on, and every later ``deviceUpdated`` arrives.
        * There is **no unsubscribe** in the canonical scripting reference (only
          ``subscribeToChanges``), so this is a one-way door. We therefore never
          try to turn it off when the allow-list empties again; the hot-path
          guard below already makes a stale subscription free, and a
          "clever" unsubscribe against an undocumented API is exactly the sort
          of thing that fails silently on an Indigo upgrade.

        Note there is deliberately **no ``pluginId`` self-loop guard** here (the
        usual companion to this subscription). It would be dead code: our own
        devices are excluded by the catalog's loop guard, so they can never
        reach the allow-list, and the id-set check below already refuses them.
        The dispatch→state→push path does not loop either — the node
        echo-guards its own writes (§6.4) and a push produces no Indigo change.
        """
        if self._subscribed_to_devices:
            return
        self._issue_device_subscription()

    def _issue_device_subscription(self) -> bool:
        """The bare call, without the once-only guard. True if it did not raise."""
        try:
            indigo.devices.subscribeToChanges()
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any failure of indigo.devices.subscribeToChanges(), an
            # undocumented, unacknowledged, one-way call. Safe because the consequence
            # is named at ERROR and _resubscribe_tick's bounded watchdog is the retry.
            # Pinned by test_a_failed_subscription_is_loud_and_not_fatal.
            self.logger.error("Matter bridge: could not subscribe to Indigo device changes — "
                              "exported accessories will not follow Indigo state. %s", exc)
            self.logger.exception(exc)
            return False
        self._subscribed_to_devices = True
        self.logger.debug("subscribed to Indigo device changes (export is active)")
        return True

    def _resubscribe_tick(self) -> None:
        """Re-issue ``subscribeToChanges`` if it looks like it never took.

        **Why this exists.** The whole outbound push path rests on one
        undocumented assumption: that ``subscribeToChanges`` works the same
        issued from a menu callback as from ``startup``. The canonical reference
        describes it as a request to the server, which is why it is safe to make
        conditional (see :meth:`_subscribe_to_device_changes`) — but it does not
        *say* so, there is no acknowledgement to check, and no unsubscribe to
        compare against. If the assumption is ever wrong on some Indigo build,
        the symptom is silence: exported accessories simply stop following
        Indigo, with no error anywhere.

        **Giving up is itself news.** Exhausting the attempts used to be a bare
        ``return``, at no log level at all — so the one feature whose entire
        purpose is to break a silence ended by going silent, in exactly the
        house where it had failed to help. It now says so once, naming the
        consequence and the remedy rather than the counter.

        **``_device_updates_seen`` deliberately never re-arms**, and that is a
        known limit rather than an oversight: a subscription that dies *later*
        in a session is not noticed here, because the flag only records that an
        update was seen at some point. Re-arming it on a window would make every
        genuinely quiet house — nobody home, nothing switching for a few minutes
        — re-issue the subscription and eventually warn that export is broken
        when it is working perfectly. The failure this watchdog exists for is a
        subscription that never registered *at all*, which is the one it can
        tell apart from a quiet house, and it stays scoped to that.
        """
        if not self._exported_ids or not self._subscribed_to_devices:
            return
        if self._device_updates_seen:
            return
        if self._resubscribe_attempts >= MAX_RESUBSCRIBE_ATTEMPTS:
            if not self._resubscribe_gave_up:
                self._resubscribe_gave_up = True
                self.logger.warning(
                    "Matter bridge: no Indigo device update has arrived since exporting, after "
                    "re-issuing subscribeToChanges %d times. Unless the house is simply idle, "
                    "exported accessories are NOT following Indigo state and nothing further will "
                    "retry on its own — reload the plugin.", MAX_RESUBSCRIBE_ATTEMPTS)
            return
        self._no_update_ticks += 1
        if self._no_update_ticks < RESUBSCRIBE_TICKS:
            return
        self._no_update_ticks = 0
        self._resubscribe_attempts += 1
        # **Bounded, because "no updates" is also what a quiet house looks
        # like.** A device that nobody touches genuinely produces no callback,
        # so this cannot be a permanent retry loop without being permanent
        # noise. A handful of re-issues covers the case it is for — a
        # subscription that never registered — and after that the evidence is
        # indistinguishable from nothing having happened.
        self.logger.debug(
            "Matter bridge: no device updates since subscribing; re-issuing "
            "subscribeToChanges (attempt %d of %d)",
            self._resubscribe_attempts, MAX_RESUBSCRIBE_ATTEMPTS)
        self._issue_device_subscription()

    def deviceUpdated(self, origDev, newDev):  # noqa: N802
        """Push an exported device's change outward.

        This runs for **every device on the server** (see
        :meth:`_subscribe_to_device_changes`), so the second statement is the
        whole performance story: a frozenset membership test on an int, against
        an attribute the plugin already holds. Nothing is classified, nothing is
        locked and nothing is allocated for a device nobody exported.
        """
        super().deviceUpdated(origDev, newDev)
        # Set for ANY device, before the allow-list gate: the flag is evidence
        # that the *subscription* is alive, and the subscription covers every
        # device on the server. Gating it on an exported device would leave the
        # watchdog re-issuing a perfectly good subscription in any house where
        # the exported devices happen to be idle.
        self._device_updates_seen = True
        if newDev.id not in self._exported_ids:
            return
        if self.export_bridge is None:
            return
        try:
            self.export_bridge.device_updated(origDev, newDev)
        except Exception as exc:  # pylint: disable=broad-except
            # never let export break Indigo's callback
            # Named and rate-limited: a bare traceback here says a device broke
            # but not which one, and this callback fires often enough that a
            # stuck device would bury the rest of the event log.
            if newDev.id not in self._export_callback_failed:
                self._export_callback_failed.add(newDev.id)
                self.logger.error(
                    "Matter bridge: the update of %s (id %s) could not be handed to the bridge "
                    "— %s. Its accessory will show stale state until this clears.",
                    getattr(newDev, "name", ""), newDev.id, exc)
                self.logger.exception(exc)
        else:
            self._export_callback_failed.discard(newDev.id)

    def deviceDeleted(self, dev):  # noqa: N802
        """A deleted device leaves the allow-list and the bridge (PRD §5.4),
        and — since issue #204/ADR-0008 — a deleted matterNode device is
        tombstoned so the next reconcile does not silently recreate it."""
        super().deviceDeleted(dev)
        if dev.deviceTypeId == "matterNode":
            # Widened to cover pluginProps.get too (issue #204 review, fix F):
            # the noqa below claims "a deletion must always complete", which
            # only held for note_node_device_deleted itself — a broken
            # pluginProps read used to escape this try entirely.
            try:
                node_id = dev.pluginProps.get("nodeId")
                if node_id not in (None, ""):
                    self.device_sync.note_node_device_deleted(int(node_id), dev.id)
            except Exception as exc:  # pylint: disable=broad-except
                # a deletion must always complete
                self.logger.exception(exc)
        # Every device type can carry a removed_state_log entry (issue #312),
        # not just matterNode — unlike the tombstone above this is unconditional
        # and unrelated to exports, so it runs before the early return below.
        try:
            log = getattr(self, "removed_state_log", None)
            if log is not None:
                log.forget(dev.id)
        except Exception as exc:  # pylint: disable=broad-except
            # a deletion must always complete
            self.logger.exception(exc)
        if dev.id not in self._exported_ids or self.exports is None:
            return
        try:
            self.exports.remove(dev.id)
            self.logger.info("Removed Matter export for %s (id %s) — the Indigo device was deleted",
                             getattr(dev, "name", ""), dev.id)
        except Exception as exc:  # pylint: disable=broad-except
            # The store rolled back, so the entry survives; the endpoint removal
            # below is still right (the device is gone either way) and the
            # startup sweep will report the orphan.
            self.logger.error("Matter bridge: removing the deleted device %s from the export "
                              "list FAILED — %s", dev.id, exc)
            self.logger.exception(exc)
        self._export_callback_failed.discard(dev.id)
        try:
            if self.export_bridge is not None:
                # Issue #274: the device is confirmed gone — destroy the
                # accessory outright.
                self.export_bridge.remove(dev.id)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: anything export_bridge.remove() raises — the socket is the
            # bridge's, not ours, so this is an expected raise path (see the finally
            # below, which exists because of it). Safe because the deletion has already
            # happened in Indigo and the startup sweep reports any surviving export.
            # Pinned by test_a_raising_endpoint_removal_still_refreshes_the_cache.
            self.logger.exception(exc)
        finally:
            # In a finally because the endpoint removal above can raise (the
            # socket is the bridge's, not ours) and the id-set cache has no
            # other way back in sync: leaving a deleted device in it makes
            # deviceUpdated hand a ghost to the bridge on every later change,
            # and deviceDeleted will never fire for it again.
            try:
                self._exports_changed()
            except Exception as exc:  # pylint: disable=broad-except
                # Absorbing: anything _exports_changed() raises. This one is inside the
                # finally, which is why it must not be removed: an exception raised from a
                # finally REPLACES whatever was in flight and would escape into Indigo's
                # deviceDeleted callback, turning a bookkeeping failure into a broken
                # delete. No test drives this arm (#310 audit).
                self.logger.exception(exc)

    def shutdown(self) -> None:
        self.logger.debug("%s shutting down", PLUGIN_NAME)
        # Signal any in-flight background install to skip its post-npm plugin-state
        # mutation, then wait briefly for it so we don't rewrite the LaunchAgent or
        # prefs against a tearing-down plugin.
        self._stopping = True
        if self._install_thread is not None and self._install_thread.is_alive():
            self._install_thread.join(timeout=5)
        if self.runtime is not None and self.runtime.is_running and self.matter is not None:
            try:
                self.runtime.submit(self.matter.close()).result(timeout=4)
            except Exception as exc:  # pylint: disable=broad-except
                # Absorbing: anything closing the matter-server connection raises during
                # shutdown, on a future already capped at 4s. Safe, and deliberately quiet,
                # because the connection is being abandoned anyway and shutdown must reach
                # the bridge-stop below; a failure to close something we are discarding is
                # teardown noise, not news. NOTE (#310): this level was never argued for in
                # a commit or pinned by a test — it is day-one code.
                self.logger.debug("matter close error: %s", exc)
        # Same ordering rule as the controller client: close the socket while the
        # loop still exists to close it on. The bridge *agent* is deliberately
        # left running (PRD §5.4 / PM-B) — a plugin reload must not un-pair
        # anyone's ecosystems.
        if self.runtime is not None and self.runtime.is_running and self.export_bridge is not None:
            self.export_bridge.stop()
        self.export_bridge = None
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

    async def _assert_fabric_label(self) -> None:
        """Name the Indigo fabric on devices (matter-server's default label is
        'HomeAssistant' — vendor apps would list us as Home Assistant).

        Sent only when the server's current label differs: set_default_fabric_label
        pushes UpdateFabricLabel to every connected node, so re-asserting an
        unchanged label on each reconnect would be pointless per-device writes.
        Cosmetic — failure (e.g. an older matter-server without the command) must
        never block reconcile.
        """
        desired = str(self.pluginPrefs.get("fabricLabel", "Indigo")).strip()[:32] or "Indigo"
        try:
            current = (self.matter.server_info or {}).get("fabric_label")
            if current == desired:
                return
            await self.matter.set_default_fabric_label(desired)
            self.logger.info('fabric label set to "%s" (was %s)', desired, current or "server default")
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: a matter-server too old to know set_default_fabric_label.
            # Safe because the label is cosmetic and this runs BEFORE _resync's own
            # try — raising here would mean no reconcile at all. Level chosen
            # deliberately in d91573e ("Cosmetic: failure logs at debug, never blocks
            # reconcile") and pinned by test_fabric_label_failure_does_not_block_reconcile.
            self.logger.debug("could not set fabric label (older matter-server?): %s", exc)

    async def _resync(self) -> None:
        """Reconcile matter nodes ↔ Indigo devices. Driven by the WS client's
        on_connect, so it runs on the first connect and again on every reconnect
        (recovers state + clears 'unreachable' after a drop / sleep-wake)."""
        # A successful (re)connect means any expected-restart window has served its
        # purpose — close it so a LATER crash isn't wrongly suppressed as "restarting".
        self._restart_expected_until = 0.0
        await self._assert_fabric_label()
        try:
            nodes = await self.matter.get_nodes() or []
            detailed = []
            for node in nodes:
                if isinstance(node, dict) and "endpoints" in node:
                    detailed.append(node)
                else:
                    node_id = node.get("node_id") if isinstance(node, dict) else node
                    detailed.append(await self.matter.get_node(node_id))
            self.device_sync.reconcile_all(detailed)
            self.logger.info("reconciled %d Matter node(s)", len(detailed))
        except Exception as exc:  # pylint: disable=broad-except
            # _resync is the sole reconcile path (first connect + every reconnect);
            # keep the traceback so a first-connect failure (user sees no devices)
            # is debuggable. On failure nothing is cleared (reconcile_all never ran).
            self.logger.warning("resync incomplete: %s", exc, exc_info=True)

    def _on_disconnected(self) -> None:
        """matter-server connection dropped — devices are unreachable until reconnect."""
        self.device_sync.mark_all_unreachable()

    def _on_server_unreachable(self, attempts: int) -> None:
        """Managed mode: matter-server still unreachable after several attempts.

        The WS client only sees "connection refused"; the real reason is in the
        server's own stderr. Surface a tail of it (or a not-installed hint) into the
        Indigo log so users don't have to hunt for ~/Library/Logs/indigo-matter.
        Fired once per failure streak by the client, so this never spams.
        """
        sp = self.server_process
        if sp is None:
            return
        if self._restart_expected():
            # We deliberately restarted it — this outage is expected, not a crash. But
            # DEFER, don't drop: re-arm the client so if the server never comes back the
            # real error still surfaces on a later cycle (past the window). _resync
            # clears the window on a successful reconnect, so this only persists on an
            # actually-failing restart.
            if not self._restart_notice_shown:
                self.logger.info("matter-server is restarting; reconnecting…")
                self._restart_notice_shown = True
            if self.matter is not None:
                self.matter.rearm_failure_diagnostic()
            return
        tail = sp.tail_error_log()
        if tail:
            hint = ""
            if "Storage is locked" in tail or "storage-lock" in tail:
                # A second matter-server (orphaned from an earlier LaunchAgent) holds the
                # storage lock. The plugin now reaps such strays on start/restart; point
                # the user at that in case a reap couldn't run (e.g. ps unavailable).
                hint = ("\nAnother matter-server appears to be holding the storage lock. "
                        "Use Plugins ▸ Matter ▸ Restart the Matter controller (it stops "
                        "stray servers), or reboot the Mac if it persists.")
            self.logger.error(
                "matter-server is not responding after %d attempts and appears to be "
                "crashing. Recent matter-server errors:\n%s%s", attempts, tail, hint,
            )
        else:
            self.logger.error(
                "matter-server is not responding after %d attempts and its error log is "
                "empty — it may not be installed (checked %s). Use Plugins ▸ Matter ▸ "
                "Install/update the Matter controller (matter-server), then restart the "
                "plugin.",
                attempts, sp.project_dir,
            )

    def _expect_restart(self) -> None:
        """Open the ~30s window during which a client outage is treated as an expected
        restart (see :meth:`_on_server_unreachable`), not a crash.

        MONOTONIC (#340 review), like the bridge twin and like the WS client's
        own backoff clock: the deadline is never compared against anything
        persisted, and on a wall clock a backwards NTP step — macOS *steps* a
        large offset rather than slewing it, which is what a Mac waking with RTC
        drift does — would extend the window by the size of the step. A 5-minute
        correction would turn 30s of quieting into 5.5 minutes of it.
        """
        self._restart_expected_until = time.monotonic() + 30
        self._restart_notice_shown = False

    def _restart_expected(self) -> bool:
        """Whether we are inside the window :meth:`_expect_restart` opened.

        The single reader of ``_restart_expected_until``, because it now has two
        consumers — this class's own crash diagnostic and (issue #340) the WS
        client's per-attempt reconnect logging, which is handed this method as
        its ``outage_expected`` seam. Every path that disarms the window still
        does so by zeroing the deadline, so both consumers go loud together.
        """
        return time.monotonic() < self._restart_expected_until

    def runConcurrentThread(self) -> None:
        """Watchdog only — no I/O. Surfaces connectivity; reconnect is owned by
        the WS client's own backoff loop."""
        try:
            while True:
                self._health_tick()
                self.sleep(15)
        except self.StopThread:
            pass

    def _health_tick(self) -> None:
        if self.runtime is not None and not self.runtime.is_running:
            self.logger.warning("async runtime is not running")
            return
        if self.matter is not None and not self.matter.connected:
            ticks = getattr(self, "_disconnect_ticks", 0) + 1
            self._disconnect_ticks = ticks
            # debug each ~15s tick, but warn once it's been down ~1 min so a
            # never-connecting matter-server is visible in the log, not buried.
            if ticks == 4:
                self.logger.warning("matter-server still not connected after ~1 min")
            else:
                self.logger.debug("matter-server not currently connected")
        else:
            self._disconnect_ticks = 0
        # The export side keeps its OWN counter (E1 audit note): the two clients
        # talk to different processes and fail independently, so a shared streak
        # counter would let a healthy bridge silence a dead matter-server, or
        # the reverse.
        if self.export_bridge is not None:
            self.export_bridge.health_tick()
        self._resubscribe_tick()
        self._port_conflict_tick()

    def _port_conflict_tick(self) -> None:
        """Periodically re-check that our launchd-managed processes still own their
        ports (#182), and — for whichever of them just (re)bootstrapped — once their
        bind window has passed rather than waiting for the next slow-interval pass
        (#187, ``LaunchAgent.due_for_bootstrap_verification``).

        ``_apply`` already checks this, but only when something calls
        ``ensure_installed()`` — plugin startup or a menu action. The jarvis incident
        developed BETWEEN reloads and ran four days: launchd respawns matter-server on
        its own, and a rival can take the port at any moment, neither of which goes
        through that path. Worse, the plugin's other health signal cannot see it
        either, because the WS client is happily *connected* — to the wrong server.
        Without this tick the diagnosis only ever fires when someone was already about
        to intervene.

        Checks BOTH agents this plugin can manage: the controller (``server_process``)
        and the export bridge node (``bridge_process`` — before #187 only the
        controller was polled, so a bridge that lost its port race went undetected for
        as long as nothing else happened to reload it).
        """
        self._check_agent_port_conflict(
            getattr(self, "server_process", None), "_next_port_check", "_last_port_conflict")
        self._check_agent_port_conflict(
            getattr(self, "bridge_process", None),
            "_next_bridge_port_check", "_last_bridge_port_conflict")

    def _check_agent_port_conflict(self, agent, next_attr: str, last_attr: str) -> None:
        """One LaunchAgent's contribution to :meth:`_port_conflict_tick` (#182/#187).

        Runs on its own slow cadence (it shells out to lsof/ps) and logs only when the
        verdict CHANGES, so a standing conflict is one line rather than 240 an hour —
        except a pending post-bootstrap verification, which runs regardless of that
        cadence gate, because #187 exists precisely to catch a lost bind race sooner
        than the next scheduled pass.

        **Review finding: "clear on any non-raising call" was wrong.** A due
        verification's ``port_conflict_report()`` answers ``None`` for THREE
        different situations — healthy, the process is too young to judge, and the
        managed job has NO pid at all — and the old code cleared the flag on all
        three alike. That last one IS the #187 fault: bootstrap succeeded, the
        process then lost the port bind race and exited, and nothing was ever
        logged. A due verification is now resolved via
        :meth:`~launch_agent.LaunchAgent.post_bootstrap_verdict`, which tells the
        three apart, and cleared (compare-and-clear, via the exact deadline token
        this call observed) only once a real verdict — conflict, dead, or
        healthy — has been rendered; "too young to tell" leaves it armed for the
        next tick to retry. A verification that keeps failing to reach a verdict
        (a raising report, or a process that never resolves) is bounded: past
        ``2 * STARTUP_GRACE_SECONDS`` since it was armed it is disarmed with a
        single warning instead — at most ~16 probes on the 15s watchdog cadence.
        """
        # getattr for the same reason _disconnect_ticks above uses it: this runs on the
        # watchdog thread, and a diagnostic must never be the thing that kills the
        # health loop if it is reached before/without full construction.
        if agent is None:                    # not managed this session: not ours to police
            return
        now = time.monotonic()
        due_token = agent.due_for_bootstrap_verification()
        if due_token is not None and now > due_token + 2 * STARTUP_GRACE_SECONDS:
            agent.clear_bootstrap_verification(due_token)
            self.logger.warning(
                "post-bootstrap port verification for %s could not conclude within %.0fs of "
                "its bootstrap, so it has been abandoned rather than retried forever. This does "
                "NOT mean %s is unhealthy — only that the check itself never reached a verdict "
                "(e.g. every port-conflict probe raised). The regular %.0fs interval check "
                "below still polices this agent.",
                agent.spec.package, 2 * STARTUP_GRACE_SECONDS, agent.spec.package,
                PORT_CONFLICT_CHECK_INTERVAL,
            )
            setattr(self, next_attr, now + PORT_CONFLICT_CHECK_INTERVAL)
            return
        if due_token is None and now < getattr(self, next_attr, 0.0):
            return
        setattr(self, next_attr, now + PORT_CONFLICT_CHECK_INTERVAL)
        try:
            conflict = agent.port_conflict_report()
        except Exception as exc:  # pylint: disable=broad-except
            # a diagnostic must never kill the watchdog
            self.logger.debug("port conflict check failed: %s", exc)
            return  # leave any pending #187 verification armed; the next tick retries
        if due_token is not None:
            if conflict:
                agent.clear_bootstrap_verification(due_token)  # a verdict; the error path below logs it
            else:
                verdict = agent.post_bootstrap_verdict()
                if verdict == VERDICT_DEAD:
                    self.logger.error(
                        "%s did not survive its own bootstrap: bootstrap succeeded, but the "
                        "managed job now has no pid at all — most likely it lost the port %s "
                        "bind race and exited on EADDRINUSE. See %s.",
                        agent.spec.package, agent.spec.port,
                        os.path.join(agent.log_dir, agent.spec.err_log),
                    )
                    agent.clear_bootstrap_verification(due_token)
                elif verdict == VERDICT_CONFLICT_FREE:
                    agent.clear_bootstrap_verification(due_token)
                # else VERDICT_PENDING: too young (or unknowable) to render a verdict yet —
                # leave the flag armed so the next tick retries.
        previous = getattr(self, last_attr, None)
        if conflict == previous:
            return
        if conflict:
            self.logger.error(conflict)
        elif previous and agent.managed_job_has_pid():
            # Gated on a pid (#187 review): port_conflict_report() also goes quiet when the
            # managed job has NO pid at all — the dead-process branch above already reported
            # that, and it is the opposite of resolved.
            self.logger.info("%s now owns port %s again; the port conflict reported "
                             "earlier is resolved.", agent.spec.package, agent.spec.port)
        setattr(self, last_attr, conflict)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def validatePrefsConfigUi(self, valuesDict):  # noqa: N802
        """Sanitise the remote host and require it when connecting off-Mac."""
        location = str(valuesDict.get("serverLocation", "local")).strip().lower()
        if location == "remote":
            host = sanitize_host(valuesDict.get("matterServerHost", ""))
            valuesDict["matterServerHost"] = host  # strip pasted scheme/port/path
            if not host:
                errors = indigo.Dict()
                errors["matterServerHost"] = (
                    "Enter the hostname or IP of the computer running matter-server."
                )
                return (False, valuesDict, errors)
        return (True, valuesDict)

    def getPrefsConfigUiValues(self):  # noqa: N802
        """Seed the plugin config dialog, computing the PRD §5.5 export readout.

        Indigo has no dynamic labels, so the readout is a **read-only textfield**
        written here — the same shape the export dialog's status line already
        uses. Every value comes from state the plugin already holds (the last
        attach or 15-second status poll, and the §5 fabric events); nothing here
        does I/O, because this runs while the dialog is opening.
        """
        values = indigo.Dict(self.pluginPrefs)
        values["exportReadout"] = self._export_readout()
        return (values, indigo.Dict())

    # Indigo 2025.2's PluginBase carries snake_case aliases for the ConfigUI
    # pre-population callbacks alongside the camelCase names, and which one it
    # dispatches on is not documented. Both are defined so the readout cannot be
    # silently empty on a build that prefers the other spelling.
    def get_prefs_config_ui_values(self):
        return self.getPrefsConfigUiValues()

    def _export_readout(self) -> str:
        """One line describing the Matter bridge for the config dialog (PRD §5.5).

        What the PRD asks for is *which* ecosystems hold a fabric and whether a
        window is open — not slot arithmetic. matter.js allows 254 fabrics, so
        the count is never the interesting number; the identity of the peers is,
        because "why has Alexa stopped working" is answered by seeing that Alexa
        is not in this list.
        """
        bridge = self.export_bridge
        if bridge is None:
            return "Plugin still starting."
        exported = len(self.exports) if self.exports is not None else 0
        if not bridge.enabled:
            return f"Export is switched off. {exported} device(s) would be exported."
        if not bridge.active:
            return (f"{exported} device(s) exported; the bridge node is not running."
                    if exported else "Nothing is exported yet, so the bridge node is not running.")
        # ⊗ `active` means "a client OBJECT exists" — it is set the moment the
        # engine builds one and stays set through every reconnect attempt. Read as
        # "the bridge node is running", it told a user whose node had been
        # crash-looping for a day "3 device(s) exported. Paired with: Apple",
        # which is the readout PRD §5.5 exists to make impossible. The client's
        # own socket state is the fact; `attached` narrows it further, because a
        # connected-but-refusing node (§1.1) serves no accessories at all.
        client = bridge.client
        if client is None or not getattr(client, "connected", False):
            return (f"{exported} device(s) exported, but the plugin is NOT connected to the bridge "
                    f"node — exported accessories are unavailable right now. See the Event Log.")
        fabrics = bridge.fabrics
        if fabrics is None:
            return f"{exported} device(s) exported; not yet connected to the bridge node."
        paired = ", ".join(bridge_health.describe_fabric(f) for f in fabrics) or "nothing yet"
        # Said as "last reported" because it is: this list is the one the last
        # attach or §5 event left behind, deliberately not a WS round trip (this
        # runs while the dialog is opening). An ecosystem that dropped us a
        # second ago is still in it.
        window = self._window_readout(bridge)
        serving = ("" if getattr(client, "attached", False) else
                   " The node is connected but not serving accessories — see the Event Log.")
        return (f"{exported} device(s) exported. Paired with (last reported): {paired}."
                f"{serving}{window}")

    @staticmethod
    def _window_readout(bridge) -> str:
        """The pairing-window sentence, or "" — and never a window that has passed.

        ⊗ ``window_expires_at`` is set by the pairing menu and cleared only by
        the §5 ``window_closed`` event, which the node does not send on shutdown.
        So a node that was restarted (or a plugin that was) left the timestamp
        standing and the readout claimed an open window indefinitely — including
        for a time hours in the past. Comparing it against now costs one parse
        and turns a permanent false claim into an expiry the user can read.
        """
        raw = getattr(bridge, "window_expires_at", None)
        if not raw:
            return ""
        try:
            # The node sends RFC 3339 with a `Z`; `fromisoformat` learned `Z` in
            # 3.11 but the substitution costs nothing and works on 3.10 too.
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            # Unparseable: report it as-is rather than dropping it. A timestamp
            # we cannot compare is still the only thing we know.
            return f" A pairing window was opened, expiring {raw}."
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return (f" The last pairing window expired at {raw} — open a new one with "
                    "Plugins ▸ Matter ▸ Pair Matter Bridge….")
        return f" A pairing window is open until {raw}."

    def closedPrefsConfigUi(self, valuesDict, userCancelled):  # noqa: N802
        if userCancelled:
            return
        self.debug = bool(valuesDict.get("verboseLogging", False))
        # The connection + managed server are wired once in startup from a prefs
        # snapshot, so a changed location/host only takes effect on reload.
        self.logger.info(
            "matter-server settings saved — reload the plugin (or Plugins ▸ Matter ▸ "
            "Restart the Matter controller) to apply them"
        )
        # Export is the exception: its switch and its ports are read on every
        # connect, and the ONE change a user expects to act immediately is
        # ticking or unticking "Enable Matter export". Re-running the transition
        # applies it without a reload — and, because the transition is the same
        # code the allow-list uses, it also brings the agent up or down.
        #
        # This call is the ONLY thing that makes that switch act without a plugin
        # reload, which is what PluginConfig.xml promises the user in prose. It
        # is three lines with no local symptom if they go, so it has its own test
        # (⊗ `test_saving_config_applies_the_export_switch_immediately`).
        if self.export_bridge is None:
            self.logger.debug(
                "Matter bridge: the export engine is not running, so the export switch will take "
                "effect when the plugin next starts.")
            return
        try:
            self.export_bridge.exports_changed()
        except Exception as exc:  # pylint: disable=broad-except
            # A bare traceback here reads as a crash in "save settings". Say what
            # did not happen and what to do instead — the prefs ARE saved.
            self.logger.error(
                "Matter bridge: your settings were saved, but applying the export switch "
                "immediately FAILED (%s). Reload the plugin to apply it.", exc)
            self.logger.exception(exc)

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):  # noqa: N802
        """Reject changing a Matter device's Indigo type (issue #58).

        Indigo's Edit Device dialog always offers the plugin's full Type menu
        and a plugin cannot remove it — but Matter device types are derived
        from the node's clusters at creation. A manual change desyncs the
        device: the next reconcile creates a duplicate of the correct type,
        and the re-typed device becomes a zombie whose actions target clusters
        the node does not implement. ``createdTypeId`` is stamped into props at
        creation (and healed onto older devices at reconcile); absence of the
        stamp (e.g. a manually created device) allows the save.

        Also strips ``matterNode``'s ``lastSurveySummary`` field from
        ``valuesDict`` (issue #204 review) — Indigo persists EVERY ConfigUI
        field into pluginProps on Save, readonly included, so left alone the
        display-only survey summary would become a stored prop that churns on
        every Save (Devices.xml's claim that it is "never a stored prop" was
        aspirational, not true, until this strip). Two costs, not one: the
        persisted copy goes stale the moment the real answer changes
        elsewhere, AND — the one that matters for fix E above — a prop churn
        is a comm restart, so a Save on this dialog alone would re-fire
        ``deviceStartComm`` for a reason with nothing to do with the device's
        actual settings. ``settings_report.SurveyLog`` already made this exact
        call for its own store (kept in pluginPrefs rather than per-device
        props, see its docstring); this is the same call applied to this
        field's per-device echo of that store.
        """
        if typeId == "matterNode":
            valuesDict.pop("lastSurveySummary", None)
        created = ""
        try:
            created = indigo.devices[devId].pluginProps.get("createdTypeId", "")
        except KeyError:
            pass  # brand-new device — nothing to protect yet
        if created and typeId != created:
            errors = indigo.Dict()
            errors["showAlertText"] = (
                "Matter device types are derived from the device itself and "
                f"cannot be changed (this device was created as '{created}'). "
                "If the device is wrong, delete it and reload the plugin — it "
                "will be recreated from the node's clusters."
            )
            return (False, valuesDict, errors)
        # Writable device settings (issue #186). Bounds only — cheap, and this
        # runs on the Indigo UI thread. The write itself is deliberately deferred
        # to closedDeviceConfigUi, because a write plus its read-back against a
        # sleepy Thread device is seconds and would freeze the dialog.
        if not settings_for_type(typeId):
            return (True, valuesDict)  # most types declare none — nothing to look up
        setting_errors = device_settings.validate_settings(
            typeId, valuesDict, self._setting_limits_lookup(valuesDict),
            self._setting_attribute_list_lookup(valuesDict),
            self._has_matter_node(valuesDict))
        if setting_errors:
            errors = indigo.Dict()
            for field, message in setting_errors.items():
                errors[field] = message
            return (False, valuesDict, errors)
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # Writable device settings — the Edit Device dialog (issue #186)
    # ------------------------------------------------------------------

    @staticmethod
    def _has_matter_node(props) -> bool:
        """Is there a Matter node behind this Indigo device yet?

        Explicit because it used to be emergent: with every setting taking its
        bounds from a device attribute, "no node" made the bounds unresolvable
        and nothing was offered. Spec-fixed bounds resolve with no device at
        all, so a never-reconciled relay was shown a full settings section it
        could not save and which threw on the way out.
        """
        node_id = str(props.get("nodeId") or "").strip()
        endpoint_id = props.get("endpointId")
        if not node_id or endpoint_id in (None, ""):
            return False
        try:
            int(node_id)
            int(endpoint_id)
        except (TypeError, ValueError):
            return False
        return True

    def _survey_summary_for(self, props) -> str:
        """The matterNode Edit Device dialog's read-only survey field (issue
        #204): what settings_report's on-demand/automatic survey last found
        for THIS node, or "Not yet surveyed." The SurveyLog itself stays in
        pluginPrefs (settings_report.SurveyLog's docstring); this only reads
        its answer back.
        """
        node_id = str(props.get("nodeId") or "").strip()
        if not node_id or self.device_sync is None or self.device_sync.survey_log is None:
            return settings_report.describe_fingerprint(None)
        try:
            fingerprint = self.device_sync.survey_log.fingerprint_for(int(node_id))
        except (TypeError, ValueError):
            return settings_report.describe_fingerprint(None)
        return settings_report.describe_fingerprint(fingerprint)

    def _log_setting_skips(self, dev_id):
        """An ``on_skip`` sink that says out loud what was withheld and why.

        Both bugs that shipped on this branch presented as "the feature is
        missing" because a setting was dropped in silence. DEBUG is not enough —
        it is off by default, so the one clue is at a level the affected user
        does not have on.
        """
        def note(setting, reason):
            self.logger.info('device %s: "%s" not offered — %s',
                             dev_id, setting.label, reason)
        return note

    def _setting_limits_lookup(self, props):
        """A ``(cluster, attribute) -> raw limits`` lookup for one device.

        Closes over the device's node/endpoint so the pure settings layer never
        has to know what a node id is. Reads props rather than the Indigo device
        so it works while a dialog is open on a device that has none of this
        saved yet. A device with no node yet yields a lookup answering None to
        everything — but that is NOT what stops settings being offered: settings
        with spec-fixed bounds resolve without any device read, so the gate is
        the explicit :meth:`_has_matter_node` check, not this lookup's answers.
        """
        node_id = props.get("nodeId")
        endpoint_id = props.get("endpointId")
        if not self._has_matter_node(props):
            return lambda cluster, attribute: None

        def lookup(cluster, attribute):
            try:
                return self.device_sync.setting_limits(
                    int(node_id), int(endpoint_id), cluster, attribute)
            except Exception as exc:  # pylint: disable=broad-except
                # a broken lookup hides the field, never breaks the dialog
                self.logger.debug("setting-limits lookup failed for node %s/%s: %s",
                                  node_id, endpoint_id, exc)
                return None

        return lookup

    def _setting_attribute_list_lookup(self, props):
        """A ``cluster -> AttributeList`` lookup for one device.

        The capability half of offering a setting: the device's own statement
        of which attributes it implements. Separate from the limits lookup
        because most settings take their bounds from the spec, so "are the
        limits readable?" cannot answer "does this unit have the attribute?".
        """
        node_id = props.get("nodeId")
        endpoint_id = props.get("endpointId")
        if not self._has_matter_node(props):
            return lambda cluster: None

        def lookup(cluster):
            try:
                return self.device_sync.attribute_list(int(node_id), int(endpoint_id), cluster)
            except Exception as exc:  # pylint: disable=broad-except
                # unknown, never "absent"
                self.logger.debug("attribute-list lookup failed for node %s/%s: %s",
                                  node_id, endpoint_id, exc)
                return None

        return lookup

    def _save_removed_state_log(self, blob: str) -> None:
        """Persist ``removed_state_log`` (issue #312).

        Same discipline as ``DiagnosticsMenuMixin._save_survey_log``: written
        through ``self.pluginPrefs`` at call time rather than a captured
        mapping, because Indigo rebinds it when the PluginConfig dialog is
        saved, and committed via ``savePluginPrefs`` without which it survives
        only until the plugin stops.

        Caught and logged rather than left to escape into
        ``RemovedStateLog._persist``, which is deliberately silent for the same
        reason ``SurveyLog._persist`` is (issue #308: the caller supplies the
        save hook and logs there). A failure here is cosmetic — the affected
        device(s) simply reprint their removed-state INFO once more on the
        next rebuild — which is why WARNING rather than anything louder.
        """
        try:
            self.pluginPrefs[REMOVED_STATE_LOG_PREF] = blob
            indigo.server.savePluginPrefs()
        except Exception as exc:  # pylint: disable=broad-except
            # bookkeeping must never sink a device rebuild
            self.logger.warning(
                "Matter: could not save the removed-state log (%s) — the affected "
                "device(s) will reprint their state-removal notice on the next rebuild.", exc)

    def getDeviceStateList(self, dev):  # noqa: N802
        """Build this device's states from what the DEVICE says it implements.

        Devices.xml declares a setting's state for a whole device *type*, so
        Indigo creates it on every unit of that type and initialises it to 0
        (issue #190). For a unit that does not implement the attribute, that 0 is
        Indigo's default and means nothing — but it is indistinguishable from a
        real reading, and for StartUpOnOff it is even a *valid* value ("Off"), so
        it reads as genuine configuration. A state is visible to triggers,
        control pages and the API, so someone can build a trigger on a setting
        the device has no concept of and watch it never fire.

        The device's own AttributeList is the authority; a reading never is.
        Unlike ConfigUI fields — static XML per type, no runtime injection —
        states CAN be built per device, which is what this does.

        Three rules, all load-bearing:

        * The parent's list is Indigo's live internal cache for the type, not a
          copy. Returning a filtered ``indigo.List`` leaves it untouched;
          mutating it in place would corrupt every later call and eventually the
          XML serialiser.
        * Only a positive NO removes a state — see
          ``device_settings.unimplemented_states``. Unknown keeps it.
        * Anything unexpected keeps the whole list. A device that fails to build
          its states is far worse than a spurious 0.
        """
        states = super().getDeviceStateList(dev)
        try:
            absent = device_settings.unimplemented_states(
                dev.deviceTypeId,
                self._setting_attribute_list_lookup(dict(dev.pluginProps)))
        except Exception as exc:  # pylint: disable=broad-except
            # never break a device's state list
            # .exception, not .debug: nothing expected can reach here. The gate
            # is a comprehension over a module constant plus `implements`, which
            # is total, and the attribute-list lookup swallows its own failures.
            # So this only ever fires on a programming error — and because it
            # fails open, the sole symptom would be #190 quietly not working.
            self.logger.exception(exc)
            return states
        if not absent or states is None:
            return states
        drop = {setting.key: setting for setting in absent}
        kept = indigo.List()
        removed = []
        unreadable = 0
        total = len(states)
        for state in states:
            try:
                key = state["Key"]
            except Exception:  # pylint: disable=broad-except
                # an entry we cannot read is an entry we keep
                unreadable += 1
                kept.append(state)
                continue
            if key in drop:
                removed.append(key)
            else:
                kept.append(state)
        if unreadable:
            # Entry shape is uniform, so this is all-or-nothing in practice: if
            # one entry is unreadable the whole list is, nothing is ever removed,
            # and #190 silently does nothing on every device. Worth a line —
            # "Key" is not actually documented in the SDK reference, so a future
            # Indigo could rename it and this is the only warning anyone gets.
            self.logger.warning(
                'device "%s": %d of %d state entries could not be read, so the Matter '
                "capability filter was not applied to them", dev.name, unreadable, total)
        if removed:
            # Logged once per distinct ANSWER, not once per evaluation (issue
            # #312). The filtering above is correct to re-run on every rebuild
            # — Indigo calls this as a filter each time, and the cache the
            # answer is drawn from can have changed — but reprinting the same
            # removed-set on every rebuild was pure noise: ~3 lines/device on
            # every plugin start, forever, for an answer that essentially never
            # changes within a session. removed_state_log persists a
            # fingerprint per device so a genuinely NEW or CHANGED answer still
            # logs (including the very first time) while an unchanged repeat
            # does not. `None` (not wired — only possible if getDeviceStateList
            # runs before startup finishes) fails open to the old behaviour
            # rather than silently going quiet.
            log = getattr(self, "removed_state_log", None)
            fingerprint = device_settings.RemovedStateLog.fingerprint(removed)
            if log is None or log.should_log(dev.id, fingerprint):
                # INFO, and worth it: a state disappearing silently breaks any
                # trigger or control page bound to it, with nothing in the log to
                # connect the two. One line per device rather than per state.
                #
                # Frequency: NOT at device start — deviceStartComm forces a rebuild
                # before the first reconcile has run, so the cache is cold, every
                # answer is unknown and nothing is removed. The line comes from the
                # first _refresh_state_lists after that reconcile, and thereafter
                # when a node's AttributeLists change OR the pass created a device
                # (the refresh covers the whole node, so a new sibling reprints this
                # for the devices already filtered). Indigo also rebuilds when an
                # Edit Device dialog is dismissed, so editing one reprints it.
                self.logger.info(
                    'device "%s": removed the state(s) %s — this unit\'s AttributeList says it '
                    "does not implement %s, so the values shown were Indigo's defaults rather "
                    "than anything the device reported",
                    dev.name, ", ".join(f'"{key}"' for key in removed),
                    ", ".join(f"0x{drop[key].attribute:04X}" for key in removed))
                if log is not None:
                    log.record(dev.id, fingerprint)
        return kept

    def getDeviceConfigUiValues(self, pluginProps, typeId, devId):  # noqa: N802
        """Seed the Edit Device dialog with this device's CURRENT settings.

        Runs every time the dialog opens, and re-seeds from the device's Indigo
        states rather than from saved props — the device is the source of truth
        for a setting, not Indigo. That is what keeps the dialog honest when the
        value was last changed from another ecosystem (these devices are
        typically multi-admin), and it is why a write that fails does not leave
        a prop quietly disagreeing with the device forever.

        No live Matter read happens here: this is the Indigo UI thread, and a
        read from a sleepy device is seconds. See the device_settings docstring.
        """
        values = dict(pluginProps)
        # A brand-new device (devId 0) or one deleted mid-dialog has no states
        # and no node, so device_known below is False and the section is hidden.
        # That is an explicit gate, not an emergent one — spec-bounded settings
        # resolve with no device at all and used to be offered regardless.
        states = self._device_states(devId)
        try:
            values.update(device_settings.config_ui_values(
                typeId, states, self._setting_limits_lookup(values),
                attribute_list=self._setting_attribute_list_lookup(values),
                device_known=self._has_matter_node(values),
                on_skip=self._log_setting_skips(devId)))
            if typeId == "matterNode":
                # Display-only summary of settings_report's SurveyLog for THIS
                # node (issue #204): the store stays in pluginPrefs (see
                # settings_report.SurveyLog's docstring for why), but its answer
                # now has somewhere to be read back besides the event log.
                # INSIDE this try, not after it: an uncaught exception here
                # would escape getDeviceConfigUiValues entirely, skip the
                # indigo.Dict(...) return below, and hit the C++ bridge with
                # nothing at all — the #186 failure mode the comment on that
                # return already documents.
                values["lastSurveySummary"] = self._survey_summary_for(values)
        except Exception as exc:  # pylint: disable=broad-except
            # never block the dialog over the settings section
            self.logger.exception("could not build device settings for %s: %s", devId, exc)
        # MUST be indigo.Dict, not a plain dict — the same shape
        # getPrefsConfigUiValues returns. Returning a plain dict fails INSIDE
        # Indigo's C++ bridge, which logs
        #   Error in plugin execution UiGetValues2: No registered converter was
        #   able to extract a C++ reference to type CXmlDict from this Python
        #   object of type dict
        # and then seeds NOTHING. For a section gated on hidden marker fields
        # that reads as "the feature is missing": the markers keep their
        # Devices.xml defaultValue of "no" and every field stays invisible, with
        # the only clue an error naming an internal symbol rather than this
        # method. Cost an hour on jarvis (#186); the tuple type is load-bearing.
        return (indigo.Dict(values), indigo.Dict())

    # Indigo 2025.2's PluginBase carries snake_case aliases for these callbacks
    # alongside the camelCase names and does not document which it dispatches
    # on — the same hedge getPrefsConfigUiValues above already makes. On this
    # build the camelCase names ARE the ones called (proven by the UiGetValues2
    # error above arriving at all), so these are belt-and-braces against a
    # future build preferring the other spelling, not a fix for anything
    # observed.
    def get_device_config_ui_values(self, pluginProps, typeId, devId):
        return self.getDeviceConfigUiValues(pluginProps, typeId, devId)

    def validate_device_config_ui(self, valuesDict, typeId, devId):
        return self.validateDeviceConfigUi(valuesDict, typeId, devId)

    def closed_device_config_ui(self, valuesDict, userCancelled, typeId, devId):
        return self.closedDeviceConfigUi(valuesDict, userCancelled, typeId, devId)

    def getSettingOptions(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002
        """Picker rows for ANY menu-rendered setting in the Edit Device dialog.

        One callback for every such field: Indigo tells a list method which
        field asked via the ``<List filter="…">`` attribute, so the filter
        carries the setting key and this does not need a method per setting.

        Distinct from :meth:`getSensitivityLevels` (the issue #85 ACTION's
        picker) because the two degrade differently, and deliberately so. The
        action's list must always offer something — it is building an automation
        that may run long before the device is ever reachable. This one is
        backed by the same bounds that gate the field's visibility, so when the
        device's values are unknown the field is not shown at all and there is
        nothing to guess about.
        """
        props = dict(valuesDict or {})
        try:
            dev = indigo.devices[targetId]
            props.setdefault("nodeId", dev.pluginProps.get("nodeId"))
            props.setdefault("endpointId", dev.pluginProps.get("endpointId"))
        except Exception as exc:  # pylint: disable=broad-except
            # fall back to whatever the dialog carries
            self.logger.debug("getSettingOptions: no device %r: %s", targetId, exc)
        wanted = str(filter or "").strip()
        for offer in device_settings.offered_settings(
                typeId, self._setting_limits_lookup(props),
                self._setting_attribute_list_lookup(props),
                self._has_matter_node(props)):
            if wanted and offer.setting.key != wanted:
                continue
            rows = offer.setting.option_rows(offer.bounds)
            if rows is not None:
                return rows
        return []

    def closedDeviceConfigUi(self, valuesDict, userCancelled, typeId, devId):  # noqa: N802
        """Apply any CHANGED device settings after the dialog closes.

        The write lives here rather than in validation for one reason: a write
        and its read-back against a sleepy Thread device take seconds, and doing
        that inside validateDeviceConfigUi would freeze the Indigo UI with no
        way out. By the time this runs the dialog is gone, so the round trip
        goes onto the asyncio loop and reports through the event log and the
        device's error state instead of a dialog.

        The consequence worth knowing: a failure surfaces AFTER the dialog has
        closed. It is logged as an error, the device is marked in error, and
        because the dialog re-seeds from device state on every open, reopening
        it shows what the device actually holds — never the value that failed.
        """
        if userCancelled or not settings_for_type(typeId):
            return
        try:
            plans = device_settings.planned_writes(
                typeId, valuesDict, self._device_states(devId),
                self._setting_limits_lookup(valuesDict),
                self._setting_attribute_list_lookup(valuesDict),
                self._has_matter_node(valuesDict))
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: anything planned_writes() raises while working out what the
            # dialog changed. Safe because it aborts having submitted NOTHING, which is
            # the only correct direction for a write path: an unknown plan must never
            # become a burst of writes to a possibly battery-powered device (the
            # failure _device_states' docstring describes). No test drives this arm
            # (#310 audit).
            self.logger.exception("could not work out device setting changes: %s", exc)
            return
        if not plans:
            return
        if self.runtime is None or self.matter is None:
            self.logger.error(
                "cannot apply device settings — the Matter connection is not running; "
                "no setting was changed")
            return
        if not self._has_matter_node(valuesDict):
            # Belt-and-braces: planned_writes is already gated on the same
            # check, so reaching here means the two disagree. int() on an
            # absent node id would throw a raw traceback out of a dialog-close
            # callback, with no user-facing message and the remaining plans
            # abandoned.
            self.logger.error(
                "cannot apply device settings — device %s has no Matter node yet", devId)
            return
        node_id = int(valuesDict.get("nodeId"))
        endpoint_id = int(valuesDict.get("endpointId"))
        for plan in plans:
            self.runtime.submit(self._apply_setting(node_id, endpoint_id, int(devId), plan))

    def _device_states(self, dev_id) -> dict:
        """This device's states, or {} if it has none we can read.

        {} is not harmless: planned_writes compares against it to decide what
        CHANGED, so an empty mapping makes every offered setting look changed
        and turns a dialog opened to read values into a burst of writes to a
        possibly battery-powered device. Narrow catch and a log line, so that
        outcome is never silent — KeyError is the one anticipated case (a
        brand-new devId 0, or a device deleted mid-dialog).
        """
        try:
            return dict(indigo.devices[dev_id].states)
        except KeyError:
            self.logger.debug("device %s has no states (new or deleted mid-dialog)", dev_id)
            return {}
        except Exception as exc:  # pylint: disable=broad-except
            # unexpected, and it changes what we write
            self.logger.warning(
                "could not read states for device %s (%s) — every setting will look "
                "changed, so only settings you actually edited should be saved", dev_id, exc)
            return {}

    async def _apply_setting(self, node_id, endpoint_id, dev_id, plan) -> None:
        """Loop-side half of a settings write: send, verify, report.

        Runs on the asyncio loop, so the STATE write goes through
        ``device_sync.apply_states`` — the same discipline the attribute
        firehose and the export bridge's command dispatch already use. The error
        flag is set directly, as ``device_sync``'s own ``_safe_unreachable`` /
        ``_clear_error`` do from the same thread; the seam rule covers
        ``updateStatesOnServer``, not every Indigo call.
        """
        label = plan.setting.label
        observed: list = []
        try:
            ok, message = await device_settings.apply_setting(
                self.matter, node_id, endpoint_id, plan,
                on_observed=observed.append)
        except Exception as exc:  # pylint: disable=broad-except
            # a crash here must not kill the loop
            self.logger.exception('"%s" could not be applied: %s', label, exc)
            return
        if ok:
            self.logger.info("%s", message)
            # The read-back already proved this value is on the device, so this
            # is a confirmed write-through, not the optimistic echo the #85
            # action does. apply_states also clears any stale error flag.
            self.device_sync.apply_states(dev_id, [{"key": plan.setting.key,
                                                    "value": plan.value}])
            return
        self.logger.error("%s", message)
        try:
            dev = indigo.devices[dev_id]
            dev.setErrorStateOnServer("setting failed")
            # A mismatch means the read-back told us what the device ACTUALLY
            # holds. Recording it keeps the dialog honest on next open — it
            # re-seeds from state, so leaving the stale value would show the
            # user a setting the device never adopted, which is the divergence
            # this whole feature exists to prevent.
            actual = observed[0] if observed else None
            if actual is not None:
                self.device_sync.apply_states(
                    dev_id, [{"key": plan.setting.key, "value": actual}])
        except KeyError:
            # The device was deleted between the dialog closing and this
            # running on the loop — routine, and there is nothing left to
            # flag or to keep honest.
            self.logger.debug("could not flag failed setting: device %s is gone", dev_id)
        except Exception as exc:  # pylint: disable=broad-except
            # NOT gone: the device exists and the flag/state write failed, so
            # the dialog will re-seed from state and show a setting the device
            # never adopted — the exact divergence the comment above says this
            # write exists to prevent. The write failure was already reported
            # to the user at ERROR; this says the record of it did not stick.
            self.logger.warning(
                "the setting failure on device %s could not be recorded (%s) — the device's "
                "settings dialog may show a value the device never adopted", dev_id, exc)

    def deviceStartComm(self, dev):  # noqa: N802
        # Indigo builds a device's state list at creation and does NOT re-read
        # Devices.xml on plugin upgrade — without this refresh, states added in
        # a new release (e.g. colorCapabilities, issue #60) never exist on
        # fielded devices and every update logs an Indigo error.
        try:
            dev.stateListOrDisplayStateIdChanged()
        except Exception as exc:  # pylint: disable=broad-except
            # refresh failure must not block startComm
            # WARNING, not debug (issue #288 review finding E): since #288 a
            # failure here can mean ten of matterBridgeHealth's twelve states
            # are missing — Indigo silently discards a write to an undeclared
            # key, so the shared `_write_health_state` call stalls (including
            # `subscriptionHealth` itself) until the device is next restarted
            # cleanly. That is not a fact a user should only learn from a
            # debug log they were not tailing.
            self.logger.warning(
                "could not refresh device %s's state list (%s) — any states added by a "
                "plugin upgrade will be missing until this device restarts cleanly", dev.id, exc)
        # Backstop for type edits made while the plugin was not running (the
        # validateDeviceConfigUi guard can't fire then) — issue #58.
        created = dev.pluginProps.get("createdTypeId", "")
        if created and dev.deviceTypeId != created:
            self.logger.warning(
                "device %s (%s) was created as type %s but is now %s — Matter "
                "device types cannot be changed; delete the device and reload "
                "this plugin to recreate it correctly",
                dev.id, dev.name, created, dev.deviceTypeId,
            )
        self.device_sync.note_device(dev)
        self.device_sync.set_active(dev.id, True)
        if dev.deviceTypeId == "matterRelay":
            self._prime_retired_on_time_state(dev)
        if dev.deviceTypeId == "matterNode":
            self._prime_absent_node_energy_state(dev)

    def _prime_absent_node_energy_state(self, dev) -> None:
        """Flag a node device's energy states as meterless — unless real
        evidence has already landed (issue #204 review, fix E; ADR-0007's
        flagged-state rule; hardened by the #204 verifier's evidence-checked-
        prime finding).

        Every ``matterNode`` device declares ``curEnergyLevel``/
        ``accumEnergyTotal`` unconditionally (Devices.xml — ep 0 may host an
        unlinkable 0x0090/0x0091 source with nowhere else to land, issue
        #204/ADR-0008), but MOST nodes have no energy measurement on endpoint
        0 at all. Left at Indigo's bare integer default, that reads as
        "0 W, plug off" — a PLAUSIBLE reading, exactly the #190 failure mode
        this plugin already knows to avoid, not "there is no meter here".
        ``uiValue`` fixes the DISPLAY the same way ``_prime_retired_on_time_state``
        does — but unlike that state, the ``value`` half here is NOT cosmetic:
        it is a real state write, visible to triggers and conditional logic
        exactly like any other. ``onTime``'s "invisible" claim does not
        transfer to this state: nothing ever writes ``onTime`` again once
        primed, so its flagged value can never diverge from reality. This
        state DOES get written again — by device_sync's own ``_prime_states``
        on every connect/reconnect — so a prime here is a live write that can
        race a genuine reading, not an inert label.

        ``deviceStartComm`` re-fires on EVERY prop change (nodeBaseName
        restamp, ``_reassert_capability_props``, any Edit-dialog Save — there
        is no ``didDeviceCommPropertyChange`` override to narrow it), not just
        first start. Priming unconditionally there clobbers a real ep-0
        reading mid-session with 0 — ``accumEnergyTotal`` especially, since it
        moves slowly enough that SQL Logger would log a huge negative delta
        immediately followed by an equally huge positive one once the real
        value returned. The guard below is what prevents that: before
        writing, read the device's CURRENT ``curEnergyLevel`` uiValue directly
        off ``indigo.devices`` (the one place this method can see live state
        without a matter-server round trip). If it is already something other
        than blank or the ``<no meter>`` placeholder, a real reading is on the
        device and this method does nothing — device_sync's own priming pass
        owns that value from here.

        Never raises: a cosmetic label must not be able to stop device start,
        and neither must a failed evidence check — on a read failure this
        primes as if the device had no reading at all, same as before this
        guard existed.
        """
        try:
            existing_ui = str(indigo.devices[dev.id].states.get("curEnergyLevel.ui", ""))
        except KeyError:
            # The device is not in indigo.devices at all, so it cannot be
            # holding a real reading for the guard to protect — and if it is
            # genuinely gone the apply_states below lands on nothing. Falls
            # through to the unconditional prime deliberately: the state must
            # not be left at Indigo's plausible-looking bare 0 (#190) just
            # because a device was mid-creation when startComm ran. Pinned by
            # test_prime_absent_node_energy_state_primes_when_the_evidence_read_fails.
            existing_ui = ""
        except Exception as exc:  # pylint: disable=broad-except
            # A DIFFERENT failure, and the distinction is the whole fix. This
            # is not "no such device" — it is the lookup itself failing, which
            # means the device most likely still exists and may well be
            # holding a real reading. The guard could not be EVALUATED, so do
            # not prime.
            #
            # Priming is the destructive half of this method — the docstring
            # above records that an unguarded prime clobbers a real ep-0
            # reading mid-session, and that accumEnergyTotal in particular then
            # makes SQL Logger log a huge negative delta immediately followed
            # by an equally huge positive one. This guard is the only thing
            # standing between that and every deviceStartComm, and startComm
            # re-fires on EVERY prop change.
            #
            # Skipping a prime costs a bare "0" in the display until
            # device_sync's own _prime_states pass runs on the next connect.
            # Skipping the GUARD costs logged data that was real. The previous
            # handler collapsed both failures into one and chose priming for
            # both ("a failed read must not block priming"), which let a
            # transient read failure — precisely when the server is least
            # trustworthy and the device most likely still there — do the
            # damage the guard exists to prevent.
            self.logger.warning(
                "could not read the current energy reading of device %s (%s) — not flagging it "
                "as meterless, because that could overwrite a real reading", dev.id, exc)
            return
        if existing_ui not in ("", "<no meter>"):
            return  # a real reading is already on the device — leave it alone
        try:
            self.device_sync.apply_states(
                dev.id,
                [{"key": "curEnergyLevel", "value": 0, "uiValue": "<no meter>"},
                 {"key": "accumEnergyTotal", "value": 0, "uiValue": "<no meter>"}])
        except Exception as exc:  # pylint: disable=broad-except
            # cosmetic, must not break device start
            #
            # Absorbing: any failure of the placeholder write itself. Safe —
            # and note this is the ONE half of this method that is genuinely
            # cosmetic. The guard above is not: its failure can destroy a real
            # reading, which is why it no longer swallows (2026.29.7). Here the
            # write has already been cleared as safe by that guard, so losing
            # it costs a bare 0 in the display until device_sync's own
            # _prime_states pass runs on the next connect.
            self.logger.debug("could not flag absent node energy state for %s: %s", dev.id, exc)

    def _prime_retired_on_time_state(self, dev) -> None:
        """Flag the retired ``onTime`` state as dead, on every start (#197).

        The setting is gone for good, but the state stays — ADR-0006's
        Confirmation found that withdrawing a state leaves any trigger already
        bound to it silently orphaned, which is worse than keeping one that
        never moves (workspace ADR-0007). Nothing writes ``onTime`` any more
        (``on_off.py``'s ``SETTING_STATES`` no longer maps it), so without this
        it would just sit at whatever Indigo's own default is and look like a
        stalled reading rather than a dead field. ``uiValue`` fixes that: it is
        display-only (Indigo API v1.6+), invisible to triggers and conditional
        logic, so it cannot repeat the #190 "phantom zero" mistake — a user
        looking at the device's properties sees ``<unused>``, not ``0``.

        Called by ``deviceStartComm`` — runs after ``stateListOrDisplayStateIdChanged``,
        which is what puts ``onTime`` on the device's state list in the first
        place for units created before this state existed.

        Never raises: a cosmetic label must not be able to stop device start.
        """
        try:
            self.device_sync.apply_states(
                dev.id, [{"key": "onTime", "value": 0, "uiValue": "<unused>"}])
        except Exception as exc:  # pylint: disable=broad-except
            # cosmetic, must not break device start
            self.logger.debug("could not flag retired onTime state for %s: %s", dev.id, exc)

    def deviceStopComm(self, dev):  # noqa: N802
        self.device_sync.set_active(dev.id, False)

    # ------------------------------------------------------------------
    # Device actions → Matter commands (bridged onto the loop, 5s ack)
    # ------------------------------------------------------------------
    def actionControlDevice(self, action, dev):  # noqa: N802
        self._send_built_commands(self.device_sync.build_command(dev, action), dev)

    def actionControlThermostat(self, action, dev):  # noqa: N802
        self._send_built_commands(self.device_sync.build_command(dev, action), dev)

    def _send_built_commands(self, commands, dev) -> None:
        """Send a handler's command(s). Handlers may return one MatterAction or
        a list for composite operations (the colour W slider is CT mode + level
        — two Matter commands); each is sent and acked individually so a
        failure surfaces on the device exactly as a single command's would."""
        if commands is None:
            return
        if not isinstance(commands, list):
            commands = [commands]
        for command in commands:
            self._send_matter_command(command, dev)

    def actionControlSensor(self, action, dev):  # noqa: N802
        self.logger.info('ignored "%s" — Matter sensor is read-only', dev.name)

    def actionControlUniversal(self, action, dev):  # noqa: N802
        """Indigo's universal buttons: Request Status / Update / Reset / Beep.

        Without this method Indigo logs "plugin does not define method
        actionControlUniversal" whenever a user presses the energy Update/Reset
        (or status) buttons. Request Status and Energy Update re-interview the
        node (see _refresh_node) to re-read its attributes incl. power/energy.
        Energy Reset is surfaced as unsupported: Matter's accumulated energy is
        cumulative on the device and there is no Matter command to zero it (and
        silently bouncing the Indigo state to 0 would be undone by the device's
        next report). Beep / any other action are ignored."""
        universal = indigo.kUniversalAction
        cmd = action.deviceAction
        if cmd in (universal.RequestStatus, universal.EnergyUpdate):
            self._refresh_node(dev)
        elif cmd == universal.EnergyReset:
            self.logger.info(
                '"%s": Matter accumulated energy is cumulative on the device and '
                "cannot be reset from Indigo (no Matter command exists for it).", dev.name)
        else:
            self.logger.debug('ignored universal action %r for "%s"', cmd, dev.name)

    def getSensitivityLevels(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the Set Sensitivity Level action's picker.

        Reads SupportedSensitivityLevels (0x0080/0x0001), cached by device_sync
        at the device's last create/reconcile pass — get_node's snapshot isn't
        otherwise retained (issue #85). A CONFIRMED count of 3 gets the Aqara
        FP300's real labels (Low/Standard/High); any other confirmed count gets
        generic "Level N" options. Unknown (device not yet reconciled, or
        offline) never presumes the FP300 labelling — it degrades to a generic
        0..2 scale so the dialog is never empty but also never lies about what
        the device actually supports.
        """
        known = None
        try:
            dev = indigo.devices[targetId]
            node_id = dev.pluginProps.get("nodeId")
            endpoint_id = dev.pluginProps.get("endpointId")
            if node_id and endpoint_id not in (None, ""):
                known = self.device_sync.sensitivity_levels_supported(int(node_id), int(endpoint_id))
        except Exception as exc:  # pylint: disable=broad-except
            # never break the dialog; degrade to the generic fallback
            self.logger.debug("getSensitivityLevels: could not resolve device %r: %s", targetId, exc)
        if known and known > 0:
            labels = ["Low (0)", "Standard (1)", "High (2)"] if known == 3 else [f"Level {i}" for i in range(known)]
        else:
            labels = [f"Level {i}" for i in range(3)]  # unconfirmed — generic 3-level fallback
        return [(str(i), label) for i, label in enumerate(labels)]

    def getCtCalibrationScope(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback for the Calibrate Colour-Temperature Bounds action's
        "Sweep:" field (issue #293's calibration extension): "all" plus one
        row per currently-exported CT-role device, so a person (or an
        automation calling executeAction) can target everything or one lamp.

        A device that fails to resolve costs its row, not the whole list —
        same discipline as ``ExportDialogMixin.getExportCandidates``'s own
        per-row try/except, just without that method's truncation/dedup
        machinery, because this list is bounded by the export allow-list,
        not by every Indigo device.
        """
        options = [(ALL_OPTION_ID, "All exported colour-temperature lights")]
        if self.exports is None:
            return options
        try:
            for entry in self.exports.all():
                if entry.role not in ct_bounds.CT_ROLES:
                    continue
                dev = self._indigo_device(entry.indigo_device_id)  # pylint: disable=no-member  # ExportDialogMixin (issue #146)
                name = str(getattr(dev, "name", "") or "") if dev is not None else \
                    f"(deleted device {entry.indigo_device_id})"
                options.append((str(entry.indigo_device_id), name))
        except Exception as exc:  # pylint: disable=broad-except
            # a broken picker must still offer "all"
            self.logger.debug("getCtCalibrationScope: could not list CT exports: %s", exc)
        return options

    def actionCalibrateCtBounds(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        """Issue #293's calibration action: parse the "Sweep:"/"Skip lamps
        that are currently on" props and hand off to
        ``CTCalibrationEngine.start``, which does the actual work on its own
        background thread — see ``ct_calibration.py``'s module docstring for
        why this callback must not do any of that work itself.

        Returns promptly whether or not ``callerWaitingForResult`` (an
        ``executeAction(..., waitUntilDone=True)`` caller) is set: Indigo's
        wait is satisfied by this method RETURNING, not by the sweep
        finishing, and there is nothing about this action worth a result
        value — its outcome is the log, exactly like
        ``actionShareMatterNode``.

        Props are read tolerantly (``action.props`` is an ``indigo.Dict``-
        like mapping, but a scripted ``executeAction`` call can hand either a
        ``str`` or an ``int`` for a device-id-shaped field) — ``scope`` is
        coerced through ``str`` before comparing to :data:`ALL_OPTION_ID` and
        through ``int`` before use as a device id, and either failure is
        reported here rather than left for the engine to discover, because
        the engine's own device-id filtering only ever silently DROPS an
        unparseable id (see ``CTCalibrationEngine.start``).
        """
        if self._ct_calibration is None:
            self.logger.error(
                "Calibrate Colour-Temperature Bounds: the export bridge has not started yet — "
                "try again once the plugin has finished starting.")
            return
        skip_lit = self._truthy(action.props.get("skipLit", True))  # pylint: disable=no-member  # ExportDialogMixin (issue #146)
        scope = str(action.props.get("scope", ALL_OPTION_ID) or ALL_OPTION_ID)
        device_ids = None
        if scope != ALL_OPTION_ID:
            try:
                device_ids = [int(scope)]
            except (TypeError, ValueError):
                self.logger.error(
                    "Calibrate Colour-Temperature Bounds: invalid device selection %r", scope)
                return
        self._ct_calibration.start(device_ids=device_ids, skip_lit=skip_lit)

    def actionSetSensitivityLevel(self, action, dev):  # noqa: N802
        """Custom device action (issue #85): write BooleanStateConfiguration's
        writable CurrentSensitivityLevel (0x0080/0x0000) — the Aqara FP300's
        motion sensitivity (co-located with OccupancySensing), or a contact
        sensor's equivalent per the Matter spec's own BooleanState pairing."""
        try:
            level = int(action.props.get("level", ""))
        except (TypeError, ValueError):
            self.logger.error('"%s": invalid sensitivity level %r', dev.name, action.props.get("level"))
            return
        if "sensitivityLevel" not in dev.states:
            # Defense-in-depth: the two Actions.xml entries are already scoped
            # to motion/contact types, but a stale saved action (or a future
            # filter change) could still hand us a device without the state.
            self.logger.error('"%s": device does not support sensitivity (no Boolean State '
                              'Configuration cluster)', dev.name)
            return
        node_id = dev.pluginProps.get("nodeId")
        endpoint_id = dev.pluginProps.get("endpointId")
        if not node_id or endpoint_id in (None, ""):
            self.logger.error('"%s": cannot set sensitivity — device has no Matter node/endpoint yet', dev.name)
            return
        supported = self.device_sync.sensitivity_levels_supported(int(node_id), int(endpoint_id))
        if supported and not 0 <= level < supported:
            self.logger.error(
                '"%s": sensitivity level %d out of range (device supports 0-%d)',
                dev.name, level, supported - 1,
            )
            return
        # Routed through the same verified write path the Edit Device dialog
        # uses (issue #186): ONE mechanism, two entry points. This action stays
        # because it serves a job the dialog cannot — changing a setting from an
        # automation — but it no longer echoes optimistically. A device that ACKs
        # and ignores would otherwise leave Indigo reporting a sensitivity the
        # device never adopted, which is precisely the bug #186 exists to kill.
        #
        # Fire-and-forget onto the loop rather than blocking: verification is a
        # second round trip to a device that may be asleep, and this runs on the
        # thread executing a trigger or action group.
        if self.runtime is None or self.matter is None:
            self.logger.error('"%s": cannot set sensitivity — the Matter connection '
                              'is not running', dev.name)
            return
        previous = None
        try:
            previous = int(dev.states.get("sensitivityLevel"))
        except (TypeError, ValueError):
            pass
        plan = device_settings.PlannedWrite(_SENSITIVITY_SETTING, level, previous)
        self.runtime.submit(self._apply_setting(int(node_id), int(endpoint_id), dev.id, plan))

    def actionReportNodeSettings(self, action, dev):  # noqa: N802, ARG002
        """Device-targeted twin of the "Report settable Matter settings…" menu
        item (issue #204): the same ``report_settable_attributes(force=True)``
        path ``DiagnosticsMenuMixin.menuReportSettings`` already uses, scoped
        to the one node ``dev`` (a matterNode) names — so an automation, not
        just a person at the menu, can ask what this node exposes that the
        plugin does not offer yet."""
        node_id = dev.pluginProps.get("nodeId")
        if not node_id:
            self.logger.error('"%s": cannot report settings — device has no Matter node id', dev.name)
            return
        try:
            node = self._fetch_node(int(node_id))
        except (TypeError, ValueError):
            self.logger.error('"%s": invalid Matter node id %r', dev.name, node_id)
            return
        except MatterUnavailable as exc:
            self.logger.warning('"%s": could not survey — %s', dev.name, exc)
            return
        if node is None:
            self.logger.warning(
                '"%s": could not survey — matter-server does not know this node', dev.name)
            return
        self.device_sync.report_settable_attributes(node, force=True)

    def actionShareMatterNode(self, action, dev):  # noqa: N802
        """Device-targeted twin of "Share a Matter device with another
        ecosystem…" (issue #210): the same ``_share_node`` core
        ``MatterServerMenuMixin.menuShareMatterNode`` uses, scoped to the one node
        ``dev`` (a matterNode) names — reachable from a control page,
        trigger, or action group, and the seam a future Domio "share this
        device" endpoint would call instead of the menu. There is no dialog
        here to report a refusal into, so the outcome only ever goes to the
        log — same shape as ``actionReportNodeSettings`` above.

        **``_share_node`` itself runs on a detached thread — unlike every
        other action in this file.** Indigo action callbacks share ONE queue
        with ``actionControlDevice``/``actionControlThermostat``/etc — the
        same queue a light switch's Toggle goes through. ``_share_node`` can
        block for up to ``SHARE_WINDOW_TIMEOUT`` (60s) waiting on
        matter-server, and unlike the modal menu item (which only blocks the
        Indigo UI thread the user pressing it is already staring at), this
        action is reachable from a trigger or schedule with nobody watching
        — a wedged matter-server would otherwise stall every OTHER action
        queued behind it, including turning off a light. Only ``_share_node``
        detaches: the node-id/duration validation above is synchronous,
        local, and fast (no I/O), so there is nothing to gain by threading it
        too, and doing so would only add a race to a plain validation error.
        """
        node_id = dev.pluginProps.get("nodeId")
        if not node_id:
            self.logger.error('"%s": cannot share — device has no Matter node id', dev.name)
            return
        try:
            node_id = int(node_id)
        except (TypeError, ValueError):
            self.logger.error('"%s": invalid Matter node id %r', dev.name, node_id)
            return
        errors = indigo.Dict()
        # pylint: disable=no-member  # PairingMenuMixin._window_duration via MRO (issue #146)
        duration = self._window_duration(action.props, errors)
        # pylint: enable=no-member
        if duration is None:
            self.logger.error('"%s": cannot share — %s', dev.name,
                              errors.get("duration", "invalid duration"))
            return
        self.logger.debug('"%s": share starting on a detached thread (node %s, %ds window)',
                          dev.name, node_id, duration)
        threading.Thread(
            target=self._run_share_node, args=(dev.name, node_id, duration),
            name="matter-share", daemon=True,
        ).start()

    def _run_share_node(self, dev_name: str, node_id: int, duration: int) -> None:
        """The detached-thread body :meth:`actionShareMatterNode` starts.

        Extracted to its own method so a test can call it directly and
        deterministically instead of joining a real ``threading.Thread`` —
        everything it does (``runtime.submit``, ``self.logger``) is already
        safe to call from any thread, so there is nothing about running it
        inline in a test that changes its behaviour.
        """
        # pylint: disable-next=no-member  # MatterServerMenuMixin (issue #146)
        ok, message = self._share_node(node_id, duration)
        if not ok:
            self.logger.error('"%s": share failed — %s', dev_name, message)

    def _refresh_node(self, dev) -> None:
        """Re-interview the device's Matter node so matter-server re-reads its
        attributes; matter-server then emits a node_updated event which
        _refresh_live_node turns into refreshed Indigo states (incl. power/
        energy via the electrical handlers).

        NOTE: the interview ⇒ node_updated emission is matter-server behaviour,
        not guaranteed here — if it stops firing, a refresh becomes a no-op.
        Mirrors _send_matter_command: visible error state on timeout/failure,
        cleared on success, and a debug line on every silent skip so a button
        that does nothing still leaves a trail."""
        if self.runtime is None or self.matter is None:
            self.logger.debug('refresh skipped for "%s" — plugin not fully started', dev.name)
            return
        node_id = dev.pluginProps.get("nodeId")
        if not node_id:
            self.logger.debug('refresh skipped for "%s" — no nodeId (device not yet reconciled)', dev.name)
            return
        try:
            self.runtime.submit(self.matter.interview_node(int(node_id))).result(timeout=COMMAND_TIMEOUT)
            self.logger.info('refreshed "%s" (node %s)', dev.name, node_id)
            if getattr(dev, "errorState", ""):
                dev.setErrorStateOnServer("")  # refresh succeeded — clear a stale error
        except FuturesTimeoutError:
            self.logger.error('refresh of "%s" timed out', dev.name)
            dev.setErrorStateOnServer("timeout")
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any non-timeout failure of interview_node — matter-server
            # down, node unreachable, or the node refusing. Safe because the user gets
            # an ERROR naming the device plus a visible errorState, and the two-value
            # vocabulary ("timeout" vs "cmd failed") is a deliberate UI contract pinned
            # by test_action_universal_refresh_timeout_logs_and_sets_error and its
            # failure twin.
            self.logger.error('refresh of "%s" failed: %s', dev.name, exc)
            dev.setErrorStateOnServer("cmd failed")

    def _send_matter_command(self, action, dev) -> bool:
        if self.runtime is None or self.matter is None:
            return False
        # An action is either a cluster-command invoke or an attribute write.
        coro = self.matter.write(action) if isinstance(action, MatterWrite) else self.matter.send_command(action)
        try:
            self.runtime.submit(coro).result(timeout=COMMAND_TIMEOUT)
            if getattr(dev, "errorState", ""):
                dev.setErrorStateOnServer("")  # command succeeded — clear a stale error
            return True
        except FuturesTimeoutError:
            self.logger.error("Matter command to %s timed out", dev.name)
            dev.setErrorStateOnServer("timeout")
            return False
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any non-timeout failure of a Matter command — the server being
            # down, or the device rejecting the cluster command (a CT-only light sent
            # MoveToHueAndSaturation, the live case behind af8540b). Safe because both
            # are reported at ERROR with the device named and both leave a visible
            # errorState; a downed server also marks devices unreachable independently
            # via _on_disconnected, so the shared "cmd failed" state is short-lived.
            self.logger.error("Matter command to %s failed: %s", dev.name, exc)
            dev.setErrorStateOnServer("cmd failed")
            return False

    # ------------------------------------------------------------------
    # asyncio → Indigo event bridge (called on the loop thread)
    # ------------------------------------------------------------------
    def _on_matter_event(self, evt) -> None:
        # Stage isolation: a device_sync failure must not starve the job
        # reconcile below — reconcile_node_added can still recover the job
        # (it runs its own device-create pass), so it always gets its shot.
        try:
            self.device_sync.handle_event(evt)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: anything device_sync.handle_event() raises for one event.
            # This is STAGE ISOLATION, not loop survival — the loop is already
            # protected two layers up (matter_client._handle_event, then
            # ws_json_client._handle_frame, each with its own reason). Without it a
            # device_sync failure would starve jobs.reconcile_node_added below, which
            # can still recover the commission on its own device-create pass. Pinned
            # by test_device_sync_failure_does_not_starve_reconcile.
            self.logger.exception(exc)
        # A node that arrives AFTER its commission job timed out is matter-server
        # finishing the join in the background (issue #16): device_sync has just
        # created the devices (bare product name); now let the job table claim it,
        # apply suggestedName/suggestedRoom, and flip the job back to success so a
        # still-polling client gets the real outcome.
        if self.jobs is not None and evt.kind == protocol.EVT_NODE_ADDED:
            data = evt.raw.get("data") if evt.raw else None
            if isinstance(data, dict):
                self.jobs.reconcile_node_added(data)

    def _on_late_matter_response(self, late) -> None:
        # self.jobs is None only in the startup gap between MatterClient's
        # construction (which wires this hook) and CommissionJobs' own — that
        # gap never reaches the loop today (matter.run() is not scheduled
        # until both exist), but this guard costs nothing and keeps the
        # invariant honest rather than assumed, same as _on_matter_event above.
        if self.jobs is not None:
            self.jobs.note_late_response(late)
        # A single MatterClient fans every late response out to whichever of
        # its several possible senders' contexts matches (issue #210):
        # note_late_response above only claims a CommissionRequest and drops
        # anything else; this claims a ShareWindowRequest the same way. Both
        # run unconditionally — a late answer names at most one of them via
        # isinstance, never both, so there is no double-handling to guard.
        self._note_late_share_response(late)  # pylint: disable=no-member  # MatterServerMenuMixin (issue #146)

