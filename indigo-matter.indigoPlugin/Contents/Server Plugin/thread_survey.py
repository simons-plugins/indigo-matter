"""Thread mesh survey: cache-first, optionally live-refreshed (#334).

The one shared implementation of "read every Thread node's ThreadNetworkDiagnostics
and, optionally, live-refresh the sleepy ones" — the menu diagnostic
(:meth:`diagnostics_menu_mixin.DiagnosticsMenuMixin.menuReportThreadMesh`) and the
IWS Thread page (``http_api_mixin.http_thread_page``) both need exactly this, and
``thread_mesh``'s own module docstring already explains why a cached sleepy-device
reading cannot be trusted on its own (decision 2: a disproved "partition split" on
2026-09-01). Putting the coroutine here once, rather than in either caller, is what
keeps that live-read policy in one place instead of two call sites drifting apart.

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
precondition is a failed call, not an empty result). :class:`Survey` (#334
post-review, B2.4) carries the same distinction one step further: ``raw_count``
tells a caller whether matter-server reported ANY nodes at all, and ``skipped``
names the raw nodes this module itself could not even address (not a Mapping, or no
``node_id``) — as opposed to a raw node that parsed fine but simply is not a Thread
node (no cluster-0x35 key at all), which is never a "skip", just not this survey's
business.
"""
from __future__ import annotations

import asyncio
import dataclasses
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any, Mapping, Optional

from matter_client import ATTRIBUTE_TIMEOUT
from matter_model import parse_node
from plugin_constants import SURVEY_READ_TIMEOUT
from thread_mesh import (
    ATTR_LEADER_ROUTER_ID,
    ATTR_NEIGHBOR_TABLE,
    ATTR_PARTITION_ID,
    ATTR_ROUTE_TABLE,
    ATTR_ROUTING_ROLE,
    CLUSTER_THREAD_DIAG,
    NodeDiag,
    parse_node_diag,
)

__all__ = ["Survey", "survey_thread_nodes", "run_survey"]

# The five attributes a live read refreshes — exactly what a sleepy device's
# cached snapshot can be hours stale on (role, neighbours, routes, and the two
# fields a partition-split diagnosis hinges on). RouteTable (8) is included
# despite this only ever live-reading NON-routers: it is NOT a router-only
# structure — every SED in the 2026-09-01 fixture carries its own single-row
# RouteTable (its path to the leader) — so leaving it out of the refresh used
# to pair a freshly live LeaderRouterId with a stale cached RouteTable, which
# could make a non-router's far_from_leader check judge a hop count that no
# longer matched reality (#334 post-review, B4.5).
_LIVE_ATTR_IDS = (ATTR_ROUTING_ROLE, ATTR_NEIGHBOR_TABLE, ATTR_ROUTE_TABLE, ATTR_PARTITION_ID, ATTR_LEADER_ROUTER_ID)


@dataclasses.dataclass(frozen=True)
class Survey:
    """The result of one survey pass (#334 post-review, B2.4).

    ``raw_count`` and ``skipped`` exist so a caller can tell three genuinely
    different situations apart, none of which should be printed the same way:

    * ``raw_count == 0`` — matter-server reports no commissioned nodes at all.
    * ``raw_count > 0`` but ``diags`` is empty and ``skipped`` is non-empty — every
      raw node matter-server returned was something this module could not even
      address (not a Mapping, or carried no ``node_id``); a real failure, not an
      empty mesh.
    * ``raw_count > 0``, ``diags`` is empty, ``skipped`` is empty — every raw node
      parsed fine and none of them is a Thread node (a Wi-Fi-only fabric); the
      friendly "no Thread devices" answer.
    """

    diags: list[NodeDiag]
    raw_count: int
    skipped: list[str]


async def survey_thread_nodes(  # pylint: disable=too-many-locals
    matter: Any,
    *,
    live_sleepy: bool,
    per_node_timeout: float = ATTRIBUTE_TIMEOUT,
    node_names: Optional[Mapping[int, str]] = None,
) -> Survey:
    """Parse every Thread node matter-server knows about, optionally live-refreshed.

    ``get_nodes()`` supplies each node in the same shape as a single ``get_node``
    result (``node_id``, ``available``, ``attributes``) — verified against the
    fixture captured live from jarvis 2026-09-01. A raw node this module cannot
    even address (not a Mapping, or missing ``node_id``) is recorded in
    :attr:`Survey.skipped` rather than silently dropped; a raw node that parses
    fine but has no cluster-0x35 key at all (not a Thread node) is simply excluded
    — that is not a skip, it is a correct "not mine".

    When ``live_sleepy`` is true, every parsed diag that is NOT a router
    (:attr:`~thread_mesh.NodeDiag.is_router`) is live-read concurrently, each
    bounded by its own ``per_node_timeout``. Routers are never touched: a router is
    mains-powered and polled continuously already, so its cache is not the stale
    one. A live-read failure or timeout never drops the node: the cached
    :class:`~thread_mesh.NodeDiag` stands, with ``read_error`` set to say why.
    """
    raw_nodes = await matter.get_nodes()  # a failure here is a failed call — let it raise
    raw_nodes = raw_nodes or []

    diags: list[NodeDiag] = []
    skipped: list[str] = []
    raw_attributes_by_node: dict[int, Mapping] = {}
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping):
            skipped.append(f"raw node[{index}]: not a mapping ({type(raw).__name__})")
            continue
        raw_node_id = raw.get("node_id")
        if raw_node_id is None:
            skipped.append(f"raw node[{index}]: no node_id")
            continue
        node_id = int(raw_node_id)
        attributes = raw.get("attributes") or {}
        name = _node_name(node_id, attributes, node_names)
        diag = parse_node_diag(node_id, name, bool(raw.get("available", True)), attributes)
        if diag is None:
            continue  # not a Thread node — correct exclusion, not a skip
        diags.append(diag)
        raw_attributes_by_node[node_id] = attributes

    if live_sleepy:
        targets = [diag for diag in diags if not diag.is_router]
        if targets:
            results = await asyncio.gather(
                *(_live_refresh(matter, diag, raw_attributes_by_node[diag.node_id], per_node_timeout)
                  for diag in targets),
                return_exceptions=True,
            )
            # _live_refresh never raises by design (every failure path inside it
            # sets read_error and returns) — but the merge/re-parse step used to
            # sit outside any try/except, so an unexpected bug there would raise
            # OUT of the coroutine, be swallowed silently by
            # return_exceptions=True, and never reach an operator (#334 post-
            # review, B2.10). This makes that impossible: any exception result is
            # stamped onto its diag exactly like every other live-read failure.
            for diag, result in zip(targets, results):
                if isinstance(result, BaseException):
                    diag.read_error = f"live refresh failed: {result!r}"

    return Survey(diags=diags, raw_count=len(raw_nodes), skipped=skipped)


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

    live_values = dict(zip(_LIVE_ATTR_IDS, values))
    neighbor_table_value = live_values[ATTR_NEIGHBOR_TABLE]
    if neighbor_table_value is None or not isinstance(neighbor_table_value, list):
        # #334 post-review, B3.3: a live NeighborTable of None/garbage must not
        # become "live / 0 neighbours / ok" — that is indistinguishable from a
        # genuinely healthy router with no neighbours, and ties to thread_mesh's
        # own absent-vs-empty distinction (B2.1). Keep the CACHED diag entirely
        # (do not partial-merge the other attrs that DID answer) rather than
        # invent a live reading this module cannot actually stand behind.
        diag.read_error = "live read returned no NeighborTable"
        return

    merged = dict(raw_attributes)
    for attr, value in live_values.items():
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
    # Per-field copy rather than diag.__dict__.update(live_diag.__dict__): same
    # behaviour, but copies only NodeDiag's own declared fields, and survives a
    # future NodeDiag gaining __slots__ (#334 post-review, B1.1).
    for declared_field in dataclasses.fields(NodeDiag):
        setattr(diag, declared_field.name, getattr(live_diag, declared_field.name))


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
) -> Survey:
    """Indigo-thread bridge for :func:`survey_thread_nodes` (Indigo -> asyncio,
    see ``async_runtime``'s module docstring). Blocks the calling Indigo thread.

    The non-router live reads inside run concurrently, so the total wait is NOT
    ``per_node_timeout`` multiplied by node count — it is one ``per_node_timeout``
    budget for the slowest of them, plus ``SURVEY_READ_TIMEOUT`` for the
    ``get_nodes()`` round trip and result marshalling back across the bridge
    (the same margin ``_fetch_node`` gives a single ``get_node`` call).

    On a timeout, the underlying future is cancelled before the
    :class:`~concurrent.futures.TimeoutError` is re-raised (#334 post-review,
    B2.7) — the survey coroutine itself keeps trying against a caller who has
    already given up otherwise, tying up the async runtime's loop for a result
    nobody is waiting for any more.
    """
    future = runtime.submit(
        survey_thread_nodes(matter, live_sleepy=live_sleepy, per_node_timeout=per_node_timeout,
                            node_names=node_names)
    )
    try:
        return future.result(timeout=per_node_timeout + SURVEY_READ_TIMEOUT)
    except FuturesTimeoutError:
        future.cancel()
        raise
