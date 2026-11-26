"""Thread mesh survey: cache-first, optionally live-refreshed (#334).

The one shared implementation of "read every Thread node's ThreadNetworkDiagnostics
and, optionally, live-refresh them" — the menu diagnostic
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
from datetime import datetime, timezone
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
    ROLE_ED,
    ROLE_REED,
    ROLE_ROUTER,
    ROLE_SED,
    ROLE_UNASSIGNED,
    ROLE_UNSPECIFIED,
    NodeDiag,
    parse_node_diag,
)

__all__ = ["Survey", "survey_thread_nodes", "run_survey"]

# The five attributes a live read refreshes — exactly what a sleepy device's
# cached snapshot can be hours stale on (role, neighbours, routes, and the two
# fields a partition-split diagnosis hinges on). RouteTable (8) is included for
# every node: it is NOT a router-only structure — every SED in the 2026-09-01
# fixture carries its own single-row RouteTable (its path to the leader) — so
# leaving it out of the refresh used to pair a freshly live LeaderRouterId with a
# stale cached RouteTable, which could make a non-router's far_from_leader check
# judge a hop count that no longer matched reality (#334 post-review, B4.5).
# NeighborTable (7) and RouteTable (8) are also the two attributes a router can
# never be trusted to have fresh in the cache: Thread marks them changes-omitted,
# so a subscription never refreshes them (#348).
_LIVE_ATTR_IDS = (ATTR_ROUTING_ROLE, ATTR_NEIGHBOR_TABLE, ATTR_ROUTE_TABLE, ATTR_PARTITION_ID, ATTR_LEADER_ROUTER_ID)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort parse of matter-server's ``date_commissioned``/
    ``last_interview`` raw-node fields (#344): a ``datetime`` passes through
    (naive assumed UTC), an ISO-8601 ``str`` is parsed tolerating a trailing
    "Z" and no timezone (assumed UTC), an ``int``/``float`` is epoch seconds.
    Anything else — or a string ``datetime.fromisoformat`` can't make sense
    of — degrades to ``None``, never raises: the shipped fixture
    (``tests/fixtures/thread_mesh/nodes.json``) has neither key at all, and
    absence must be the boring case, not a crash (root workspace CLAUDE.md
    degradation-path convention)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


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
    live_read: bool,
    per_node_timeout: float = ATTRIBUTE_TIMEOUT,
    node_names: Optional[Mapping[int, str]] = None,
    event_stamps: Optional[Mapping[int, float]] = None,
) -> Survey:
    """Parse every Thread node matter-server knows about, optionally live-refreshed.

    ``get_nodes()`` supplies each node in the same shape as a single ``get_node``
    result (``node_id``, ``available``, ``attributes``) — verified against the
    fixture captured live from jarvis 2026-09-01. A raw node this module cannot
    even address (not a Mapping, or missing ``node_id``) is recorded in
    :attr:`Survey.skipped` rather than silently dropped; a raw node that parses
    fine but has no cluster-0x35 key at all (not a Thread node) is simply excluded
    — that is not a skip, it is a correct "not mine".

    When ``live_read`` is true, EVERY parsed diag — routers included (#348) — is
    live-read concurrently, each bounded by its own ``per_node_timeout``. Routers
    used to be skipped on the theory that a mains-powered node's cache is fresh,
    but NeighborTable and RouteTable are changes-omitted attributes: a
    subscription never refreshes them, so a router's cached tables go stale
    exactly like a sleepy device's. Because the reads are concurrent, the worst
    case is one ``per_node_timeout`` however many nodes there are. A live-read
    failure or timeout never drops the node: the cached
    :class:`~thread_mesh.NodeDiag` stands, with ``read_error`` set to say why (and
    its cached children stay too — a failed read is "unknown", never "no
    children"; see :func:`_live_refresh` for the opposite, successful case).

    **#344 cache-age fields are applied AFTER live-refresh, not before.**
    ``date_commissioned``/``last_interview`` (from the raw node's own top-level
    keys) and ``last_event_seen`` (from ``event_stamps``) are captured during the
    raw loop below but only WRITTEN onto each diag once the live-refresh block
    has finished. ``_live_refresh`` field-copies every ``dataclasses.fields
    (NodeDiag)`` from its own freshly-parsed live diag (which never sets these
    three — only :mod:`thread_survey` does) onto the cached one it is
    refreshing; applying them earlier would have a successful live read wipe
    them straight back to ``None`` on exactly the node whose refresh worked.

    **A present-but-unparseable timestamp is visible, not silent** (#344
    review, finding A1): when the raw node carries a ``date_commissioned``/
    ``last_interview`` key that :func:`_parse_timestamp` could not make sense
    of, that lands in the diag's own ``parse_warnings`` — distinguishing
    "matter-server omits this" (the shipped fixture's own case, and boring)
    from "the wire shape changed" (worth an operator's attention). An absent
    key stays silent either way.
    """
    raw_nodes = await matter.get_nodes()  # a failure here is a failed call — let it raise
    raw_nodes = raw_nodes or []

    diags: list[NodeDiag] = []
    skipped: list[str] = []
    raw_attributes_by_node: dict[int, Mapping] = {}
    raw_timestamps_by_node: dict[int, tuple[Optional[datetime], Optional[datetime]]] = {}
    # #344 review, A1: the RAW (pre-parse) values alongside the parsed ones —
    # a key that IS present but that _parse_timestamp could not make sense of
    # (matter-server's wire shape changing) must be visible, the same
    # principle as _on_attribute's malformed-frame warning; a key that is
    # simply ABSENT (the shipped fixture's own case) stays silent. Applied as
    # a parse_warning in the final loop below, alongside the fields
    # themselves, so it inherits the same after-live-refresh timing (decision
    # 2's own ordering) rather than needing a second one.
    raw_stamp_values_by_node: dict[int, tuple[Any, Any]] = {}
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
        raw_date_commissioned = raw.get("date_commissioned")
        raw_last_interview = raw.get("last_interview")
        raw_timestamps_by_node[node_id] = (
            _parse_timestamp(raw_date_commissioned),
            _parse_timestamp(raw_last_interview),
        )
        raw_stamp_values_by_node[node_id] = (raw_date_commissioned, raw_last_interview)

    if live_read:
        targets = diags
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

    # #344's apply-after-refresh rule (see this function's docstring):
    # applied HERE, after every live refresh above has already run its
    # per-field copy, so a successful live read cannot wipe these back to
    # None.
    stamps = event_stamps or {}
    for diag in diags:
        date_commissioned, last_interview = raw_timestamps_by_node.get(diag.node_id, (None, None))
        diag.date_commissioned = date_commissioned
        diag.last_interview = last_interview
        diag.last_event_seen = stamps.get(diag.node_id)
        # #344 review, A1: a raw value that WAS present but did not survive
        # _parse_timestamp is a wire-shape change worth surfacing — this is
        # the only place it is safe to mutate parse_warnings (a mutable
        # list), because this loop runs after _live_refresh's own per-field
        # copy has already finished for every targeted diag.
        raw_date_commissioned, raw_last_interview = raw_stamp_values_by_node.get(diag.node_id, (None, None))
        if raw_date_commissioned is not None and date_commissioned is None:
            diag.parse_warnings.append(f"date_commissioned unparseable: {raw_date_commissioned!r}")
        if raw_last_interview is not None and last_interview is None:
            diag.parse_warnings.append(f"last_interview unparseable: {raw_last_interview!r}")

    return Survey(diags=diags, raw_count=len(raw_nodes), skipped=skipped)


_ATTR_NAMES = {
    ATTR_ROUTING_ROLE: "RoutingRole",
    ATTR_NEIGHBOR_TABLE: "NeighborTable",
    ATTR_ROUTE_TABLE: "RouteTable",
    ATTR_PARTITION_ID: "PartitionId",
    ATTR_LEADER_ROUTER_ID: "LeaderRouterId",
}

#: Live roles that say "not attached": such a node has no partition or leader to
#: report, so a null there is its true answer, not an unusable one (#348 review).
#: Unspecified counts — a live "role unknown" statement must not be replaced by a
#: cached "attached" one (it surfaces as ``role_unknown``).
_NOT_ATTACHED_ROLES = (ROLE_UNSPECIFIED, ROLE_UNASSIGNED)

#: The attached, NON-leader roles. Only for these is "I have no neighbours at all"
#: implausible enough, when the role is unchanged from the cache, to be distrusted.
#: A Leader can legitimately be alone in its own partition, and Unassigned has no
#: neighbours by definition.
_ATTACHED_NON_LEADER_ROLES = (ROLE_SED, ROLE_ED, ROLE_REED, ROLE_ROUTER)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unusable_live_answers(live_values: Mapping[int, Any]) -> list[str]:
    """Why the live answers cannot replace the cache — one message per unusable
    attribute, empty when every one is usable. The governing rule of the live pass
    (#348 review): a live answer replaces the cache only when it is USABLE; the
    caller treats ANY unusable one as a failed read of the whole node, so a node
    is never left half cached and half live."""
    problems: list[str] = []
    role = live_values[ATTR_ROUTING_ROLE]
    if not _is_int(role):
        problems.append("live read returned no usable RoutingRole")
    if not isinstance(live_values[ATTR_NEIGHBOR_TABLE], list):
        problems.append("live read returned no NeighborTable")
    if not isinstance(live_values[ATTR_ROUTE_TABLE], list):
        problems.append("live read returned no usable RouteTable")
    detached = role in _NOT_ATTACHED_ROLES
    for attr in (ATTR_PARTITION_ID, ATTR_LEADER_ROUTER_ID):
        value = live_values[attr]
        if not _is_int(value) and not (detached and value is None):
            problems.append(f"live read returned no usable {_ATTR_NAMES[attr]}")
    return problems


async def _live_refresh(  # pylint: disable=too-many-return-statements
        matter: Any, diag: NodeDiag, raw_attributes: Mapping, per_node_timeout: float) -> None:
    """Live-read ``diag`` in place; never raises — a failure only sets ``read_error``.

    Mutates ``diag`` directly (it is a plain, non-frozen dataclass already sitting
    in the caller's list) rather than returning a replacement, so one failing node
    cannot disturb the others sharing the same ``asyncio.gather`` call.

    **All-or-nothing, usable-or-failed (#348 review).** Either every live answer is
    usable and they replace the cached values together (``source`` -> ``"live"``),
    or the read FAILED: the cached diag is left exactly as it was, ``read_error``
    says why, ``source`` stays ``"cache"``. ``read_error`` is never set on a
    ``"live"`` diag. Failed means: any attribute read raised or the node timed out
    (the message names each attribute); any answer is unusable (see
    :func:`_unusable_live_answers`); or the live NeighborTable came back empty when
    the cache had neighbours AND the node still claims, live, the same attached
    non-leader role as in the cache (Router, REED, End Device, Sleepy End Device).
    An unchanged "attached, but I see nobody" is far likelier a failed read than
    "everyone left"; but a node that live-reports Unassigned/Unspecified (really
    detached), Leader (alone in its own partition) or ANY different role has
    demonstrably answered, and that answer is exactly what this diagnostic exists
    to show — it is taken as it is. A table whose rows existed but none parsed is
    unparseable (not empty) and is a failed read under every role.
    """
    node_id = diag.node_id
    cached_child_rlocs = [child.rloc16 for child in diag.children]
    cached_neighbour_count = len(diag.neighbours)
    cached_role = diag.role
    try:
        values = await asyncio.wait_for(
            asyncio.gather(*(matter.read(node_id, 0, CLUSTER_THREAD_DIAG, attr) for attr in _LIVE_ATTR_IDS),
                           return_exceptions=True),
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

    # return_exceptions=True so one attribute failing is NAMED rather than hiding
    # which of the five it was; any failure is still a failed read of the node.
    failures = []
    for attr, value in zip(_LIVE_ATTR_IDS, values):
        if isinstance(value, BaseException):
            reason = "timed out" if isinstance(value, asyncio.TimeoutError) else str(value) or repr(value)
            failures.append(f"{_ATTR_NAMES[attr]} {reason}")
    if failures:
        if all(isinstance(value, asyncio.TimeoutError) for value in values):
            diag.read_error = f"live read timed out after {per_node_timeout:g} s"  # the whole node, as before
        else:
            diag.read_error = f"live read failed: {'; '.join(failures)}"
        return

    live_values = dict(zip(_LIVE_ATTR_IDS, values))
    problems = _unusable_live_answers(live_values)
    if problems:
        # #334 post-review, B3.3 (and #348 review): a live NeighborTable of
        # None/garbage must not become "live / 0 neighbours / ok" — and the same
        # goes for a role, route table, partition or leader that came back
        # unusable. Keep the CACHED diag entirely (no partial merge of the attrs
        # that DID answer) rather than invent a live reading this module cannot
        # stand behind.
        diag.read_error = "; ".join(problems)
        return

    merged = dict(raw_attributes)
    for attr, value in live_values.items():
        # Both key shapes: parse_node_diag reads whichever is present, and the
        # source dict may already be in either form.
        merged[(0, CLUSTER_THREAD_DIAG, attr)] = value
        merged[f"0/{CLUSTER_THREAD_DIAG}/{attr}"] = value

    live_diag = parse_node_diag(node_id, diag.name, diag.available, merged)
    if live_diag is None:
        diag.read_error = "live read returned no usable RoutingRole"
        return
    live_row_count = len(live_values[ATTR_NEIGHBOR_TABLE])
    skipped_rows = live_row_count - len(live_diag.neighbours)
    if live_row_count and not live_diag.neighbours:
        diag.read_error = (
            f"live neighbour table had {live_row_count} row(s) and none could be parsed — "
            "the cached table is shown"
        )
        return
    if (not live_row_count and cached_neighbour_count
            and live_diag.role == cached_role and cached_role in _ATTACHED_NON_LEADER_ROLES):
        diag.read_error = (
            "live neighbour table came back empty while the cache had "
            f"{cached_neighbour_count} neighbour(s) and the role is unchanged — the cached table is shown"
        )
        return
    live_diag.source = "live"
    # Per-field copy rather than diag.__dict__.update(live_diag.__dict__): same
    # behaviour, but copies only NodeDiag's own declared fields, and survives a
    # future NodeDiag gaining __slots__ (#334 post-review, B1.1).
    for declared_field in dataclasses.fields(NodeDiag):
        setattr(diag, declared_field.name, getattr(live_diag, declared_field.name))

    # #348: only reached after a SUCCESSFUL, usable read. The live table replaced
    # the cached one, so a cached child it no longer lists was not seen live; it is
    # not drawn as an anonymous child of this router (the map is built from
    # diag.children) and the node's own parse_warnings say so. Worded as the
    # OBSERVATION, not "departed": the child may have re-attached elsewhere, and an
    # owned device is still drawn from its own parent data. Skipped when any live row
    # was skipped as malformed — the skipped row might have been that child.
    # Appended AFTER the field copy above, which overwrites parse_warnings.
    if skipped_rows == 0:
        live_rlocs = {neighbour.rloc16 for neighbour in diag.neighbours}
        for rloc16 in cached_child_rlocs:
            if rloc16 not in live_rlocs:
                diag.parse_warnings.append(
                    f"child 0x{rloc16:04X} was in matter-server's cache but is absent from this router's "
                    "live neighbour table (it has left or re-attached elsewhere), so it is not drawn as an "
                    "anonymous child of this router"
                )


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
    live_read: bool,
    per_node_timeout: float = ATTRIBUTE_TIMEOUT,
    node_names: Optional[Mapping[int, str]] = None,
    event_stamps: Optional[Mapping[int, float]] = None,
) -> Survey:
    """Indigo-thread bridge for :func:`survey_thread_nodes` (Indigo -> asyncio,
    see ``async_runtime``'s module docstring). Blocks the calling Indigo thread.

    The live reads inside run concurrently, so the total wait is NOT
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
        survey_thread_nodes(matter, live_read=live_read, per_node_timeout=per_node_timeout,
                            node_names=node_names, event_stamps=event_stamps)
    )
    try:
        return future.result(timeout=per_node_timeout + SURVEY_READ_TIMEOUT)
    except FuturesTimeoutError:
        future.cancel()
        raise
