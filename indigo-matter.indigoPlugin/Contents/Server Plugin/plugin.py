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
import time
from concurrent.futures import CancelledError as FuturesCancelledError
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone

import indigo  # provided by the Indigo runtime

import fabric_backup
from async_runtime import AsyncRuntime
from commission_jobs import CommissionJobs, node_id_to_str
from device_sync import DeviceSync
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def startup(self) -> None:
        self.debug = bool(self.pluginPrefs.get("verboseLogging", False))
        prefs = dict(self.pluginPrefs)

        self.runtime = AsyncRuntime(self.logger)
        self.runtime.start()

        if prefs.get("manageLaunchAgent", False):
            try:
                self.server_process = ServerProcess(prefs, self.logger)
                self.server_process.ensure_installed()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception(exc)

        self.matter = MatterClient(
            self.proto, self.logger, prefs,
            on_event=self._on_matter_event,
            on_connect=self._resync,
            on_disconnect=self._on_disconnected,
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
    def closedPrefsConfigUi(self, valuesDict, userCancelled):  # noqa: N802
        if userCancelled:
            return
        self.debug = bool(valuesDict.get("verboseLogging", False))

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
        fabric_removed = True
        try:
            await self.matter.remove_node(node_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("remove_node failed (device may be offline): %s", exc)
            fabric_removed = False
        removed_ids = self.device_sync.delete_node(node_id)
        if not removed_ids and fabric_removed is False:
            return None  # unknown node and unreachable → 404
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
    def menuRestartMatterServer(self):  # noqa: N802
        if self.server_process is None:
            self.logger.warning("LaunchAgent management is off; start matter-server manually")
            return
        if self.server_process.restart():
            self.logger.info("matter-server restart requested")
        else:
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

    def _resolve_storage_path(self) -> str:
        """Storage dir path in BOTH managed and manual modes.

        In managed mode ``self.server_process`` already knows it. In manual mode
        we construct a throwaway ``ServerProcess`` purely to read ``storage_path``
        — its ``__init__`` writes no plist and runs no launchctl, so this is a
        side-effect-free path lookup.
        """
        if self.server_process is not None:
            return self.server_process.storage_path
        return ServerProcess(dict(self.pluginPrefs), self.logger).storage_path

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
