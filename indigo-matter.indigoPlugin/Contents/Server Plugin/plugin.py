"""indigo-matter — Matter device support for the Indigo home automation server.

Lifecycle glue only. All I/O lives on the asyncio loop owned by
:class:`AsyncRuntime`; this class wires the async services in ``startup``, runs a
non-I/O watchdog in ``runConcurrentThread``, tears everything down in
``shutdown``, bridges Indigo device actions onto the loop, and exposes the Domio
HTTP API as Indigo Web Server hidden-action handlers.

See ``docs/PRD-indigo-matter-plugin.md``, ``docs/IMPLEMENTATION.md`` (protocol +
scaffold) and ``docs/API.md`` (the Domio contract). matter-server protocol field
names are isolated in ``protocol.py``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import CancelledError as FuturesCancelledError
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

import indigo  # provided by the Indigo runtime

import fabric_backup
from async_runtime import AsyncRuntime
from commission_jobs import CommissionJobs, node_id_to_str
from device_sync import DeviceSync
import export_catalog
from export_store import ExportEntry, ExportStore, OPTION_INVERT
from http_handlers import HttpApi, MatterUnavailable
from matter_client import MatterClient
from matter_handlers.boolean_state_config import (
    ATTR_CURRENT_SENSITIVITY,
    CLUSTER_BOOLEAN_STATE_CONFIG,
)
from matter_handlers.registry import HandlerRegistry
import protocol
from protocol import MatterWrite, Protocol
from server_process import ServerProcess

PLUGIN_NAME = "indigo-matter"
COMMAND_TIMEOUT = 5.0
DECOMMISSION_TIMEOUT = 15.0

#: Menu id of the export dialog (MenuItems.xml) — matched in
#: ``get_menu_action_config_ui_values`` so other menus are never seeded.
MENU_MANAGE_EXPORTS = "manageMatterExports"
#: Option-id prefix marking a picker row the user may look at but not choose
#: (PRD §5.2: excluded devices are shown *with a reason*, never hidden — XAC9).
EXCLUDED_OPTION_PREFIX = "x-"
#: The "nothing selected" sentinel. Never "": Indigo rejects an empty list id
#: with "UI dynamic list function returned illegal ID string" and silently
#: drops the option. The picker always emits a REAL row carrying this id
#: (:data:`NO_SELECTION_LABEL`), because the dialog is seeded with it — a
#: seeded value with no matching row renders as a blank first item.
NO_SELECTION_ID = "0"
NO_SELECTION_LABEL = "— select a device —"
#: Informational rows. They get their own ids so :data:`NO_SELECTION_ID` stays
#: unique, and the ``x-`` prefix keeps them unpickable through the same door
#: excluded devices use.
TRUNCATED_OPTION = (f"{EXCLUDED_OPTION_PREFIX}truncated",
                    "…too many matches — narrow the filter")
NO_MATCH_OPTION = (f"{EXCLUDED_OPTION_PREFIX}nomatch", "(no devices match the filter)")
#: What a list callback returns when it fails outright. An empty list would
#: render as an empty popup the user cannot tell from "nothing to choose".
LIST_ERROR_OPTION = (NO_SELECTION_ID, "(error building list — see Event Log)")
#: One unreadable device inside an otherwise fine list (D3): the row is kept so
#: the count is honest, but it is not selectable.
ROW_ERROR_LABEL = "(error reading device — see Event Log)"
#: Picker cap. Past this the tail row asks the user to narrow the filter — a
#: 2000-device database would otherwise build an unusable popup menu.
EXPORT_PICKER_LIMIT = 300


def server_location(prefs: dict) -> str:
    """Resolve the one user-facing choice: is matter-server on this Mac?

    Returns ``"local"`` (the plugin runs and manages matter-server here on
    loopback) or ``"remote"`` (connect to a matter-server elsewhere).

    Migrates pre-2026.6 prefs that predate the ``serverLocation`` menu:
      * a managed LaunchAgent meant the plugin already ran the server here → local;
      * a host pointed at another machine → remote (keep its host/port);
      * anything else — a fresh install or a loopback self-run server → local,
        the turnkey default.
    """
    loc = str(prefs.get("serverLocation") or "").strip().lower()
    if loc in ("local", "remote"):
        return loc
    if prefs.get("manageLaunchAgent", False):
        return "local"
    host = str(prefs.get("matterServerHost") or "").strip().lower()
    if host and host not in ("localhost", "127.0.0.1", "::1"):
        return "remote"
    return "local"


def sanitize_host(raw: str) -> str:
    """Reduce a user-entered host to a bare hostname / IP.

    Users paste full URLs into the host field (e.g. ``http://jobs2.local:8176``);
    a scheme, an embedded port, and any path all corrupt ``ws://{host}:{port}{path}``.
    Strip them so the separate port field stays authoritative. IPv6 literals
    (multiple colons) are left untouched.
    """
    host = str(raw or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]  # drop any /path
    # strip an embedded :PORT (host:1234) but preserve IPv6 literals (many colons)
    if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host = host.rsplit(":", 1)[0]
    return host


class Plugin(indigo.PluginBase):
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
        # The export allow-list (PRD §5.1). Built in startup, before anything
        # can consult it; None means "the plugin has not started yet", which
        # every export callback checks rather than assuming.
        self.exports: ExportStore | None = None
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

        self.runtime = AsyncRuntime(self.logger)
        self.runtime.start()

        if managed:
            try:
                self.server_process = ServerProcess(prefs, self.logger)
                self.server_process.ensure_installed()
            except Exception as exc:  # noqa: BLE001
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
        )
        self.jobs = CommissionJobs(
            self.matter, self.device_sync.create_from_raw, self.logger,
            schedule=self.runtime.submit,
        )
        self.http = HttpApi(
            self.jobs, self.logger,
            status_provider=self._status_body,
            decommission_provider=self._decommission_sync,
            diagnostics_provider=self._diagnostics_sync,
        )

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
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("matter close error: %s", exc)
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
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
        if time.time() < self._restart_expected_until:
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
                        "Use Plugins ▸ Matter ▸ Restart matter-server (it stops stray "
                        "servers), or reboot the Mac if it persists.")
            self.logger.error(
                "matter-server is not responding after %d attempts and appears to be "
                "crashing. Recent matter-server errors:\n%s%s", attempts, tail, hint,
            )
        else:
            self.logger.error(
                "matter-server is not responding after %d attempts and its error log is "
                "empty — it may not be installed (checked %s). Use Plugins ▸ Matter ▸ "
                "Install/update matter-server, then restart the plugin.",
                attempts, sp.project_dir,
            )

    def _expect_restart(self) -> None:
        """Open the ~30s window during which a client outage is treated as an expected
        restart (see :meth:`_on_server_unreachable`), not a crash."""
        self._restart_expected_until = time.time() + 30
        self._restart_notice_shown = False

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

    def closedPrefsConfigUi(self, valuesDict, userCancelled):  # noqa: N802
        if userCancelled:
            return
        self.debug = bool(valuesDict.get("verboseLogging", False))
        # The connection + managed server are wired once in startup from a prefs
        # snapshot, so a changed location/host only takes effect on reload.
        self.logger.info(
            "matter-server settings saved — reload the plugin (or Plugins ▸ Matter ▸ "
            "Restart matter-server) to apply them"
        )

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
        """
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
        return (True, valuesDict)

    def deviceStartComm(self, dev):  # noqa: N802
        # Indigo builds a device's state list at creation and does NOT re-read
        # Devices.xml on plugin upgrade — without this refresh, states added in
        # a new release (e.g. colorCapabilities, issue #60) never exist on
        # fielded devices and every update logs an Indigo error.
        try:
            dev.stateListOrDisplayStateIdChanged()
        except Exception as exc:  # noqa: BLE001 - refresh failure must not block startComm
            self.logger.debug("state list refresh failed for %s: %s", dev.id, exc)
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
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to the generic fallback
            self.logger.debug("getSensitivityLevels: could not resolve device %r: %s", targetId, exc)
        if known and known > 0:
            labels = ["Low (0)", "Standard (1)", "High (2)"] if known == 3 else [f"Level {i}" for i in range(known)]
        else:
            labels = [f"Level {i}" for i in range(3)]  # unconfirmed — generic 3-level fallback
        return [(str(i), label) for i, label in enumerate(labels)]

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
        write = MatterWrite(int(node_id), int(endpoint_id), CLUSTER_BOOLEAN_STATE_CONFIG,
                             ATTR_CURRENT_SENSITIVITY, level)
        if self._send_matter_command(write, dev):
            # Optimistic echo (precedent: color_control's whiteLevel echo) — the
            # firehose attribute_updated report will confirm/correct this once
            # matter-server processes the write.
            try:
                dev.updateStateOnServer("sensitivityLevel", level)
            except Exception as exc:  # noqa: BLE001 - cosmetic echo only, must not fail the action
                self.logger.debug('optimistic sensitivityLevel echo failed for "%s": %s', dev.name, exc)

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
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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

    # ------------------------------------------------------------------
    # HTTP API (IWS hidden-action handlers — API.md v1.1)
    # ------------------------------------------------------------------
    def http_status(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        status, body = self.http.status()
        return self._reply(status, body)

    def http_commission(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        method, path_args, query = self._parse_request(action)
        status, body = self.http.commission(method, path_args, query)
        return self._reply(status, body)

    def http_decommission(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        method, path_args, query = self._parse_request(action)
        status, body = self.http.decommission(method, path_args, query)
        return self._reply(status, body)

    def http_diagnostics(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        _method, path_args, query = self._parse_request(action)
        status, body = self.http.diagnostics(path_args, query)
        return self._reply(status, body)

    @staticmethod
    def _parse_request(action):
        props = dict(action.props)
        method = props.get("incoming_request_method", "GET")
        path_args = list(props.get("file_path", []) or [])
        query = dict(props.get("url_query_args", {}) or {})
        body_params = props.get("body_params")
        if body_params:
            query = {**query, **dict(body_params)}
        return method, path_args, query

    @staticmethod
    def _reply(status, body):
        reply = indigo.Dict()
        reply["status"] = status
        reply["headers"] = indigo.Dict({"Content-Type": "application/json"})
        reply["content"] = json.dumps(body)
        return reply

    # ----- providers used by HttpApi (bridge into the loop where needed) -----
    def _status_body(self) -> dict:
        # server_info fields per ws-controller v0.6.2: sdk_version, fabric_id,
        # bluetooth_enabled (there is no plain "version" key).
        connected = bool(self.matter is not None and self.matter.connected)
        info = (self.matter.server_info if self.matter else None) or {}
        body = {
            "ready": connected,
            "controllerVersion": self._version,
            "matterServerReachable": connected,
            "matterServerVersion": str(info.get("sdk_version", "unknown")),
            "fabricId": str(info.get("fabric_id", "")),
            "nodeCount": self.device_sync.node_count(),
            "bleAvailable": bool(info.get("bluetooth_enabled", False)),
            "uptime": int(time.monotonic() - self._start_ts),
        }
        if not connected:
            # API.md §3.1: the 503 body carries the standard error envelope so
            # Domio can surface an actionable message, not a generic failure.
            uri = getattr(self.matter, "uri", None) if self.matter else None
            body["error"] = "matter_server_unreachable"
            body["message"] = f"Cannot reach matter-server at {uri}" if uri else "Cannot reach matter-server"
        return body

    def _decommission_sync(self, node_id):
        # None → genuine unknown node (404). MatterUnavailable → 503. Other → 500.
        if self.runtime is None:
            raise MatterUnavailable("plugin not ready")
        if self.matter is None:
            # Without this guard the AttributeError inside _decommission would be
            # misread as "device offline" and the Indigo devices deleted anyway.
            raise MatterUnavailable("matter-server not connected")
        try:
            return self.runtime.submit(self._decommission(node_id)).result(timeout=DECOMMISSION_TIMEOUT)
        except FuturesTimeoutError as exc:
            self.logger.error("decommission %s timed out", node_id)
            raise MatterUnavailable("matter-server timed out") from exc
        except MatterUnavailable:
            raise
        except RuntimeError as exc:  # asyncio runtime not running
            raise MatterUnavailable(str(exc)) from exc

    async def _decommission(self, node_id):
        # Captured BEFORE the delete: it is the only way to tell "node we have
        # never heard of" (a genuine 404) from "node we know, whose removal
        # failed". Conflating them told the user "Unknown node" about a node that
        # was still commissioned, and — for a node with no Indigo devices, where
        # removed_ids is always empty — dropped it from the picker so the
        # decommission could not be retried (issue #111 review).
        known = self.device_sync.knows_node(node_id)
        fabric_removed = True
        try:
            await self.matter.remove_node(node_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("remove_node failed (device may be offline): %s", exc)
            fabric_removed = False
        # Only forget the node if it actually left the fabric; otherwise it must
        # stay listed so the user can retry.
        removed_ids = self.device_sync.delete_node(node_id, forget=fabric_removed)
        if not removed_ids and not fabric_removed and not known:
            return None  # genuinely unknown and unreachable → 404
        return {
            "nodeId": node_id_to_str(node_id),
            "removedIndigoDeviceIds": removed_ids,
            "fabricRemoved": fabric_removed,
        }

    def _diagnostics_sync(self, node_id):
        if self.runtime is None:
            raise MatterUnavailable("plugin not ready")
        try:
            return self.runtime.submit(self._diagnostics(node_id)).result(timeout=DECOMMISSION_TIMEOUT)
        except FuturesTimeoutError as exc:
            self.logger.error("diagnostics %s timed out", node_id)
            raise MatterUnavailable("matter-server timed out") from exc
        except MatterUnavailable:
            raise
        except RuntimeError as exc:
            raise MatterUnavailable(str(exc)) from exc

    async def _diagnostics(self, node_id):
        from matter_model import parse_node
        try:
            raw = await self.matter.get_node(node_id)
        except ConnectionError as exc:
            raise MatterUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - protocol/timeout error reading the node
            self.logger.warning("diagnostics get_node(%s) failed: %s", node_id, exc)
            raise MatterUnavailable(str(exc)) from exc
        if not raw:
            return None  # genuine unknown node → 404
        node = parse_node(raw)
        return {
            "nodeId": node_id_to_str(node_id),
            "reachable": True,
            "vendorId": node.vendor_id,
            "productId": node.product_id,
            "vendorName": node.vendor_name,
            "productName": node.product_name,
            "softwareVersion": node.sw_version,
            "endpoints": [
                {
                    "endpointId": ep.endpoint_id,
                    "clusters": sorted(ep.cluster_ids),
                    "indigoDeviceId": self.device_sync.lookup(node.node_id, ep.endpoint_id),
                }
                for ep in node.endpoints
            ],
        }

    # ------------------------------------------------------------------
    # Menu items
    # ------------------------------------------------------------------
    def menuInstallMatterServer(self):  # noqa: N802
        """Install/update the matter-server npm package, then pin the node used.

        Only meaningful in local (managed) mode — self-managers keep their own
        server untouched. Runs off the Indigo main thread so the UI never blocks on
        npm; progress and outcome go to the log.
        """
        if server_location(self.pluginPrefs) != "local":
            self.logger.error(
                "Install is only for local mode. Set 'is matter-server on this Mac?' "
                "to local (managed) first, or install/manage the server yourself."
            )
            return
        if self._install_thread is not None and self._install_thread.is_alive():
            self.logger.warning("matter-server install already in progress.")
            return
        self.logger.info("Starting matter-server install in the background — watch the "
                         "log for progress; this can take a minute.")
        self._install_thread = threading.Thread(
            target=self._install_matter_server, name="matter-install", daemon=True)
        self._install_thread.start()

    def _install_matter_server(self, clean: bool = False) -> None:
        try:
            sp = self.server_process or ServerProcess(self._server_prefs(), self.logger)
            if clean:
                # "Start fresh": delete node_modules and reinstall. Stops/reaps the server
                # first and leaves the storage (fabric/pairings) intact. Used to recover a
                # matter-server that won't start after an upgrade.
                self.logger.info("Removing the installed matter-server for a clean "
                                 "reinstall (your devices/pairings are kept)…")
                sp.remove_package()
            if not sp.install():
                self.logger.error(
                    "Install/update matter-server did not complete — see the error "
                    "above. The server was not (re)installed; retry when resolved."
                )
                return
            if self._stopping:  # plugin is tearing down — don't mutate its state
                return
            # Pin the exact node used so the LaunchAgent runs the same one forever —
            # this is what keeps install-node == run-node and avoids ABI crash-loops.
            self.pluginPrefs["nodeBinDir"] = sp.resolved_bin_dir
            indigo.server.savePluginPrefs()
            self.server_process = ServerProcess(self._server_prefs(), self.logger)
            self.server_process.ensure_installed()
            # Restart matter-server onto the just-installed version — otherwise the
            # newly-installed package sits on disk while the OLD process keeps running
            # (a running LaunchAgent doesn't pick up new files). This is what makes the
            # menu action a one-click, no-CLI update.
            self._expect_restart()
            if not self.server_process.restart():
                # Don't claim success: the new version may not be running.
                self._restart_expected_until = 0.0  # let the crash diagnostic work
                self.logger.error(
                    "matter-server was installed and pinned to node at %s, but the "
                    "restart onto the new version FAILED — the old version may still be "
                    "running. Use Plugins ▸ Matter ▸ Restart matter-server, or reload "
                    "the plugin.", sp.resolved_bin_dir,
                )
                return
            self.logger.info(
                "matter-server installed, pinned to node at %s, and restarting onto the "
                "new version — it reconnects automatically.", sp.resolved_bin_dir,
            )
        except Exception as exc:  # noqa: BLE001
            # npm may have succeeded and only the pin/activate step failed — say so, so
            # the user doesn't reinstall in circles chasing a downstream problem.
            self.logger.exception(exc)
            self.logger.error(
                "matter-server install did not complete after the npm step — the "
                "package may be installed but the node was not pinned and the server "
                "was not (re)started. See the trace above, then retry Plugins ▸ Matter "
                "▸ Install/update matter-server."
            )

    def menuReinstallMatterServerClean(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Menu callback: delete matter-server and reinstall it fresh (keeps devices).

        The "blow it all away and start over" recovery when matter-server won't start
        after an upgrade (e.g. a wedged install or a stray process). Removes
        ~/indigo-matter/node_modules and reinstalls; the fabric/storage is left intact so
        commissioned devices survive. Runs in the background like the plain install.
        """
        errors = indigo.Dict()
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm the reinstall."
            return (False, valuesDict, errors)
        if server_location(self.pluginPrefs) != "local":
            errors["confirm"] = ("Reinstall is only for local (managed) mode. Set 'is "
                                 "matter-server on this Mac?' to local first.")
            return (False, valuesDict, errors)
        if self._install_thread is not None and self._install_thread.is_alive():
            errors["confirm"] = "An install is already in progress — wait for it to finish."
            return (False, valuesDict, errors)
        self.logger.info("Starting a clean matter-server reinstall in the background — "
                         "watch the log for progress; this can take a minute.")
        self._install_thread = threading.Thread(
            target=self._install_matter_server, kwargs={"clean": True},
            name="matter-reinstall", daemon=True)
        self._install_thread.start()
        return (True, valuesDict)

    def menuRestartMatterServer(self):  # noqa: N802
        if self.server_process is None:
            self.logger.warning("LaunchAgent management is off; start matter-server manually")
            return
        # Rebuild from CURRENT prefs first. ServerProcess snapshots prefs at construction
        # and restart() bootstraps the plist *as it is on disk*, which only
        # ensure_installed() regenerates — so without this, a setting changed since
        # startup (notably the attestation flag) is silently NOT applied and this menu
        # still logs success.
        self.server_process = ServerProcess(self._server_prefs(), self.logger)
        self._expect_restart()  # expected outage, not a crash
        try:
            # None = preflight failed (plist torn down, nothing to restart);
            # True = it already reloaded launchd, so a restart() here would stop and
            # start the server a SECOND time for nothing — two outages, every device's
            # session dropped twice; False = job left running, so we do the restart.
            reloaded = self.server_process.ensure_installed()
            restarted = True if reloaded else (
                False if reloaded is None else self.server_process.restart()
            )
        except Exception as exc:  # noqa: BLE001
            # Unguarded, this would escape with the expected-restart window still armed,
            # suppressing the crash diagnostic for 30s while the server is down.
            self._restart_expected_until = 0.0
            self.logger.exception(exc)
            return
        if restarted:
            self.logger.info("matter-server restart requested")
        elif reloaded is None:
            self._restart_expected_until = 0.0
            self.logger.error(
                "matter-server cannot be restarted — see the error above. Its LaunchAgent "
                "was removed to stop a crash-loop; fix the cause, then reload the plugin."
            )
        else:
            self._restart_expected_until = 0.0  # restart failed — don't suppress the diagnostic
            self.logger.error(
                "matter-server restart failed; check ~/Library/Logs/indigo-matter/matter-server.err.log"
            )

    def menuShowMatterServerLogs(self):  # noqa: N802
        self.logger.info("matter-server log: ~/Library/Logs/indigo-matter/matter-server.log")

    def menuCommissionDeviceManually(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        if self.jobs is None:
            # Surface WHY OK did nothing — a bare (False, valuesDict) leaves the
            # dialog open with no explanation at all.
            self.logger.warning("manual commission requested before the plugin finished starting")
            errors = indigo.Dict()
            errors["setupCode"] = "Plugin still starting — try again in a moment."
            return (False, valuesDict, errors)
        status, body = self.jobs.create_job({
            "setupCode": valuesDict.get("setupCode", ""),
            "suggestedName": valuesDict.get("suggestedName", "Matter Device"),
            # The picker's value is a folder id; map it back to the folder NAME and
            # pass it as suggestedRoom, which device_sync resolves to that folder
            # (the same path Domio's room uses). "0"/unknown → no folder (root).
            "suggestedRoom": self._folder_name_for(valuesDict.get("folder")),
        })
        self.logger.info("manual commission → %s %s", status, body)
        return (status in (202, 409), valuesDict)

    def getDeviceFolders(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the folder picker on the manual-commission menu.

        Options are (folderId, folderName) with a leading "0" → no folder (the
        device-list root). The id MUST be a non-empty string — Indigo rejects an
        empty list id with "UI dynamic list function returned illegal ID string",
        which silently drops the option — so the no-folder sentinel is "0" (folder
        id 0 == no folder), never "". menuCommissionDeviceManually maps the chosen
        id back to the folder NAME for suggestedRoom."""
        options = [("0", "(no folder)")]
        try:
            for folder in indigo.devices.folders:
                options.append((str(folder.id), folder.name))
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to no-folder only
            self.logger.exception(exc)
        return options

    def _folder_name_for(self, folder_id):
        """Resolve the folder picker's selected id (string) to the folder NAME.

        "0", empty, or an unknown/stale id → None (commission to the device-list
        root). Never raises — an unresolvable folder must not fail the commission."""
        if not folder_id or folder_id == "0":
            return None
        try:
            fid = int(folder_id)
            for folder in indigo.devices.folders:
                if folder.id == fid:
                    return folder.name
            # Parses fine but matches nothing — e.g. folder deleted between the
            # picker rendering and submit. Benign (device lands at root), but leave
            # a trail rather than silently dropping the selection.
            self.logger.debug("folder id %r not found, commissioning at root", folder_id)
        except Exception as exc:  # noqa: BLE001 - degrade to no folder, never fail the commission
            self.logger.warning("folder id %r not resolvable, commissioning without a folder: %s", folder_id, exc)
        return None

    def getMatterNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the decommission picker (one entry per node)."""
        if self.device_sync is None:
            return []
        try:
            options = []
            for node_id, names in self.device_sync.list_nodes():
                label = ", ".join(names) if names else "(no Indigo devices)"
                options.append((str(node_id), f"{label} — node {node_id_to_str(node_id)}"))
            return options
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to an empty picker
            self.logger.exception(exc)
            return []

    def menuDecommissionDevice(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Menu callback: decommission the selected node.

        Returns ``(False, valuesDict, errors)`` to keep the dialog open with a
        field error, ``(True, valuesDict)`` only when the node was fully
        removed (fabric AND Indigo devices) — partial outcomes are dialog
        errors so they can't masquerade as success.
        """
        errors = indigo.Dict()
        selected = valuesDict.get("node", "")
        if not selected:
            errors["node"] = "Select a device to decommission."
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm removal from Indigo."
            return (False, valuesDict, errors)
        try:
            node_id = int(selected)
        except (TypeError, ValueError):
            errors["node"] = "Invalid selection."
            return (False, valuesDict, errors)
        try:
            result = self._decommission_sync(node_id)
        except MatterUnavailable as exc:
            self.logger.error("decommission %s failed — matter-server unavailable: %s",
                              node_id_to_str(node_id), exc)
            # A timeout does NOT cancel the in-flight coroutine — the removal may
            # still complete in the background, so don't claim nothing happened.
            errors["node"] = ("matter-server did not respond — see the log. The removal may "
                              "still complete in the background; check the device before retrying.")
            return (False, valuesDict, errors)
        except (Exception, FuturesCancelledError) as exc:  # CancelledError is BaseException on 3.10+
            self.logger.error("decommission %s failed: %s", node_id_to_str(node_id), exc)
            self.logger.exception(exc)
            errors["node"] = "Decommission failed — see the Indigo event log."
            return (False, valuesDict, errors)
        if result is None:
            errors["node"] = "Unknown node — nothing was removed."
            return (False, valuesDict, errors)
        if result["fabricRemoved"]:
            self.logger.info(
                "Decommissioned Matter node %s: fabric removed, Indigo device(s) deleted: %s",
                result["nodeId"], result["removedIndigoDeviceIds"] or "none",
            )
            return (True, valuesDict)
        # remove_node failed (usually: device offline) — matter-server most likely
        # still has the node (any remove_node failure is treated as not-removed),
        # so the next reconcile (plugin restart or matter-server reconnect) will
        # recreate the Indigo devices we just deleted. Surface that in the dialog —
        # never report success when the underlying op only half-happened.
        self.logger.warning(
            "Decommission of node %s incomplete: Indigo device(s) %s deleted but the "
            "fabric removal failed (device offline?). The node is still commissioned in "
            "matter-server and its devices will reappear at the next reconcile — retry "
            "once the device is reachable.",
            result["nodeId"], result["removedIndigoDeviceIds"] or "none",
        )
        errors["node"] = ("Device unreachable — removed from Indigo, but it is still commissioned "
                          "in matter-server and will reappear at the next reconcile (plugin restart "
                          "or reconnect). Retry once the device is powered and reachable.")
        return (False, valuesDict, errors)

    # ------------------------------------------------------------------
    # Matter export allow-list — the "Manage Matter Exports…" dialog
    # (PRD-indigo-matter-export §5.1 UI-D; roles per BRIDGE_PROTOCOL §4.2)
    # ------------------------------------------------------------------
    def _export_plugin_id(self) -> str:
        """This plugin's id, for the loop guard (XNG3/XAC6).

        Read from the running plugin rather than hardcoded, so the guard can
        never drift from the bundle it is protecting; the catalog constant is
        only the fallback for a plugin object built without one (tests).
        """
        return getattr(self, "pluginId", "") or export_catalog.DEFAULT_PLUGIN_ID

    @staticmethod
    def _truthy(value) -> bool:
        """Indigo checkboxes arrive as bools or as "true"/"false" strings."""
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)

    @staticmethod
    def _indigo_device(device_id):
        """``indigo.devices[device_id]`` or None — a stale id is never fatal."""
        try:
            return indigo.devices[int(device_id)]
        except Exception:  # pylint: disable=broad-except  # KeyError/ValueError/Indigo's own
            return None

    def _export_selection(self, values_dict) -> tuple[str, int]:
        """Decode the picker value into ``(kind, device_id)``.

        ``kind`` is ``"none"`` (nothing chosen, or one of the informational
        rows — the "select a device" seed, the truncation tail, the no-match
        note), ``"excluded"`` (an ``x-`` row the user may see but not pick), or
        ``"device"``.
        """
        raw = str((values_dict or {}).get("exportDevice", "") or "")
        if not raw or raw == NO_SELECTION_ID or raw in (TRUNCATED_OPTION[0], NO_MATCH_OPTION[0]):
            return ("none", 0)
        excluded = raw.startswith(EXCLUDED_OPTION_PREFIX)
        if excluded:
            raw = raw[len(EXCLUDED_OPTION_PREFIX):]
        try:
            device_id = int(raw)
        except (TypeError, ValueError):
            return ("none", 0)
        return ("excluded" if excluded else "device", device_id)

    def _save_plugin_prefs(self) -> None:
        """Flush pluginPrefs to Indigo's database (the store's commit step)."""
        indigo.server.savePluginPrefs()

    def _reject_unexportable_entry(self, entry) -> str | None:
        """Validator for entries restored from prefs — the loop guard, re-run.

        Load is the one write path the dialog's guards never see: a blob
        restored from a backup, or hand-edited in the ``.indiPref``, can name a
        device this plugin created. Only the loop guard is enforced here.
        Ordinary ineligibility is *reported* by the startup reconcile and left
        alone, because a device can be temporarily odd (a plugin still
        starting) and silently deleting the user's export would be worse than
        an accessory that fails to build.
        """
        dev = self._indigo_device(entry.indigo_device_id)
        if dev is None:
            return None
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if isinstance(verdict, export_catalog.Excluded) \
                and verdict.reason == export_catalog.REASON_LOOP_GUARD:
            return export_catalog.REASON_LOOP_GUARD
        return None

    def _reconcile_exports(self) -> None:
        """Report-only startup sweep of the allow-list (never edits it).

        An export whose device has been deleted, or which no longer classifies
        as exportable, is a real problem the user should hear about at startup
        rather than discovering as a missing accessory. It is NOT auto-removed:
        the allow-list is the user's declaration, and E3 re-classifies at
        endpoint-build time anyway.
        """
        if self.exports is None:
            return
        try:
            plugin_id = self._export_plugin_id()
            for entry in self.exports.all():
                dev = self._indigo_device(entry.indigo_device_id)
                if dev is None:
                    self.logger.warning(
                        "Matter export allow-list: device %s is exported as %s but no longer "
                        "exists in Indigo — it will not be bridged. Remove it in "
                        "'Manage Matter Exports…'.",
                        entry.indigo_device_id, entry.role)
                    continue
                verdict = export_catalog.classify(dev, plugin_id)
                if isinstance(verdict, export_catalog.Excluded):
                    self.logger.warning(
                        "Matter export allow-list: %s (id %s) is exported as %s but is no longer "
                        "exportable: %s. It will not be bridged.",
                        getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                        verdict.reason)
                elif entry.role not in verdict.eligible_roles:
                    self.logger.warning(
                        "Matter export allow-list: %s (id %s) is exported as %s, which this "
                        "device no longer offers (%s). Re-pick its role in "
                        "'Manage Matter Exports…'.",
                        getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                        ", ".join(verdict.eligible_roles))
        except Exception as exc:  # pylint: disable=broad-except
            # A diagnostic sweep must never be the thing that fails startup.
            self.logger.exception(exc)

    def _export_summary(self) -> str:
        if self.exports is None:
            return "Plugin still starting — reopen this dialog in a moment."
        count = len(self.exports)
        # A load failure has to lead. Reporting "Nothing is exported yet." over
        # a blob we could not read invites the user to rebuild the list from
        # scratch, and the rebuild's first save overwrites the rescue copy.
        error = self.exports.load_error
        if error:
            return error if not count else f"{error} {count} device(s) exported."
        if not count:
            return "Nothing is exported yet."
        return f"{count} device(s) exported."

    def get_menu_action_config_ui_values(self, menu_id):
        """Seed the export dialog (menu dialogs never remember their values).

        Only the export menu is seeded — this callback fires for EVERY menu
        item that has a ConfigUI, and returning values for another one would
        overwrite its defaults.
        """
        values = indigo.Dict()
        if menu_id != MENU_MANAGE_EXPORTS:
            return values
        values["exportFilter"] = ""
        values["exportDevice"] = NO_SELECTION_ID
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportStatus"] = self._export_summary()
        return values

    def _log_row_failure(self, exc, first: bool) -> None:
        """Log one unreadable picker row — stack for the first, one line after.

        A database with fifty broken proxies must not write fifty tracebacks
        into the event log, but the first one has to carry enough to debug.
        """
        if first:
            self.logger.exception(exc)
        else:
            self.logger.error("Matter export: another device could not be read — %s", exc)

    @staticmethod
    def _candidate_row(dev, name: str, plugin_id: str, exported) -> tuple[str, str]:
        """One picker row for ``dev``. May raise — the caller contains it."""
        device_id = dev.id
        # An excluded device that IS exported keeps its marker: the pair
        # "excluded" + "exported" is exactly the state the user has to know
        # about, and hiding half of it reads as a picker bug rather than the
        # stale export it actually is.
        mark = "● " if device_id in exported else ""
        verdict = export_catalog.classify(dev, plugin_id)
        if isinstance(verdict, export_catalog.Excluded):
            return (f"{EXCLUDED_OPTION_PREFIX}{device_id}",
                    f"{mark}{name} — not exportable: {verdict.reason}")
        return (str(device_id), f"{mark}{name}")

    def getExportCandidates(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Picker rows: every Indigo device, exportable or not (XAC9).

        Excluded devices are listed **with the reason in the label** and an
        ``x-``-prefixed id so the callbacks can reject the pick cleanly —
        hiding them would leave a user hunting for a device that will never
        appear. ``filter`` here is the XML's static filter attribute, NOT the
        user's text: textfields have no callbacks, so the typed filter arrives
        in ``valuesDict`` and the Apply-filter button drives the reload.

        One device that cannot be read costs one row, not the whole list: the
        try/except is INSIDE the loop, because the alternative is a dialog that
        renders empty the moment any device in the database misbehaves.
        """
        try:
            text = str((valuesDict or {}).get("exportFilter", "") or "").strip().lower()
            exported = self.exports.ids() if self.exports is not None else frozenset()
            plugin_id = self._export_plugin_id()
            # Always a real row for the seeded value, and always first.
            options: list[tuple[str, str]] = [(NO_SELECTION_ID, NO_SELECTION_LABEL)]
            matched = 0
            truncated = 0
            failures = 0
            for dev in indigo.devices:
                try:
                    name = str(getattr(dev, "name", "") or "")
                    if text and text not in name.lower():
                        continue
                    matched += 1
                    if matched > EXPORT_PICKER_LIMIT:
                        truncated += 1
                        continue
                    options.append(self._candidate_row(dev, name, plugin_id, exported))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)
                    failures += 1
                    # Position-keyed id: the device's own id is one of the
                    # things we could not read.
                    options.append((f"{EXCLUDED_OPTION_PREFIX}err{len(options)}",
                                    f"— {ROW_ERROR_LABEL}"))
            if truncated:
                options.append(TRUNCATED_OPTION)
            if len(options) == 1:
                options.append(NO_MATCH_OPTION)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def getExportRoles(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Roles the picked device may legitimately be exported as (§5.2).

        Empty for no selection or an excluded pick — an empty role menu is the
        honest rendering of "there is nothing you may choose here". An outright
        failure is NOT empty: it says so, so the user does not read a broken
        callback as "this device offers no roles".
        """
        try:
            kind, device_id = self._export_selection(valuesDict)
            if kind != "device":
                return []
            dev = self._indigo_device(device_id)
            if dev is None:
                return []
            verdict = export_catalog.classify(dev, self._export_plugin_id())
            if isinstance(verdict, export_catalog.Excluded):
                return []
            options = []
            for role in verdict.eligible_roles:
                try:
                    options.append((role, export_catalog.role_label(role)))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not options)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def getCurrentExports(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Read-only summary of the allow-list (one row per export)."""
        try:
            if self.exports is None:
                return [(NO_SELECTION_ID, "(plugin still starting)")]
            options = []
            failures = 0
            for entry in self.exports.all():
                try:
                    dev = self._indigo_device(entry.indigo_device_id)
                    name = str(getattr(dev, "name", "") or "") if dev is not None else ""
                    if not name:
                        name = f"(deleted device {entry.indigo_device_id})"
                    label = f"{name} → {export_catalog.role_label(entry.role)}"
                    if entry.name_override:
                        label += f' · shown as "{entry.name_override}"'
                    if entry.options.get(OPTION_INVERT):
                        label += " · inverted"
                    options.append((str(entry.indigo_device_id), label))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)
                    failures += 1
                    options.append((f"{EXCLUDED_OPTION_PREFIX}err{len(options)}",
                                    f"— {ROW_ERROR_LABEL}"))
            return options or [(NO_SELECTION_ID, "(nothing exported yet)")]
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def _exported_warning(self, device_id: int) -> str:
        """Suffix warning shown when an EXCLUDED device is nonetheless exported.

        This is the incoherent state worth naming out loud: the allow-list says
        export it, the catalog says it cannot be. Left alone it becomes an
        accessory that never appears, with no visible cause.
        """
        if self.exports is not None and device_id in self.exports:
            return " — but this device IS currently exported — remove it or it will fail to bridge"
        return ""

    def exportReloadPicker(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Apply-filter button: the return trip is what reloads the lists."""
        values = valuesDict
        text = str(values.get("exportFilter", "") or "").strip()
        values["exportStatus"] = (f'Filtered on "{text}". {self._export_summary()}' if text
                                  else self._export_summary())
        return values

    def exportDeviceChanged(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Picker selection changed: load that device's saved export, or defaults.

        Menu callbacks return a valuesDict, not an error dict (the SDK's menu
        contract), so an excluded pick is reported in the read-only status
        field here — and refused again by the Add/update button below. Both
        paths are covered by tests.
        """
        values = valuesDict
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = self._export_summary()
            return values
        dev = self._indigo_device(device_id)
        if kind == "excluded" or dev is None:
            reason = "that device no longer exists"
            if dev is not None:
                verdict = export_catalog.classify(dev, self._export_plugin_id())
                reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                    else "that device is not exportable"
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = (f"Not exportable: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if isinstance(verdict, export_catalog.Excluded):
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = (f"Not exportable: {verdict.reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        entry = self.exports.get(device_id) if self.exports is not None else None
        if entry is not None:
            values["exportRole"] = entry.role
            values["exportName"] = entry.name_override or ""
            values["exportInvert"] = bool(entry.options.get(OPTION_INVERT, False))
            values["exportStatus"] = f"{dev.name} is exported as {export_catalog.role_label(entry.role)}."
        else:
            values["exportRole"] = verdict.default_role
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = f"{dev.name} is not exported yet."
        return values

    def exportAddOrUpdate(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Add or update one export. Validates the role against the catalog.

        A role the catalog does not offer for this device is refused here
        rather than by the bridge node, which would only reject it with
        ``unknown_role``/``role_change`` long after the user could connect the
        failure to what they did (BRIDGE_PROTOCOL §1.1).

        Returns **the values dict only**. A ``(valuesDict, errorsDict)`` tuple
        is the documented contract for *validation* methods, not for button
        ``CallbackMethod``s — the SDK's button reference says a button callback
        returns a dictionary of field changes, and the field carrying a button's
        outcome is read-only, so it cannot hold an error message anyway. Every
        refusal therefore lands in ``exportStatus``, which is what the dialog
        actually shows.
        """
        values = valuesDict
        if self.exports is None:
            values["exportStatus"] = "Plugin still starting — try again in a moment."
            return values
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportStatus"] = "Select a device to export."
            return values
        dev = self._indigo_device(device_id)
        if dev is None:
            values["exportStatus"] = "That device no longer exists — refresh the list."
            return values
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if kind == "excluded" or isinstance(verdict, export_catalog.Excluded):
            reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                else "not exportable"
            values["exportStatus"] = (f"{dev.name} cannot be exported: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        role = str(values.get("exportRole", "") or "")
        if role not in verdict.eligible_roles:
            values["exportStatus"] = ("Choose how this device should appear "
                                      f"({', '.join(verdict.eligible_roles)}).")
            return values
        name_override = str(values.get("exportName", "") or "").strip() or None
        options = {}
        if role == export_catalog.ROLE_WINDOW_COVERING and self._truthy(values.get("exportInvert")):
            options[OPTION_INVERT] = True
        existed = device_id in self.exports
        try:
            self.exports.upsert(ExportEntry(
                indigo_device_id=device_id, role=role,
                name_override=name_override, options=options,
            ))
        except Exception as exc:  # pylint: disable=broad-except
            # The store rolled back, so nothing was saved — say so rather than
            # reporting the success the old code reported unconditionally.
            self.logger.error("Matter export: saving the export list FAILED — %s", exc)
            self.logger.exception(exc)
            values["exportStatus"] = "FAILED to save the export list — see Event Log"
            return values
        verb = "Updated" if existed else "Added"
        self.logger.info("%s Matter export: %s (id %s) as %s%s",
                         verb, dev.name, device_id, role,
                         f' named "{name_override}"' if name_override else "")
        values["exportStatus"] = f"{verb} {dev.name} as {export_catalog.role_label(role)}. " \
                                 f"{self._export_summary()}"
        return values

    def exportRemove(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Drop the picked device from the allow-list. Returns values only (see above)."""
        values = valuesDict
        if self.exports is None:
            values["exportStatus"] = "Plugin still starting — try again in a moment."
            return values
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportStatus"] = "Select a device to remove from the export list."
            return values
        try:
            removed = self.exports.remove(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error("Matter export: saving the export list FAILED — %s", exc)
            self.logger.exception(exc)
            values["exportStatus"] = "FAILED to save the export list — see Event Log"
            return values
        if not removed:
            values["exportStatus"] = "That device is not exported."
            return values
        dev = self._indigo_device(device_id)
        name = str(getattr(dev, "name", "") or "") if dev is not None else f"device {device_id}"
        self.logger.info("Removed Matter export: %s (id %s)", name, device_id)
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportStatus"] = f"Removed {name}. {self._export_summary()}"
        return values

    def _resolve_storage_path(self) -> str:
        """Storage dir path in BOTH managed and manual modes.

        In managed mode ``self.server_process`` already knows it. In manual mode
        we construct a throwaway ``ServerProcess`` purely to read ``storage_path``
        — its ``__init__`` writes no plist and runs no launchctl, so this is a
        side-effect-free path lookup.
        """
        if self.server_process is not None:
            return self.server_process.storage_path
        return ServerProcess(self._server_prefs(), self.logger).storage_path

    @staticmethod
    def _human_size(num_bytes: int) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    def menuExportFabricBackup(self):  # noqa: N802
        # menuItem has no ConfigUI/valuesDict, so outcome can only surface via the
        # log — make both success and failure unmistakable there. create_backup
        # already prunes (no duplicate prune here) and validates its own output.
        storage_path = None
        try:
            storage_path = self._resolve_storage_path()
            archive = fabric_backup.create_backup(
                storage_path, now=datetime.now(timezone.utc), logger=self.logger,
            )
            size = self._human_size(os.path.getsize(archive))
            self.logger.info(
                "Fabric backup complete: %s (%s). This is a best-effort live snapshot — "
                "matter-server was NOT stopped. Backups live in %s.",
                archive, size, fabric_backup.backups_dir_for(storage_path),
            )
        except FileNotFoundError as exc:
            # storage dir missing or empty — there is no fabric to back up.
            self.logger.error(
                "Fabric backup FAILED — no fabric to back up, nothing was written: %s", exc,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Fabric backup FAILED — nothing was written: %s", exc)
            self.logger.exception(exc)

    def getFabricBackups(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002
        """List-callback populating the restore picker (newest first)."""
        try:
            storage_path = self._resolve_storage_path()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
            return []
        options = []
        for entry in fabric_backup.list_backups(storage_path):
            when = datetime.fromtimestamp(entry["mtime"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            label = f"{entry['filename']} — {self._human_size(entry['size_bytes'])} — {when}"
            options.append((entry["path"], label))
        return options

    def menuRestoreFabricBackup(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        errors = indigo.Dict()
        # Restore must stop/start matter-server, which the plugin can only do in
        # managed mode. Externally-managed (run.sh / manual) servers must be
        # stopped by the user by hand.
        if self.server_process is None:
            msg = ("LaunchAgent management is off — the plugin cannot stop matter-server. "
                   "Stop matter-server yourself, unzip the chosen backup over the storage dir, "
                   "then restart it. Refusing to restore automatically.")
            self.logger.warning(msg)
            errors["backup"] = "Turn on 'Manage LaunchAgent', or restore by hand (see log)."
            return (False, valuesDict, errors)

        selected = valuesDict.get("backup", "")
        if not selected:
            errors["backup"] = "Select a backup to restore."
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm — restore replaces the current fabric."
            return (False, valuesDict, errors)

        try:
            storage_path = self._resolve_storage_path()
            result = fabric_backup.restore_backup(
                selected, storage_path, self.server_process,
                now=datetime.now(timezone.utc), logger=self.logger,
            )
            # restore_backup only returns on success: the server was stopped, the
            # fabric was swapped, the restored dir is non-empty, and start()
            # returned True. Be honest — matter-server is RESTARTING, the node
            # count is not yet known; point the user at the real signal instead of
            # logging a likely-stale count and pretending it is confirmation.
            self.logger.info(
                "Fabric restored from %s; previous fabric preserved at %s. matter-server is "
                "restarting — watch the log for 'reconciled N node(s)' to confirm the devices "
                "came back.",
                result["restored_from"], result["moved_aside_to"],
            )
            return (True, valuesDict)
        except Exception as exc:  # noqa: BLE001
            # restore_backup rolled back and preserved the original fabric (or
            # aborted before touching it). Surface the failure in the UI dialog —
            # never report success when the underlying op failed.
            self.logger.error("Fabric restore FAILED: %s", exc)
            self.logger.exception(exc)
            errors["backup"] = "Restore failed — see the log. Your existing fabric was preserved."
            return (False, valuesDict, errors)
