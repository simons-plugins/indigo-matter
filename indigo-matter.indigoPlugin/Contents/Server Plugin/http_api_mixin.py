"""The Domio HTTP API (IWS hidden-action handlers — API.md v1.1) and the pairing
page (PRD §6). Both are IWS ``<Action uiPath="hidden">`` surfaces reached over
the Reflector, and both share ``_parse_request``/``_reply``, so they live in one
mixin rather than being split further. See issue #146.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any

import indigo  # provided by the Indigo runtime

import thread_mesh
import thread_survey
from commission_jobs import node_id_to_str
from http_handlers import MatterUnavailable
from matter_client import ATTRIBUTE_TIMEOUT
from pairing_page import _pairing_html
from plugin_constants import DECOMMISSION_TIMEOUT, PAIRING_READ_TIMEOUT, SURVEY_READ_TIMEOUT
from protocol import is_node_not_exists
from thread_page import render_thread_page

#: #334 finding B5.5 set this to 12 s (below the menu's ATTRIBUTE_TIMEOUT,
#: 30 s) on 2.7-6.9 s observed single reads, reasoning the synchronous IWS
#: handler shouldn't repeat the menu's 30 s budget with a browser tab sitting
#: on it. Field correction (#334, 2026-09-01): a real ``?live=1`` refresh hit
#: 12-s timeouts on 3 of 4 sleepy nodes — a sleepy end device answers a read
#: only at its next poll, which can approach a minute (this rig's Aqara
#: FP300 subscribes at 67 s), so 12 s structurally misses a napping device.
#: Now matches ATTRIBUTE_TIMEOUT (30 s), the menu's own budget, which covers
#: a typical poll; a node napping longer still degrades honestly into the
#: page's "Unreadable / stale" section rather than blocking forever. Reads
#: run concurrently (``_live_refresh`` via ``asyncio.gather``, thread_survey.py),
#: so the worst case stays ~30 s + SURVEY_READ_TIMEOUT for the whole page, not
#: summed per node. The menu itself keeps ATTRIBUTE_TIMEOUT (a background
#: dialog result, not a page a person is staring at). No result cache: every
#: request re-surveys, live or cached.
_PAGE_LIVE_PER_NODE_TIMEOUT = 30.0


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
        # Distinct from fabric_removed: "we sent a RemoveFabric and it worked" and
        # "there was nothing there to remove" are both fine outcomes for the caller,
        # but only the FORMER is evidence the node ever existed. Without this the 404
        # below became unreachable and any nonsense node id answered 200 (API.md §3.3).
        fabric_already_absent = False
        try:
            await self.matter.remove_node(node_id)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: deliberately every error type, so is_node_not_exists can
            # inspect it — the discrimination happens INSIDE, not in the except
            # clause. 'Already absent' completes the decommission (#111/#182);
            # anything else degrades to fabricRemoved:false with the node kept for
            # retry. An escape would abandon the Indigo-side bookkeeping this
            # endpoint owes either way.
            if is_node_not_exists(exc):
                fabric_already_absent = True
                # matter-server has no such node, so there is no fabric entry left to
                # remove and the decommission's goal is ALREADY MET. Treating this as a
                # failure was a trap: the node stayed in the picker, every retry failed
                # identically, and the user was told to "retry once the device is
                # reachable" about a device that could never make the message come true.
                # Reachable in normal use whenever matter-server's node list and the
                # plugin's index disagree — a decommission from another admin, a fabric
                # restored from an older backup, or the duplicate-server cutover in #182.
                self.logger.info(
                    "node %s is not in matter-server's fabric — nothing to remove there; "
                    "completing the Indigo-side cleanup.", node_id_to_str(node_id),
                )
            else:
                self.logger.warning("remove_node failed (device may be offline): %s", exc)
                fabric_removed = False
        # Only forget the node if it actually left the fabric; otherwise it must
        # stay listed so the user can retry.
        removed_ids = self.device_sync.delete_node(node_id, forget=fabric_removed)
        if not removed_ids and not known and (fabric_already_absent or not fabric_removed):
            # Never heard of it, no devices to show for it, and nothing was actually
            # removed from the fabric → 404. A node we DID remove for real still
            # answers 200 even with no Indigo devices, which is why this cannot
            # simply test `not fabric_removed`.
            return None
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
        except (ConnectionError, TimeoutError) as exc:
            # 503 matter_server_unreachable — and ONLY these two (issue #310).
            # Both mean the server did not answer: the socket is down, or the
            # request timed out. API.md tells the client to prompt the user to
            # retry on a 503, and for these that is honest advice.
            #
            # Everything else — a ProtocolError, a bug in here — is now left to
            # propagate into the 500 the handler already has. It used to be
            # caught and reported as "unreachable" too, which sent the client
            # away to retry against a server that was up and answering, over a
            # fault that would recur every time. `TimeoutError` is named
            # explicitly because it is a SIBLING of ConnectionError under
            # OSError, not a subclass — catching ConnectionError alone would
            # silently turn every timeout into a 500.
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
    # Shared: the IWS URL of one of this plugin's hidden-action pages (#339)
    # ------------------------------------------------------------------
    def _iws_page_url(self, action_id: str) -> str:
        """The IWS URL of a hidden-action page (an ``Actions.xml`` entry with
        ``uiPath="hidden"`` — e.g. ``"pairing"``, ``"thread"``).

        ``getWebServerURL`` picks the reflector, then the Bonjour name, then
        localhost — so this is reachable from the phone the user is holding
        whenever a reflector or a ``.local`` name exists, which is the case a
        page link is FOR. A failure falls back to the loopback default rather
        than omitting the line: a wrong-host URL a user can edit beats no URL.
        The RPC failure itself is DEBUG-absorbed because nothing downstream
        gates on this — it feeds a display-only URL, never a decision.

        Generalised (#339) from ``PairingMenuMixin._pairing_page_url``, which
        is now a one-line delegate to this method kept for its own
        callers/tests — this lives on ``HttpApiMixin`` because that is where
        every IWS page handler and its ``Actions.xml`` entry actually is.
        """
        base = "http://localhost:8176"
        try:
            base = str(indigo.server.getWebServerURL() or base)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any failure of the Indigo server RPC behind a display-only
            # URL. Safe because a wrong-host URL the user can edit beats no URL at
            # all — hence DEBUG.
            self.logger.debug("could not resolve the Indigo web server URL (%s)", exc)
        return f"{base}/message/{self._export_plugin_id()}/{action_id}/"  # pylint: disable=no-member  # ExportDialogMixin

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
            # Cache-Control: no-store — mirrors http_thread_page's fix below (#334,
            # 2026-09-01): this page carries a LIVE pairing code, an even stronger
            # case never to let a browser re-serve a cached authenticated GET.
            reply = indigo.Dict()
            reply["status"] = 405
            reply["headers"] = indigo.Dict({"Content-Type": "application/json", "Cache-Control": "no-store"})
            reply["content"] = json.dumps({"error": "method_not_allowed"})
            return reply
        reply = indigo.Dict()
        reply["status"] = 200
        reply["headers"] = indigo.Dict({"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
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
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any failure reading pairing state — timeout, dead runtime,
            # protocol error. Safe because this handler returns HTML to a human at a
            # browser: the failure becomes a readable error page carrying the reason,
            # which is the contract its test names — a failed get_pairing becomes a
            # page, not a 500.
            self.logger.exception(exc)
            return _pairing_html(None, f"Could not read the bridge node's pairing state: {exc}")
        if not pairing.manual_pairing_code:
            return _pairing_html(
                pairing,
                "No pairing window is open, so there is no code to show. Open one with "
                "Plugins ▸ Matter ▸ Pair Matter Bridge… in Indigo.")
        return _pairing_html(pairing, "")

    # ------------------------------------------------------------------
    # The read-only Thread mesh page (IWS hidden action — #334, ADR-0004)
    # ------------------------------------------------------------------
    def http_thread_page(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802, ARG002
        """Serve the Thread mesh page. GET only, mirroring ``http_pairing``.

        ``?live=1`` live-refreshes every sleepy (non-router) node first, via
        the exact same :func:`thread_survey.run_survey` the "Report Thread
        mesh…" menu diagnostic uses — the cache-vs-live policy lives in one
        place, not two drifting call sites. Always 200 for a GET: a
        matter-server failure renders as the page's own error banner
        (ADR-0004 has no write path to fall back to, and a broken read must
        never look like a real but empty mesh — root workspace CLAUDE.md
        degradation-path convention).

        This handler and :meth:`_thread_mesh_snapshot` are read-only by
        DISCIPLINE, not by a source-level guard: ADR-0004's own test
        (``test_the_diagnostics_never_write_to_a_device``) only ever covered
        ``diagnostics_menu_mixin.py`` and ``thread_survey.py``, and cannot be
        extended to this whole module — ``http_api_mixin.py`` legitimately
        holds the commission/decommission write paths (#334 post-review,
        B5.9). ``tests/test_thread_page.py`` pins this handler's OWN source
        instead, narrower than the module-wide guard would be.
        """
        method, _path_args, query = self._parse_request(action)
        if method.upper() != "GET":
            # Cache-Control: no-store — Safari re-served a cached ?live=1 render of
            # this page after a promotion, so a "Refresh (live)" click showed stale,
            # pre-promotion data (#334, 2026-09-01). Content-Type alone isn't enough.
            reply = indigo.Dict()
            reply["status"] = 405
            reply["headers"] = indigo.Dict({"Content-Type": "application/json", "Cache-Control": "no-store"})
            reply["content"] = json.dumps({"error": "method_not_allowed"})
            return reply
        live = str(query.get("live", "")) == "1"
        mesh, diags, error = self._thread_mesh_snapshot(live_sleepy=live)
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        content = render_thread_page(
            mesh, diags, generated_at=generated_at, live=live,
            plugin_id=self._export_plugin_id(),  # pylint: disable=no-member  # ExportDialogMixin
            error=error,
        )
        reply = indigo.Dict()
        reply["status"] = 200
        reply["headers"] = indigo.Dict({"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"})
        reply["content"] = content
        return reply

    def _thread_mesh_snapshot(self, *, live_sleepy: bool):  # pylint: disable=too-many-return-statements
        """``(mesh, diags, error)`` for the Thread page.

        Mirrors ``DiagnosticsMenuMixin.menuReportThreadMesh``'s failure
        handling (not connected; ``run_survey``/``get_nodes()`` failing
        outright; the three :class:`~thread_survey.Survey` shapes, #334
        finding B2.4) but reports an HTML banner instead of a dialog error.
        A per-node live-read timeout is NOT one of these — that degrades to
        ``NodeDiag.read_error`` inside ``thread_survey`` and is shown in the
        page's own "Unreadable" section, same as the menu report prints it
        from cache rather than failing.

        A ``?live=1`` request that times out OUTRIGHT (the whole survey, not
        one node) falls back to a cache-only survey instead of the bare
        banner (#334 finding B3.2) — the page still has real, useful data to
        show, with ``error`` naming the live timeout so nobody mistakes it
        for a genuinely fresh read. Only when the cache-only fallback ALSO
        fails does this reach the banner-only path.
        """
        if self.runtime is None or self.matter is None or not self.matter.connected:
            return thread_mesh.build_mesh([]), [], "The plugin is not connected to matter-server yet."

        def _survey(live: bool):
            return thread_survey.run_survey(
                self.runtime, self.matter, live_sleepy=live,
                per_node_timeout=_PAGE_LIVE_PER_NODE_TIMEOUT if live else ATTRIBUTE_TIMEOUT,
                node_names=self._thread_node_names(),
            )

        try:
            survey = _survey(live_sleepy)
        except FuturesTimeoutError:
            if not live_sleepy:
                return thread_mesh.build_mesh([]), [], "matter-server did not answer in time."
            self.logger.warning(
                "Matter: Thread mesh survey timed out after %.0f s (live=%s) — falling back to cache.",
                _PAGE_LIVE_PER_NODE_TIMEOUT + SURVEY_READ_TIMEOUT, live_sleepy)
            try:
                survey = _survey(False)
            except FuturesTimeoutError:
                return thread_mesh.build_mesh([]), [], "matter-server did not answer in time."
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.warning("Matter: could not read the Thread mesh (cache fallback): %s", exc)
                return thread_mesh.build_mesh([]), [], f"matter-server could not be read: {exc}"
            mesh = thread_mesh.build_mesh(survey.diags)
            return mesh, survey.diags, (
                f"Live refresh timed out after {_PAGE_LIVE_PER_NODE_TIMEOUT:.0f} s — showing cached data.")
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: get_nodes() failing is a failed call (thread_survey's own
            # contract), not an empty mesh — surfaced as the page's error banner
            # rather than a silently "no Thread devices" map.
            self.logger.warning("Matter: could not read the Thread mesh for the IWS page: %s", exc)
            return thread_mesh.build_mesh([]), [], f"matter-server could not be read: {exc}"

        # #334 finding B2.4: raw_count == 0 (nothing commissioned at all) and
        # "every raw node was unaddressable" (skipped, non-empty) are both
        # real failures, distinct from the friendly "no Thread devices"
        # success (raw_count > 0, diags empty, skipped empty).
        if survey.raw_count == 0:
            return thread_mesh.build_mesh([]), [], "matter-server reports no commissioned nodes at all."
        if not survey.diags and survey.skipped:
            return thread_mesh.build_mesh([]), [], (
                f"{len(survey.skipped)} raw node(s) could not be read: {'; '.join(survey.skipped)}")
        return thread_mesh.build_mesh(survey.diags), survey.diags, None

    # ``_thread_node_names`` lives on DiagnosticsMenuMixin (#334 post-review,
    # B5.4) — cross-mixin ``self.`` call, the established pattern issue #146
    # set, so this page and the "Report Thread mesh…" menu item can never name
    # a node differently. See that module for the method itself.
