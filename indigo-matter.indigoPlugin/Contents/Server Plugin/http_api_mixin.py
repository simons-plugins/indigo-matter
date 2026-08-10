"""The Domio HTTP API (IWS hidden-action handlers — API.md v1.1) and the pairing
page (PRD §6). Both are IWS ``<Action uiPath="hidden">`` surfaces reached over
the Reflector, and both share ``_parse_request``/``_reply``, so they live in one
mixin rather than being split further. See issue #146.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

import indigo  # provided by the Indigo runtime

from commission_jobs import node_id_to_str
from http_handlers import MatterUnavailable
from pairing_page import _pairing_html
from plugin_constants import DECOMMISSION_TIMEOUT, PAIRING_READ_TIMEOUT


class HttpApiMixin:
    """IWS HTTP handlers, their shared request/reply helpers, and the QR pairing page.

    Composed into ``Plugin`` alongside the other three mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146.
    runtime: Any
    matter: Any
    http: Any
    device_sync: Any
    export_bridge: Any
    logger: Any
    _version: str
    _start_ts: float

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
    # The QR page (IWS hidden action — PRD §6 "display mechanism")
    # ------------------------------------------------------------------
    def http_pairing(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802, ARG002
        """Serve the pairing page. GET only; authenticated by IWS before we run.

        This is the *only* handler here that returns HTML rather than JSON, and
        it exists because the one thing the event log cannot carry is a QR code.
        """
        method, _path_args, _query = self._parse_request(action)
        if method.upper() != "GET":
            return self._reply(405, {"error": "method_not_allowed"})
        reply = indigo.Dict()
        reply["status"] = 200
        reply["headers"] = indigo.Dict({"Content-Type": "text/html; charset=utf-8"})
        reply["content"] = self._pairing_page()
        return reply

    def _pairing_page(self) -> str:
        """Build the pairing page's HTML from a live ``get_pairing``.

        **No QR is generated here, and that is a deliberate choice.** Rendering
        one needs either a Python dependency (Indigo's framework Python has no
        image stack and this plugin ships none) or a hand-written JS encoder —
        a few hundred lines of Reed-Solomon and bit-masking whose failure mode is
        a plausible-looking square that no phone can read. Neither is worth it
        for a code that Apple Home, Alexa and Google all accept *typed in*: the
        page therefore shows the manual code at a size you can read across a
        room, the raw ``MT:`` payload for copying, and a link to the CHIP
        project's own QR viewer for anyone who wants to scan.
        """
        client = self.export_bridge.client if self.export_bridge is not None else None
        if client is None or not client.connected:
            return _pairing_html(None, "The plugin is not connected to the Matter bridge node. "
                                       "Export at least one device, then reload this page.")
        try:
            pairing = self.runtime.submit(client.get_pairing()).result(timeout=PAIRING_READ_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
            return _pairing_html(None, f"Could not read the bridge node's pairing state: {exc}")
        if not pairing.manual_pairing_code:
            return _pairing_html(
                pairing,
                "No pairing window is open, so there is no code to show. Open one with "
                "Plugins ▸ Matter ▸ Pair Matter Bridge… in Indigo.")
        return _pairing_html(pairing, "")
