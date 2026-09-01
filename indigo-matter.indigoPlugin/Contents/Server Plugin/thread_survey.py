"""Thread mesh survey: cache-first, optionally live-refreshed (#334).

The one shared implementation of "read every Thread node's ThreadNetworkDiagnostics
and, optionally, live-refresh the sleepy ones" — the menu diagnostic
(:meth:`diagnostics_menu_mixin.DiagnosticsMenuMixin.menuReportThreadMesh`) and the
IWS page (a later PR) both need exactly this, and ``thread_mesh``'s own module
docstring already explains why a cached sleepy-device reading cannot be trusted on
its own (decision 2: a disproved "partition split" on 2026-09-01). Putting the
coroutine here once, rather than in either caller, is what keeps that live-read
policy in one place instead of two call sites drifting apart.

Pure asyncio + :mod:`thread_mesh` (plus :func:`matter_model.parse_node` for the
vendor/product fallback name — Indigo-free, matter-server-free, same as
``thread_mesh`` itself). No ``indigo`` import, no matter-server import: the caller
supplies an already-connected ``matter`` client (``matter_client.MatterClient`` in
production, a fake in tests) and, for the sync helper, an already-running
``async_runtime.AsyncRuntime``.

**A ``get_nodes()`` failure raises.** Every other per-node failure — a live read
that times out, a node whose attributes will not parse — degrades to a flag or a
``read_error`` on that one node's :class:`~thread_mesh.NodeDiag`, never a crash and
never a silently thinned result. But an empty mesh because the fabric genuinely has
no Thread devices and an empty mesh because ``get_nodes()`` itself failed are two
different answers, and only one of them is safe to print as "no Thread devices are
commissioned" (root workspace CLAUDE.md degradation-path convention: an unusable
precondition is a failed call, not an empty result).
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping, Optional

from matter_client import ATTRIBUTE_TIMEOUT
from matter_model import parse_node
from plugin_constants import SURVEY_READ_TIMEOUT
from thread_mesh import (
    ATTR_LEADER_ROUTER_ID,
    ATTR_NEIGHBOR_TABLE,
    ATTR_PARTITION_ID,
    ATTR_ROUTING_ROLE,
    CLUSTER_THREAD_DIAG,
    NodeDiag,
    parse_node_diag,
)

__all__ = ["survey_thread_nodes", "run_survey"]

# The four attributes a live read refreshes — exactly what a sleepy device's
# cached snapshot can be hours stale on (role, neighbours, and the two fields a
# partition-split diagnosis hinges on). NeighborTable (7) also carries the
# frame-error/RSSI numbers the health flags key off of. RouteTable (8) is left
# alone: it is a router-only structure and this only ever live-reads non-routers.
_LIVE_ATTR_IDS = (ATTR_ROUTING_ROLE, ATTR_NEIGHBOR_TABLE, ATTR_PARTITION_ID, ATTR_LEADER_ROUTER_ID)


async def survey_thread_nodes(
    matter: Any,
    *,
    live_sleepy: bool,
    per_node_timeout: float = ATTRIBUTE_TIMEOUT,
    node_names: Optional[Mapping[int, str]] = None,
) -> list[NodeDiag]:
    """Parse every Thread node matter-server knows about, optionally live-refreshed.

    ``get_nodes()`` supplies each node in the same shape as a single ``get_node``
    result (``node_id``, ``available``, ``attributes``) — verified against the
    fixture captured live from jarvis 2026-09-01. Any node without attribute 1
    (RoutingRole) is not a Thread node and :func:`thread_mesh.parse_node_diag`
    returns ``None`` for it, so it is silently excluded here too.

    When ``live_sleepy`` is true, every returned diag that is NOT a router
    (:attr:`NodeDiag.is_router`) is live-read concurrently, each bounded by its own
    ``per_node_timeout``. Routers are never touched: their RouteTable — the far
    more useful table for a router — is not part of this refresh, and a router is
    mains-powered and polled continuously already, so its cache is not the stale
    one. A live-read failure or timeout never drops the node: the cached
    :class:`~thread_mesh.NodeDiag` stands, with ``read_error`` set to say why.
    """
    raw_nodes = await matter.get_nodes()  # a failure here is a failed call — let it raise

    diags: list[NodeDiag] = []
    raw_attributes_by_node: dict[int, Mapping] = {}
    for raw in raw_nodes or []:
        raw_node_id = raw.get("node_id") if isinstance(raw, Mapping) else None
        if raw_node_id is None:
            continue
        node_id = int(raw_node_id)
        attributes = raw.get("attributes") or {}
        name = _node_name(node_id, attributes, node_names)
        diag = parse_node_diag(node_id, name, bool(raw.get("available", True)), attributes)
        if diag is None:
            continue
        diags.append(diag)
        raw_attributes_by_node[node_id] = attributes

    if live_sleepy:
        targets = [diag for diag in diags if not diag.is_router]
        if targets:
            await asyncio.gather(
                *(_live_refresh(matter, diag, raw_attributes_by_node[diag.node_id], per_node_timeout)
                  for diag in targets),
                return_exceptions=True,
            )

    return diags


async def _live_refresh(matter: Any, diag: NodeDiag, raw_attributes: Mapping, per_node_timeout: float) -> None:
    """Live-read ``diag`` in place; never raises — a failure only sets ``read_error``.

    Mutates ``diag`` directly (it is a plain, non-frozen dataclass already sitting
    in the caller's list) rather than returning a replacement, so one failing node
    cannot disturb the others sharing the same ``asyncio.gather`` call.
    """
    node_id = diag.node_id
    try:
        values = await asyncio.wait_for(
            asyncio.gather(*(matter.read(node_id, 0, CLUSTER_THREAD_DIAG, attr) for attr in _LIVE_ATTR_IDS)),
            timeout=per_node_timeout,
        )
    except asyncio.TimeoutError:
        diag.read_error = f"live read timed out after {per_node_timeout:g} s"
        return
    except Exception as exc:  # pylint: disable=broad-except
        # Absorbing: any failure of a live device read for THIS node only —
        # matter.read's failure modes are open-ended (protocol refusal, a
        # connection dropping mid-await, a sleepy device that never polls in
        # time). The cached diag already parsed successfully and stands; the
        # only question is whether to say the live refresh failed, which this
        # does via read_error rather than swallowing it.
        diag.read_error = f"live read failed: {exc}"
        return

    merged = dict(raw_attributes)
    for attr, value in zip(_LIVE_ATTR_IDS, values):
        # Both key shapes: parse_node_diag reads whichever is present, and the
        # source dict may already be in either form.
        merged[(0, CLUSTER_THREAD_DIAG, attr)] = value
        merged[f"0/{CLUSTER_THREAD_DIAG}/{attr}"] = value

    live_diag = parse_node_diag(node_id, diag.name, diag.available, merged)
    if live_diag is None:
        # RoutingRole came back empty/unparseable — treat like any other failed
        # live read rather than silently keeping half-updated cached data.
        diag.read_error = "live read returned no usable RoutingRole"
        return
    live_diag.source = "live"
    diag.__dict__.update(live_diag.__dict__)


def _node_name(node_id: int, attributes: Mapping, node_names: Optional[Mapping[int, str]]) -> str:
    """The caller's name for this node if given, else "<vendor> <product>" from
    BasicInformation, else "node 0x..". Reuses :func:`matter_model.parse_node`
    rather than re-deriving vendor/product extraction — same attribute shape,
    already tested there."""
    if node_names:
        name = node_names.get(node_id)
        if name:
            return name
    info = parse_node({"node_id": node_id, "attributes": attributes})
    parts = [part for part in (info.vendor_name, info.product_name) if part]
    if parts:
        return " ".join(parts)
    return f"node 0x{node_id:X}"


def run_survey(
    runtime: Any,
    matter: Any,
    *,
    live_sleepy: bool,
    per_node_timeout: float = ATTRIBUTE_TIMEOUT,
    node_names: Optional[Mapping[int, str]] = None,
) -> list[NodeDiag]:
    """Indigo-thread bridge for :func:`survey_thread_nodes` (Indigo -> asyncio,
    see ``async_runtime``'s module docstring). Blocks the calling Indigo thread.

    The non-router live reads inside run concurrently, so the total wait is NOT
    ``per_node_timeout`` multiplied by node count — it is one ``per_node_timeout``
    budget for the slowest of them, plus ``SURVEY_READ_TIMEOUT`` for the
    ``get_nodes()`` round trip and result marshalling back across the bridge
    (the same margin ``_fetch_node`` gives a single ``get_node`` call).
    """
    return runtime.submit(
        survey_thread_nodes(matter, live_sleepy=live_sleepy, per_node_timeout=per_node_timeout,
                            node_names=node_names)
    ).result(timeout=per_node_timeout + SURVEY_READ_TIMEOUT)
