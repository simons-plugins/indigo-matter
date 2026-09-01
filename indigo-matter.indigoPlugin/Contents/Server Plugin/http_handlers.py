"""Dispatch logic for the Domio HTTP API (API.md v1.1), served over IWS.

The Plugin's ``http_*`` methods are thin adapters: they translate Indigo's
``action.props`` into ``(method, path_args, query)`` and hand off to this class,
then serialise the ``(status, body)`` result into an ``indigo.Dict``. Keeping the
routing here (with no Indigo and no asyncio) makes the whole API surface unit
testable.

Async providers (status / decommission / diagnostics talk to matter-server, which
lives on the loop) are injected as **synchronous** callables — the Plugin builds
them by bridging into the loop via ``runtime.submit(coro).result(timeout)``.
``commission`` create/poll is synchronous already (the job worker is scheduled
onto the loop by :class:`CommissionJobs`).
"""
from __future__ import annotations

from typing import Any, Callable, Optional


class MatterUnavailable(Exception):
    """Raised by a provider when matter-server is unreachable / timed out.

    Mapped to 503 so a wedged or down matter-server is never reported to Domio
    as a 404 'node_not_found' (which the contract reserves for an unknown node).
    """


def parse_node_id(text: str) -> Optional[int]:
    """Parse a nodeId path component ('0xABC' or decimal) to int, or None."""
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        return None


class HttpApi:
    """Routes the five Domio endpoints to the job table and matter providers."""

    def __init__(
        self,
        jobs: Any,
        logger: Any,
        *,
        status_provider: Callable[[], dict],
        decommission_provider: Callable[[int], Optional[dict]],
        diagnostics_provider: Callable[[int], Optional[dict]],
    ) -> None:
        self._jobs = jobs
        self.logger = logger
        self._status_provider = status_provider
        self._decommission = decommission_provider
        self._diagnostics = diagnostics_provider

    # ------------------------------------------------------------------
    # GET …/status
    # ------------------------------------------------------------------
    def status(self) -> tuple[int, dict]:
        body = self._status_provider()
        code = 200 if body.get("ready") else 503
        return code, body

    # ------------------------------------------------------------------
    # POST …/commission   and   GET …/commission/{jobId}
    # ------------------------------------------------------------------
    def commission(self, method: str, path_args: list, query: dict) -> tuple[int, dict]:
        if path_args:
            return self._jobs.get_job(path_args[0])
        if method.upper() != "POST":
            return 405, {"error": "method_not_allowed", "message": "POST required to start commissioning"}
        return self._jobs.create_job(query)

    # ------------------------------------------------------------------
    # POST …/decommission?nodeId=…   (or …/decommission/{nodeId})
    # ------------------------------------------------------------------
    def decommission(self, method: str, path_args: list, query: Optional[dict] = None) -> tuple[int, dict]:
        if method.upper() != "POST":
            return 405, {"error": "method_not_allowed", "message": "POST required to decommission"}
        # IWS only delivers trailing path components on GET, never on POST, so a
        # POST handler must take its id from the query string. The path component
        # is kept as a fallback for any transport that does deliver it.
        raw = path_args[0] if path_args else (query or {}).get("nodeId")
        if not raw:
            return 400, {"error": "invalid_request", "message": "nodeId required (nodeId query param)"}
        node_id = parse_node_id(raw)
        if node_id is None:
            return 400, {"error": "invalid_node_id", "message": f"bad nodeId: {raw}"}
        try:
            result = self._decommission(node_id)
        except MatterUnavailable as exc:
            return 503, {"error": "matter_server_unreachable", "message": str(exc)}
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any decommission failure the providers did not classify. The
            # bottom rung of a deliberate ladder — 405 method, 400 bad nodeId, 503
            # MatterUnavailable, 404 unknown node all sit above it — so this is the
            # 500 case, not a collapse. An escape would blow the IWS action callback
            # and give the HTTP caller nothing at all.
            self.logger.exception(exc)
            return 500, {"error": "internal_error", "message": str(exc)}
        if result is None:
            return 404, {"error": "node_not_found"}
        return 200, result

    # ------------------------------------------------------------------
    # GET …/diagnostics/{nodeId}
    # ------------------------------------------------------------------
    def diagnostics(self, path_args: list, query: Optional[dict] = None) -> tuple[int, dict]:
        # GET delivers the path component; accept a nodeId query param too for
        # symmetry with decommission and transport independence.
        raw = path_args[0] if path_args else (query or {}).get("nodeId")
        if not raw:
            return 400, {"error": "invalid_request", "message": "nodeId required (path component or nodeId query param)"}
        node_id = parse_node_id(raw)
        if node_id is None:
            return 400, {"error": "invalid_node_id", "message": f"bad nodeId: {raw}"}
        try:
            result = self._diagnostics(node_id)
        except MatterUnavailable as exc:
            return 503, {"error": "matter_server_unreachable", "message": str(exc)}
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any diagnostics failure the providers did not classify. The
            # bottom rung of a deliberate ladder — 405 method, 400 bad nodeId, 503
            # MatterUnavailable, 404 unknown node all sit above it — so this is the
            # 500 case, not a collapse. An escape would blow the IWS action callback
            # and give the HTTP caller nothing at all.
            self.logger.exception(exc)
            return 500, {"error": "internal_error", "message": str(exc)}
        if result is None:
            return 404, {"error": "node_not_found"}
        return 200, result
