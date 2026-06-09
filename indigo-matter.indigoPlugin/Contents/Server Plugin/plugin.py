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
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError

import indigo  # provided by the Indigo runtime

from async_runtime import AsyncRuntime
from commission_jobs import CommissionJobs, node_id_to_str
from device_sync import DeviceSync
from http_handlers import HttpApi, MatterUnavailable
from matter_client import MatterClient
from matter_handlers.registry import HandlerRegistry
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

        self.matter = MatterClient(self.proto, self.logger, prefs, on_event=self._on_matter_event)
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

        self.runtime.submit(self.matter.run())
        self.runtime.submit(self._initial_sync())
        self.logger.info("%s started", PLUGIN_NAME)

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

    async def _initial_sync(self) -> None:
        """After the WS connects, reconcile matter nodes ↔ Indigo devices."""
        try:
            await self.matter.wait_connected(timeout=30)
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
            self.logger.warning("initial sync incomplete: %s", exc)

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
        elif self.matter is not None and not self.matter.connected:
            self.logger.debug("matter-server not currently connected")

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
    def deviceStartComm(self, dev):  # noqa: N802
        self.device_sync.note_device(dev)
        self.device_sync.set_active(dev.id, True)

    def deviceStopComm(self, dev):  # noqa: N802
        self.device_sync.set_active(dev.id, False)

    # ------------------------------------------------------------------
    # Device actions → Matter commands (bridged onto the loop, 5s ack)
    # ------------------------------------------------------------------
    def actionControlDevice(self, action, dev):  # noqa: N802
        command = self.device_sync.build_command(dev, action)
        if command is None:
            return
        self._send_matter_command(command, dev)

    def actionControlThermostat(self, action, dev):  # noqa: N802
        command = self.device_sync.build_command(dev, action)
        if command is None:
            return
        self._send_matter_command(command, dev)

    def actionControlSensor(self, action, dev):  # noqa: N802
        self.logger.info('ignored "%s" — Matter sensor is read-only', dev.name)

    def _send_matter_command(self, action, dev) -> None:
        if self.runtime is None or self.matter is None:
            return
        # An action is either a cluster-command invoke or an attribute write.
        coro = self.matter.write(action) if isinstance(action, MatterWrite) else self.matter.send_command(action)
        try:
            self.runtime.submit(coro).result(timeout=COMMAND_TIMEOUT)
            if getattr(dev, "errorState", ""):
                dev.setErrorStateOnServer("")  # command succeeded — clear a stale error
        except FuturesTimeoutError:
            self.logger.error("Matter command to %s timed out", dev.name)
            dev.setErrorStateOnServer("timeout")
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Matter command to %s failed: %s", dev.name, exc)
            dev.setErrorStateOnServer("cmd failed")

    # ------------------------------------------------------------------
    # asyncio → Indigo event bridge (called on the loop thread)
    # ------------------------------------------------------------------
    def _on_matter_event(self, evt) -> None:
        self.device_sync.handle_event(evt)

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
        return {
            "ready": connected,
            "controllerVersion": self._version,
            "matterServerReachable": connected,
            "matterServerVersion": str(info.get("sdk_version", "unknown")),
            "fabricId": str(info.get("fabric_id", "")),
            "nodeCount": self.device_sync.node_count(),
            "bleAvailable": bool(info.get("bluetooth_enabled", False)),
            "uptime": int(time.monotonic() - self._start_ts),
        }

    def _decommission_sync(self, node_id):
        # None → genuine unknown node (404). MatterUnavailable → 503. Other → 500.
        if self.runtime is None:
            raise MatterUnavailable("plugin not ready")
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
        if self.server_process is not None:
            self.server_process.restart()
            self.logger.info("matter-server restart requested")
        else:
            self.logger.warning("LaunchAgent management is off; start matter-server manually")

    def menuShowMatterServerLogs(self):  # noqa: N802
        self.logger.info("matter-server log: ~/Library/Logs/indigo-matter/matter-server.log")

    def menuCommissionDeviceManually(self, valuesDict):  # noqa: N802
        if self.jobs is None:
            return (False, valuesDict)
        status, body = self.jobs.create_job({
            "setupCode": valuesDict.get("setupCode", ""),
            "suggestedName": valuesDict.get("suggestedName", "Matter Device"),
        })
        self.logger.info("manual commission → %s %s", status, body)
        return (status in (202, 409), valuesDict)

    def menuExportFabricBackup(self):  # noqa: N802
        self.logger.info("Fabric backup: copy ~/Library/Application Support/com.simon.indigo-matter/")
