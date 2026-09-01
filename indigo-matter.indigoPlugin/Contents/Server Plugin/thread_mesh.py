"""Thread mesh model: parse ThreadNetworkDiagnostics (cluster 0x0035) into a mesh (#334).

Thread devices commissioned through Indigo/Domio are invisible to Apple's own Thread
tooling (Thread Doctor only lists HomeKit accessories), and matter-server already
caches every node's ThreadNetworkDiagnostics attributes. This module turns that cache
into roles, neighbour/route links, the leader, foreign routers and health flags —
pure data in, pure data out. No ``indigo`` import, no matter-server import, no I/O.

Two decisions here are not obvious from the Matter spec, and were reached against
real hardware on 2026-09-01 (fixture: ``tests/fixtures/thread_mesh/nodes.json``, 7
nodes):

1. **Nodes and routers are keyed by RLOC16 / router id, never by ExtAddress.**
   matter-server serialises a Thread ExtAddress (a uint64) as a JS ``Number``, which
   silently loses low bits above 2**53 — every real ext address in the fixture is
   above that limit. Two independent readings of the *same* physical router can
   still agree with each other (same lossy conversion, same source), which is enough
   to *corroborate* an identity, but not enough to *establish* one. RLOC16 (and the
   router id folded into it, ``rloc16 >> 10``) is a 16-bit field carried intact, and
   is what every other node's tables reference it by, so it is the only reliable key.
   ExtAddress is shown only as informational hex text, flagged "≈" past the
   precision limit (:func:`format_ext_address`).
2. **A cached value is not a live one, and this module says so rather than concludes
   from it.** ``matter-server``'s ``get_node`` is a snapshot; for sleepy devices it
   can be hours stale. On 2026-09-01, two nodes' cached ``PartitionId`` disagreed with
   the other five — this looked exactly like a partition split until a live read
   disproved it. So every :class:`NodeDiag` carries ``source`` ("cache" or "live"),
   :func:`build_mesh` records disagreeing partitions in ``Mesh.disagreements``
   instead of asserting a split, and :func:`health_flags` emits an info-level
   ``stale`` note for any cached sleepy-device reading — never a conclusion.

Two more things worth stating because they are not obvious from re-reading the code
later. First, a router's own self-entry in its RouteTable is the row with
``NextHop == INVALID_ROUTER_ID and PathCost == 0 and Allocated and not LinkEstablished``
— a node is never its own radio neighbour, so its self-entry is the one candidate
*without* an established link, and this only makes sense for a Router/Leader (role
gate): a REED's RouteTable can otherwise contain a row shaped exactly like a
self-entry that actually describes a neighbouring router. 0x34 in the fixture has
two rows shaped like ``NextHop==63, PathCost==0, Allocated`` before that last
condition — its own entry (router 62, LinkEstablished False) and its neighbour 0x3F's
entry (router 23, LinkEstablished True) — and only the LinkEstablished check tells
them apart. :attr:`NodeDiag.router_id` still returns ``None`` (with a note in
``parse_warnings``, and — since #334's post-review pass — a ``router_id_unknown``
health flag) on the — now theoretical — case where more than one candidate remains
even after both the role gate and the link check, because a wrong guess would
silently merge two different routers under one key in :func:`build_mesh`.

Second, the "representative" RSSI used for the ``weak_link`` health check is the
WORSE of AverageRssi and LastRssi (either may be null), and the flag message names
which one tripped it — a real parent link in the fixture averages -21 dBm but last
read -95 dBm, and a check that only looked at one of the two could miss it depending
on which. :attr:`Neighbour.rssi`, used for *display* (``summary_states``'
``threadLinkRssi``), stays LastRssi-preferred: the most recent reading is what an
operator watching the state wants, whereas the health check's job is to catch
whichever reading is worse — a deliberately different convention from the health
check's, not the same one restated (see the property's own docstring).

**Absent is not empty (#334 post-review, finding B2.1).** matter-server reporting
``None`` for NeighborTable (never subscribed/read) and matter-server reporting some
non-list garbage both used to collapse silently to "no neighbours" — indistinguishable
from a router that genuinely has none, and specifically wrong: an un-cached table is
"we don't know", not "there is nothing there". :attr:`NodeDiag.neighbours_known`
carries that distinction; :func:`health_flags` and :func:`summary_states` both
consult it before drawing any conclusion from ``neighbours``/``parent``.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Optional

__all__ = [
    "CLUSTER_THREAD_DIAG", "ROLE_NAMES", "INVALID_ROUTER_ID",
    "RSSI_WEAK", "RSSI_BAD", "FRAME_ERROR_HIGH", "PATH_COST_FAR", "PARENT_CHANGES_HIGH",
    "Neighbour", "Route", "NodeDiag", "HealthFlag", "MeshNode", "MeshLink", "Mesh",
    "parse_node_diag", "health_flags", "build_mesh", "summary_states", "render_report",
    "format_ext_address", "rloc_label", "partition_header_text",
]

# ThreadNetworkDiagnostics cluster, always read on endpoint 0.
CLUSTER_THREAD_DIAG = 0x0035

ATTR_CHANNEL = 0
ATTR_ROUTING_ROLE = 1
ATTR_NETWORK_NAME = 2
ATTR_PAN_ID = 3
ATTR_EXTENDED_PAN_ID = 4
ATTR_NEIGHBOR_TABLE = 7
ATTR_ROUTE_TABLE = 8
ATTR_PARTITION_ID = 9
ATTR_LEADER_ROUTER_ID = 13
ATTR_DETACHED_ROLE_COUNT = 14
ATTR_CHILD_ROLE_COUNT = 15
ATTR_ROUTER_ROLE_COUNT = 16
ATTR_LEADER_ROLE_COUNT = 17
ATTR_ATTACH_ATTEMPT_COUNT = 18
ATTR_PARTITION_ID_CHANGE_COUNT = 19
ATTR_BETTER_PARTITION_ATTACH_ATTEMPT_COUNT = 20
ATTR_PARENT_CHANGE_COUNT = 21
#: Provisional attributes. No node we have ever seen implements them (ground truth,
#: 2026-09-01) — never rely on a reading; only trust them if listed in AttributeList.
ATTR_EXT_ADDRESS = 0x3F
ATTR_RLOC16 = 0x40
#: Global AttributeList (0xFFFB) — the ADR-0003 capability authority. The MLECNT
#: counters (14-21) are spec-optional and gated on this, not on a raw reading.
ATTR_ATTRIBUTE_LIST = 65531

ROLE_UNSPECIFIED = 0
ROLE_UNASSIGNED = 1
ROLE_SED = 2
ROLE_ED = 3
ROLE_REED = 4
ROLE_ROUTER = 5
ROLE_LEADER = 6

ROLE_NAMES: dict[int, str] = {
    ROLE_UNSPECIFIED: "Unspecified",
    ROLE_UNASSIGNED: "Unassigned",
    ROLE_SED: "SleepyEndDevice",
    ROLE_ED: "EndDevice",
    ROLE_REED: "Reed",
    ROLE_ROUTER: "Router",
    ROLE_LEADER: "Leader",
}

#: RouteTableStruct NextHop sentinel meaning "no route" / "this row is the reader's
#: own entry, not a route via another router". Also the ceiling on real router ids
#: (0-62 are valid).
INVALID_ROUTER_ID = 63

RSSI_WEAK = -80
RSSI_BAD = -90
FRAME_ERROR_HIGH = 20
PATH_COST_FAR = 3
PARENT_CHANGES_HIGH = 5

_EXT_ADDRESS_PRECISION_LIMIT = 2 ** 53

#: MLECNT-gated counter attributes -> the NodeDiag.counters key each becomes.
_COUNTER_ATTRS: dict[int, str] = {
    ATTR_DETACHED_ROLE_COUNT: "detached_role",
    ATTR_CHILD_ROLE_COUNT: "child_role",
    ATTR_ROUTER_ROLE_COUNT: "router_role",
    ATTR_LEADER_ROLE_COUNT: "leader_role",
    ATTR_ATTACH_ATTEMPT_COUNT: "attach_attempts",
    ATTR_PARTITION_ID_CHANGE_COUNT: "partition_changes",
    ATTR_BETTER_PARTITION_ATTACH_ATTEMPT_COUNT: "better_partition_attach_attempts",
    ATTR_PARENT_CHANGE_COUNT: "parent_changes",
}

_MISSING = object()


def format_ext_address(ext_address: Optional[int]) -> str:
    """Render a Thread ExtAddress as hex text, flagged "≈" past the JS-Number
    precision limit (2**53) that matter-server's serialisation loses bits above."""
    if ext_address is None:
        return "unknown"
    prefix = "≈" if ext_address >= _EXT_ADDRESS_PRECISION_LIMIT else ""
    return f"{prefix}0x{ext_address:016X}"


def _opt_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _attr(attributes: Mapping, attr_id: int, cluster: int = CLUSTER_THREAD_DIAG, endpoint: int = 0) -> Any:
    """Read one attribute, accepting both key shapes callers hand this module:
    matter-server's raw ``get_node`` ("0/53/7" strings) and ``matter_model.NodeInfo``
    ((0, 0x35, 7) tuples)."""
    value = attributes.get((endpoint, cluster, attr_id), _MISSING)
    if value is not _MISSING:
        return value
    return attributes.get(f"{endpoint}/{cluster}/{attr_id}")


def _has_thread_cluster_data(attributes: Mapping) -> bool:
    """Whether ``attributes`` carries ANY cluster-0x35 key at all (either key
    shape), regardless of whether RoutingRole itself parsed. Distinguishes a
    genuine Wi-Fi node (no Thread cluster present — :func:`parse_node_diag`
    correctly returns ``None``) from a Thread node whose RoutingRole came back
    unparseable (#334 finding B2.3) — the latter must still surface as an
    unreadable :class:`NodeDiag`, not vanish silently."""
    for key in attributes:
        if isinstance(key, tuple) and len(key) == 3 and key[1] == CLUSTER_THREAD_DIAG:
            return True
        if isinstance(key, str):
            parts = key.split("/")
            if len(parts) == 3 and parts[1] == str(CLUSTER_THREAD_DIAG):
                return True
    return False


@dataclass(frozen=True)
class Neighbour:
    """One row of NeighborTable (attribute 7): a node this reader can hear directly."""

    ext_address: int
    age_s: int
    rloc16: int
    lqi: int
    avg_rssi: Optional[int]
    last_rssi: Optional[int]
    frame_error_pct: int
    message_error_pct: int
    rx_on_when_idle: bool
    full_thread_device: bool
    is_child: bool

    @property
    def router_id(self) -> int:
        return self.rloc16 >> 10

    @property
    def rssi(self) -> Optional[int]:
        """The representative rssi for DISPLAY (``summary_states``'
        ``threadLinkRssi``): LastRssi-preferred, because an operator watching this
        state wants the most-recent reading. This is deliberately NOT the health
        check's convention — :func:`_worst_reading` picks whichever of avg/last is
        the WORSE one, because catching a weak link is its job, whereas showing the
        current reading is this property's — AverageRssi is used only when no
        LastRssi exists."""
        return self.last_rssi if self.last_rssi is not None else self.avg_rssi


@dataclass(frozen=True)
class Route:
    """One row of RouteTable (attribute 8): a known router and how to reach it."""

    ext_address: int
    rloc16: int
    router_id: int
    next_hop: int
    path_cost: int
    lqi_in: int
    lqi_out: int
    age_s: int
    allocated: bool
    link_established: bool

    @property
    def is_self(self) -> bool:
        """Shaped like the reader's own RouteTable entry: no next hop needed, no
        path cost, allocated — AND not an established link, because a node is
        never its own radio neighbour. That last condition is what disambiguates
        it from a directly-linked neighbour router recorded with the same shape
        (verified against 0x34 in the fixture; see the module docstring)."""
        return (
            self.next_hop == INVALID_ROUTER_ID
            and self.path_cost == 0
            and self.allocated
            and not self.link_established
        )

    @property
    def reachable(self) -> bool:
        return self.link_established or self.next_hop != INVALID_ROUTER_ID


def _parse_neighbours(raw: Any) -> tuple[list[Neighbour], list[str], bool]:
    """``(neighbours, warnings, known)`` — ``known`` is False when ``raw`` is
    ``None`` (matter-server never cached this attribute) or not a list at all
    (malformed), as opposed to a genuinely empty ``[]`` (known, and means "no
    neighbours"). See the module docstring's "Absent is not empty" note (#334
    finding B2.1) — callers must not draw a health conclusion from an unknown
    table the way they safely can from a known-empty one."""
    if raw is None:
        return [], ["NeighborTable absent"], False
    if not isinstance(raw, list):
        return [], [f"NeighborTable malformed ({type(raw).__name__})"], False
    warnings: list[str] = []
    out: list[Neighbour] = []
    for index, entry in enumerate(raw):
        try:
            out.append(Neighbour(
                ext_address=int(entry["0"]),
                age_s=int(entry["1"]),
                rloc16=int(entry["2"]),
                lqi=int(entry["5"]),
                avg_rssi=_opt_int(entry.get("6")),
                last_rssi=_opt_int(entry.get("7")),
                frame_error_pct=int(entry["8"]),
                message_error_pct=int(entry["9"]),
                rx_on_when_idle=bool(entry["10"]),
                full_thread_device=bool(entry["11"]),
                is_child=bool(entry["13"]),
            ))
        except (TypeError, ValueError, KeyError):
            warnings.append(f"neighbour[{index}]: malformed struct, skipped")
    return out, warnings, True


def _parse_routes(raw: Any) -> tuple[list[Route], list[str]]:
    """``(routes, warnings)`` — same absent-vs-malformed distinction as
    :func:`_parse_neighbours`, named for RouteTable. Unlike the neighbour table,
    nothing downstream needs a ``routes_known`` bool: an absent RouteTable already
    surfaces as ``NodeDiag.router_id is None`` (the ``router_id_unknown`` flag) for
    a router, and as a normal (if route-table-empty) diag for anything else."""
    if raw is None:
        return [], ["RouteTable absent"]
    if not isinstance(raw, list):
        return [], [f"RouteTable malformed ({type(raw).__name__})"]
    warnings: list[str] = []
    out: list[Route] = []
    for index, entry in enumerate(raw):
        try:
            out.append(Route(
                ext_address=int(entry["0"]),
                rloc16=int(entry["1"]),
                router_id=int(entry["2"]),
                next_hop=int(entry["3"]),
                path_cost=int(entry["4"]),
                lqi_in=int(entry["5"]),
                lqi_out=int(entry["6"]),
                age_s=int(entry["7"]),
                allocated=bool(entry["8"]),
                link_established=bool(entry["9"]),
            ))
        except (TypeError, ValueError, KeyError):
            warnings.append(f"route[{index}]: malformed struct, skipped")
    return out, warnings


@dataclass  # pylint: disable=too-many-instance-attributes
class NodeDiag:
    """One node's ThreadNetworkDiagnostics, parsed from cache or a live read.

    ``read_error`` is only ever set while ``source == "cache"``; a successful live
    read clears it (:mod:`thread_survey`'s ``_live_refresh`` only flips ``source``
    to ``"live"`` on the success path, and every failure path it takes — timeout,
    read error, a re-parse exception — leaves ``source`` at ``"cache"`` instead).
    """

    node_id: int
    name: str
    available: bool
    role: int
    channel: Optional[int]
    network_name: str
    pan_id: Optional[int]
    ext_pan_id: Optional[int]
    partition_id: Optional[int]
    leader_router_id: Optional[int]
    neighbours: list[Neighbour]
    routes: list[Route]
    #: Only the keys actually present on this node (ADR-0003: AttributeList decides;
    #: a rev-2 IKEA node with FeatureMap 0 has none of these).
    counters: dict[str, int]
    source: Literal["cache", "live"] = "cache"
    read_error: Optional[str] = None
    parse_warnings: list[str] = field(default_factory=list)
    #: False when NeighborTable was never cached (None) or came back malformed —
    #: "we don't know", never conflated with a known-empty table (#334, B2.1).
    neighbours_known: bool = True

    @property
    def role_name(self) -> str:
        return ROLE_NAMES.get(self.role, f"Unknown({self.role})")

    @property
    def is_router(self) -> bool:
        return self.role in (ROLE_ROUTER, ROLE_LEADER)

    @property
    def router_id(self) -> Optional[int]:
        """This node's own router id, from its RouteTable self-entry.

        Only meaningful for a Router/Leader — a SED/ED/REED has no self-entry, and
        (verified against 0x40 in the fixture, a REED) its RouteTable can otherwise
        contain a row shaped exactly like one, describing a NEIGHBOUR router, not
        itself — hence the role gate. ``Route.is_self`` already requires
        ``not link_established`` (a node is never its own radio neighbour), which
        resolves the one genuine ambiguity seen live (0x34: two rows matched before
        that condition, one of them its own). If candidates still disagree after
        both, this returns ``None`` rather than guess, because a wrong guess would
        merge two different routers under one key in :func:`build_mesh` — and
        :func:`health_flags` emits a ``router_id_unknown`` warning for exactly this
        case, so it is never silent.
        """
        if not self.is_router:
            return None
        candidates = _self_entry_router_ids(self.routes)
        return candidates.pop() if len(candidates) == 1 else None

    @property
    def rloc16(self) -> Optional[int]:
        router_id = self.router_id
        return None if router_id is None else router_id << 10

    @property
    def parent(self) -> Optional[Neighbour]:
        """The neighbour that is this (non-router) node's parent: a full-thread,
        radio-on-when-idle neighbour, preferring one that is not itself our child,
        and — if several remain — the one with the best AverageRssi."""
        if self.is_router:
            return None
        candidates = [n for n in self.neighbours if n.rx_on_when_idle and n.full_thread_device]
        if not candidates:
            return None
        preferred = [n for n in candidates if not n.is_child] or candidates
        return max(preferred, key=lambda n: n.avg_rssi if n.avg_rssi is not None else -1000)

    @property
    def children(self) -> list[Neighbour]:
        return [n for n in self.neighbours if n.is_child]

    @property
    def best_rssi(self) -> Optional[int]:
        values = [n.rssi for n in self.neighbours if n.rssi is not None]
        return max(values) if values else None


def _path_cost_to_leader(diag: NodeDiag) -> Optional[int]:
    if diag.leader_router_id is None:
        return None
    for route in diag.routes:
        if route.router_id == diag.leader_router_id:
            return route.path_cost
    return None


def _self_entry_router_ids(routes: list[Route]) -> set[int]:
    """Router ids whose RouteTable row is shaped like a self-entry
    (:attr:`Route.is_self`). Shared between :attr:`NodeDiag.router_id` and
    :func:`parse_node_diag`'s ambiguity warning so the two never disagree."""
    return {route.router_id for route in routes if route.is_self}


def parse_node_diag(  # pylint: disable=too-many-locals
        node_id: int, name: str, available: bool, attributes: Mapping) -> Optional[NodeDiag]:
    """Parse one node's ThreadNetworkDiagnostics attributes into a :class:`NodeDiag`.

    Accepts both key shapes: matter-server's raw "0/53/7" strings and
    ``matter_model.NodeInfo``'s ``(0, 0x35, 7)`` tuples. Returns ``None`` only when
    NO cluster-0x35 key is present at all — a genuine Wi-Fi node, not a Thread node
    whose RoutingRole simply failed to parse (#334 finding B2.3): when any 0x35 key
    exists but RoutingRole itself is missing/unparseable, this still returns a
    :class:`NodeDiag` — role :data:`ROLE_UNSPECIFIED`, ``read_error`` naming the
    problem, so it lands in :attr:`Mesh.unreadable` instead of vanishing. Tolerates
    missing/``None``/malformed fields elsewhere: a malformed neighbour or route
    struct is skipped and noted in ``NodeDiag.parse_warnings`` rather than raising.
    """
    role = _opt_int(_attr(attributes, ATTR_ROUTING_ROLE))
    role_unreadable = False
    if role is None:
        if not _has_thread_cluster_data(attributes):
            return None
        role = ROLE_UNSPECIFIED
        role_unreadable = True

    neighbours, warnings, neighbours_known = _parse_neighbours(_attr(attributes, ATTR_NEIGHBOR_TABLE))
    routes, route_warnings = _parse_routes(_attr(attributes, ATTR_ROUTE_TABLE))
    warnings.extend(route_warnings)

    attribute_list = _attr(attributes, ATTR_ATTRIBUTE_LIST)
    counters: dict[str, int] = {}
    if isinstance(attribute_list, list):
        for attr_id, counter_name in _COUNTER_ATTRS.items():
            if attr_id in attribute_list:
                value = _opt_int(_attr(attributes, attr_id))
                if value is not None:
                    counters[counter_name] = value

    if role in (ROLE_ROUTER, ROLE_LEADER):
        self_candidates = _self_entry_router_ids(routes)
        if len(self_candidates) > 1:
            warnings.append(
                f"self-entry ambiguous: router ids {sorted(self_candidates)} all match "
                "NextHop=invalid/PathCost=0/Allocated/LinkEstablished=False"
            )

    return NodeDiag(
        node_id=int(node_id),
        name=name,
        available=bool(available),
        role=role,
        channel=_opt_int(_attr(attributes, ATTR_CHANNEL)),
        network_name=str(_attr(attributes, ATTR_NETWORK_NAME) or ""),
        pan_id=_opt_int(_attr(attributes, ATTR_PAN_ID)),
        ext_pan_id=_opt_int(_attr(attributes, ATTR_EXTENDED_PAN_ID)),
        partition_id=_opt_int(_attr(attributes, ATTR_PARTITION_ID)),
        leader_router_id=_opt_int(_attr(attributes, ATTR_LEADER_ROUTER_ID)),
        neighbours=neighbours,
        routes=routes,
        counters=counters,
        read_error="RoutingRole unreadable" if role_unreadable else None,
        parse_warnings=warnings,
        neighbours_known=neighbours_known,
    )


@dataclass(frozen=True)
class HealthFlag:
    """One diagnostic finding about a node — never a write, only an observation."""

    node_key: str
    severity: Literal["info", "warn", "bad"]
    code: str
    message: str


def _mesh_key(diag: NodeDiag) -> str:
    return f"node:0x{diag.node_id:X}"


def _router_key(router_id: int) -> str:
    """The mesh key for a router this plugin does not own — a foreign
    ``MeshNode`` created only from what a neighbour/route table says about it."""
    return f"rid:{router_id}"


def _child_key(rloc16: int) -> str:
    """The mesh key for an anonymous child identified by its own RLOC16 (only
    used when NONE of a router's children are already attributed to an owned
    device — see :func:`build_mesh`'s ``identified == 0`` branch, B5.2)."""
    return f"child:0x{rloc16:04X}"


def _worst_reading(neighbour: Neighbour) -> tuple[Optional[int], Optional[str]]:
    """The worse (more negative, i.e. weaker) of a neighbour's AverageRssi and
    LastRssi, and which field it came from — used only by the ``weak_link`` health
    check, which needs to catch whichever of the two readings is bad. Either may be
    null; both null returns ``(None, None)``. See the module docstring: this is
    deliberately NOT the same convention as :attr:`Neighbour.rssi` (display)."""
    avg, last = neighbour.avg_rssi, neighbour.last_rssi
    if avg is None and last is None:
        return None, None
    if avg is None:
        return last, "last"
    if last is None:
        return avg, "avg"
    return (last, "last") if last <= avg else (avg, "avg")


def _worst_link_reading(diag: NodeDiag) -> tuple[Optional[int], Optional[str]]:
    """The rssi (and which field) the ``weak_link`` check should judge: the best
    neighbour's worst reading for a router (any one decent link is fine), the
    parent's worst reading for a non-router."""
    if diag.is_router:
        readings = [_worst_reading(n) for n in diag.neighbours]
        readings = [r for r in readings if r[0] is not None]
        return max(readings, key=lambda r: r[0]) if readings else (None, None)
    parent = diag.parent
    return _worst_reading(parent) if parent is not None else (None, None)


def health_flags(diag: NodeDiag) -> list[HealthFlag]:  # pylint: disable=too-many-branches
    """Derive health flags for one node. Never raises: every check degrades to
    "no data, no flag" rather than assuming the worst or the best.

    Two gates worth calling out (#334 post-review):

    * ``neighbours_known`` (B2.1) — when NeighborTable was never cached or came
      back malformed, this emits ONE ``info`` flag ("neighbour table not yet
      available") instead of ``weak_link``/``single_neighbour``/``no_parent``,
      because "we don't know" and "there is nothing there" must never look the
      same to whoever reads these flags.
    * Non-router with no parent (B5.1 — CRITICAL, previously unreachable: the
      zero-neighbour check lived only inside the router branch) — a SED/ED/REED
      whose NeighborTable is known but empty (or has no full-thread,
      radio-on-when-idle candidate) cannot reach the mesh at all, and used to
      report clean silence rather than ``bad``.
    """
    key = _mesh_key(diag)
    flags: list[HealthFlag] = []

    if diag.role in (ROLE_UNSPECIFIED, ROLE_UNASSIGNED) or not diag.available:
        flags.append(HealthFlag(
            key, "bad", "detached",
            f"{diag.name}: detached (role={diag.role_name}, available={diag.available})",
        ))

    if diag.is_router and diag.router_id is None:
        flags.append(HealthFlag(
            key, "warn", "router_id_unknown",
            f"{diag.name}: its router id could not be resolved from its route table; "
            "a 'Router N' box on the map may be this device",
        ))

    if not diag.neighbours_known:
        flags.append(HealthFlag(
            key, "info", "neighbours_unknown", f"{diag.name}: neighbour table not yet available",
        ))
    else:
        rssi, which = _worst_link_reading(diag)
        if rssi is not None:
            label = f"{which}Rssi" if which else "rssi"
            if rssi < RSSI_BAD:
                flags.append(HealthFlag(key, "bad", "weak_link", f"{diag.name}: link at {rssi} dBm ({label})"))
            elif rssi < RSSI_WEAK:
                flags.append(HealthFlag(key, "warn", "weak_link", f"{diag.name}: link at {rssi} dBm ({label})"))

        for neighbour in diag.neighbours:
            if neighbour.frame_error_pct > FRAME_ERROR_HIGH:
                flags.append(HealthFlag(
                    key, "warn", "high_frame_error",
                    f"{diag.name}: {neighbour.frame_error_pct}% frame errors to 0x{neighbour.rloc16:04X}",
                ))

        if diag.is_router:
            neighbour_count = len(diag.neighbours)
            if neighbour_count == 0:
                flags.append(HealthFlag(key, "bad", "single_neighbour", f"{diag.name}: no neighbours"))
            elif neighbour_count == 1:
                flags.append(HealthFlag(key, "warn", "single_neighbour", f"{diag.name}: only one neighbour"))
        elif diag.parent is None:
            flags.append(HealthFlag(
                key, "bad", "no_parent", f"{diag.name}: no parent router — the device cannot reach the mesh",
            ))

    path_cost = _path_cost_to_leader(diag)
    if path_cost is not None and path_cost >= PATH_COST_FAR:
        flags.append(HealthFlag(
            key, "warn", "far_from_leader", f"{diag.name}: {path_cost} hops from the leader",
        ))

    parent_changes = diag.counters.get("parent_changes")
    if parent_changes is not None and parent_changes >= PARENT_CHANGES_HIGH:
        flags.append(HealthFlag(
            key, "warn", "unstable_parent", f"{diag.name}: {parent_changes} parent changes",
        ))

    # "Sleepy/live-read candidate" is defined ONCE, by thread_survey's own
    # criterion (NodeDiag.is_router) — a REED is exactly as live-read-worthy as a
    # SED/ED, so it must get the same staleness note a cached reading deserves
    # (#334 finding B5.7; this used to name only SED/ED and silently skip REEDs).
    if diag.source == "cache" and not diag.is_router:
        flags.append(HealthFlag(
            key, "info", "stale",
            f"{diag.name}: cached — matter-server's cache can be hours stale for sleepy devices",
        ))

    return flags


@dataclass(frozen=True)
class MeshNode:  # pylint: disable=too-many-instance-attributes
    """One vertex of the mesh: an owned node, a foreign router, or an anonymous
    unidentified child. ``key`` namespaces which: ``node:0x..``, ``rid:N``,
    ``child:0x..`` or ``child:node:0x..#unattributed`` — see the module
    docstring's decision 1 and :func:`build_mesh`'s B5.2 note."""

    key: str
    node_id: Optional[int]
    name: str
    role_name: str
    router_id: Optional[int]
    rloc16: Optional[int]
    foreign: bool
    is_leader: bool
    partition_id: Optional[int]
    children_count: int


@dataclass(frozen=True)
class MeshLink:  # pylint: disable=too-many-instance-attributes
    """One edge of the mesh, from the reporting node's own point of view."""

    a: str
    b: str
    kind: Literal["parent", "router", "child"]
    avg_rssi: Optional[int]
    last_rssi: Optional[int]
    lqi: int
    frame_error_pct: int
    seen_from: str


@dataclass(frozen=True)
class Mesh:  # pylint: disable=too-many-instance-attributes
    """The whole fabric's Thread picture, assembled by :func:`build_mesh`."""

    network_name: str
    channel: Optional[int]
    pan_id: Optional[int]
    #: Sorted distinct non-None partition ids reported by ANY diag (cache or
    #: live) — display-only ("Partition(s)" row); the majority/consensus
    #: reading is ``majority_partition`` below (#334 finding B1.3).
    partition_ids: list[int]
    #: The consensus PartitionId, computed once from the live pool when any
    #: diag is live-sourced, else from all diags — ``None`` on an outright tie
    #: (no majority), never guessed (B1.3 + B3.4).
    majority_partition: Optional[int]
    #: The consensus LeaderRouterId across all diags — ``None`` on a tie.
    majority_leader: Optional[int]
    nodes: list[MeshNode]
    links: list[MeshLink]
    flags: list[HealthFlag]
    unreadable: list[tuple[int, str]]
    disagreements: list[str]
    #: (node_id, message) — per-node parse warnings (B2.5) plus mesh-level notes
    #: with no single owning node_id of their own, e.g. the unattributed-children
    #: note (B5.2), keyed to the reporting router's node_id.
    warnings: list[tuple[int, str]]


def _majority(values: Iterable[int]) -> Optional[int]:
    """The most common value, or ``None`` on an empty input OR an outright tie.

    ``Counter.most_common``'s own tie-break is first-seen order, which would
    silently pick a "winner" that is not one — returning ``None`` here forces
    every caller to say "no majority" instead of asserting a winner that does
    not exist (#334 finding B3.4)."""
    counted = Counter(values)
    if not counted:
        return None
    common = counted.most_common()
    if len(common) > 1 and common[0][1] == common[1][1]:
        return None
    return common[0][0]


def rloc_label(mesh: Mesh, rloc16: int) -> Optional[str]:
    """A human name for ``rloc16`` if some :class:`MeshNode` in ``mesh`` claims
    it, else ``None`` so the caller can fall back to the bare hex (#334 finding
    A2): "to 0x3800" means nothing to a human reading a report or a flag message;
    "to Router 14 (0x3800)" does. The raw rloc is always kept in the caller's own
    formatting, in parentheses — this function only supplies the name half.

    Trimmed at the first " (" — a foreign leader's :attr:`MeshNode.name` already
    carries a "(leader — probably your border router)" suffix, which belongs on
    that node's OWN line (the report's "Foreign routers" section, the page's node
    box) but would blow a 120-char report line's budget repeated on every inline
    mention of it.
    """
    for node in mesh.nodes:
        if node.rloc16 == rloc16:
            return node.name.split(" (", 1)[0]
    return None


_RLOC_TO_PATTERN = re.compile(r"to (0x[0-9A-Fa-f]{4})\b")


def _resolve_rloc_mentions(message: str, nodes: list[MeshNode]) -> str:
    """Rewrite every "to 0xRLOC" in a health-flag message to "to NAME (0xRLOC)"
    when the mesh can name that rloc (#334 finding A2) — post-processed here,
    once the mesh's nodes exist, rather than inside :func:`health_flags` itself,
    which runs per-diag before any mesh (and therefore any name) exists."""
    by_rloc = {node.rloc16: node.name.split(" (", 1)[0] for node in nodes if node.rloc16 is not None}

    def _sub(match: "re.Match[str]") -> str:
        hex_text = match.group(1)
        name = by_rloc.get(int(hex_text, 16))
        return f"to {name} ({hex_text})" if name else match.group(0)

    return _RLOC_TO_PATTERN.sub(_sub, message)


def _worst_of(children: list[Neighbour]) -> Neighbour:
    """The representative child for an aggregated unattributed-children group's
    single displayed link (B5.2): the WORST individual reading among them, so the
    link's colour/label are order-independent — the same result regardless of
    NeighborTable iteration order, unlike picking "whichever came first". Falls
    back to sorting by rloc16 (still deterministic, if arbitrary) when none of
    the excess children has any rssi reading at all."""
    with_reading = [(child, _worst_reading(child)[0]) for child in children]
    have_reading = [(child, value) for child, value in with_reading if value is not None]
    if have_reading:
        return min(have_reading, key=lambda pair: pair[1])[0]
    return min(children, key=lambda child: child.rloc16)


def build_mesh(diags: Iterable[NodeDiag]) -> Mesh:
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Assemble owned + foreign routers, links, health flags and partition
    disagreements from a set of parsed nodes. Never raises on a partial/degraded
    node — an unresolvable router id, an empty neighbour table or a cache/live
    mismatch all resolve to a flag or a disagreement, never a crash."""
    diags = list(diags)

    network_name, channel, pan_id = "", None, None
    for diag in diags:
        network_name = network_name or diag.network_name
        channel = channel if channel is not None else diag.channel
        pan_id = pan_id if pan_id is not None else diag.pan_id

    live = [d for d in diags if d.source == "live"]
    partition_pool = live or diags
    partition_ids = sorted({d.partition_id for d in diags if d.partition_id is not None})
    majority_partition = _majority(d.partition_id for d in partition_pool if d.partition_id is not None)
    majority_leader = _majority(d.leader_router_id for d in diags if d.leader_router_id is not None)

    owned_key_by_router_id: dict[int, str] = {}
    for diag in diags:
        if diag.router_id is not None:
            owned_key_by_router_id[diag.router_id] = _mesh_key(diag)

    foreign_ids: set[int] = set()
    for diag in diags:
        for neighbour in diag.neighbours:
            if neighbour.router_id not in owned_key_by_router_id:
                foreign_ids.add(neighbour.router_id)
        for route in diag.routes:
            if route.allocated and route.router_id not in owned_key_by_router_id:
                foreign_ids.add(route.router_id)

    def target_key(router_id: int) -> str:
        return owned_key_by_router_id.get(router_id, _router_key(router_id))

    links: list[MeshLink] = []
    #: router mesh key -> the set of THAT router's own children's RLOC16s that
    #: some owned SED/ED has actually claimed as its parent — identity-based
    #: (not "however many owned children exist"), so which specific
    #: NeighborTable entries count as "identified" cannot depend on iteration
    #: order (#334 finding B5.2 — see the anonymous-children block below).
    identified_child_rlocs: dict[str, set[int]] = {}
    for diag in diags:
        own_key = _mesh_key(diag)
        if diag.is_router:
            for neighbour in diag.neighbours:
                if neighbour.is_child:
                    continue
                links.append(MeshLink(
                    a=own_key, b=target_key(neighbour.router_id), kind="router",
                    avg_rssi=neighbour.avg_rssi, last_rssi=neighbour.last_rssi,
                    lqi=neighbour.lqi, frame_error_pct=neighbour.frame_error_pct, seen_from=own_key,
                ))
        else:
            parent = diag.parent
            if parent is not None:
                parent_key = target_key(parent.router_id)
                links.append(MeshLink(
                    a=own_key, b=parent_key, kind="parent",
                    avg_rssi=parent.avg_rssi, last_rssi=parent.last_rssi,
                    lqi=parent.lqi, frame_error_pct=parent.frame_error_pct, seen_from=own_key,
                ))
                identified_child_rlocs.setdefault(parent_key, set()).add(parent.rloc16)

    # Children a router's own NeighborTable reports that no owned SED/ED claims as
    # its parent become anonymous nodes for the excess only — never phantom nodes
    # for children we can already attribute to a real owned device. Excess is
    # computed by RLOC identity (identified_child_rlocs above), not by slicing
    # the first N entries off the list: a positional slice would make "which
    # children count as identified" depend on NeighborTable iteration order,
    # exactly the coin flip this fix exists to remove. Which RLOC becomes which
    # individual "child:0x.." node is only safe to show when NONE of this
    # router's children are attributed yet (identified == 0): the moment even
    # one is, the rest are collapsed into one "N unattributed child(ren)"
    # placeholder instead, because which specific RLOC a future survey matches
    # next is not something this module can promise ahead of time (B5.2).
    anonymous_nodes: list[MeshNode] = []
    mesh_warnings: list[tuple[int, str]] = []
    for diag in diags:
        if not diag.is_router or not diag.children:
            continue
        own_key = _mesh_key(diag)
        claimed_rlocs = identified_child_rlocs.get(own_key, set())
        identified = sum(1 for child in diag.children if child.rloc16 in claimed_rlocs)
        excess = [child for child in diag.children if child.rloc16 not in claimed_rlocs]
        if not excess:
            continue
        if identified == 0:
            for child in excess:
                child_key = _child_key(child.rloc16)
                anonymous_nodes.append(MeshNode(
                    key=child_key, node_id=None, name=f"Child 0x{child.rloc16:04X}", role_name="Child",
                    router_id=None, rloc16=child.rloc16, foreign=False, is_leader=False,
                    partition_id=None, children_count=0,
                ))
                links.append(MeshLink(
                    a=child_key, b=own_key, kind="child",
                    avg_rssi=child.avg_rssi, last_rssi=child.last_rssi,
                    lqi=child.lqi, frame_error_pct=child.frame_error_pct, seen_from=own_key,
                ))
        else:
            group_key = f"child:{own_key}#unattributed"
            anonymous_nodes.append(MeshNode(
                key=group_key, node_id=None, name=f"{len(excess)} unattributed child(ren)",
                role_name="Child", router_id=None, rloc16=None, foreign=False, is_leader=False,
                partition_id=None, children_count=0,
            ))
            worst = _worst_of(excess)
            links.append(MeshLink(
                a=group_key, b=own_key, kind="child",
                avg_rssi=worst.avg_rssi, last_rssi=worst.last_rssi,
                lqi=worst.lqi, frame_error_pct=worst.frame_error_pct, seen_from=own_key,
            ))
            router_text = diag.router_id if diag.router_id is not None else "?"
            mesh_warnings.append((diag.node_id, (
                f"router {router_text}: {len(excess)} of {len(diag.children)} child entries could not "
                "be matched to an Indigo device; RLOCs not shown because they may belong to a device "
                "already listed"
            )))

    def children_count(key: str) -> int:
        return sum(1 for link in links if link.kind in ("parent", "child") and link.b == key)

    nodes: list[MeshNode] = []
    for diag in diags:
        own_key = _mesh_key(diag)
        router_id = diag.router_id
        nodes.append(MeshNode(
            key=own_key, node_id=diag.node_id, name=diag.name, role_name=diag.role_name,
            router_id=router_id, rloc16=diag.rloc16, foreign=False,
            is_leader=(router_id is not None and router_id == majority_leader),
            partition_id=diag.partition_id, children_count=children_count(own_key),
        ))

    for router_id in sorted(foreign_ids):
        key = _router_key(router_id)
        is_leader = router_id == majority_leader
        suffix = " (leader — probably your border router)" if is_leader else ""
        nodes.append(MeshNode(
            key=key, node_id=None, name=f"Router {router_id}{suffix}", role_name="Router",
            router_id=router_id, rloc16=router_id << 10, foreign=True, is_leader=is_leader,
            partition_id=None, children_count=children_count(key),
        ))

    nodes.extend(anonymous_nodes)

    flags: list[HealthFlag] = []
    for diag in diags:
        flags.extend(health_flags(diag))
    # A2: resolve RLOC mentions in flag messages to names now that ``nodes``
    # exists — health_flags itself runs per-diag before any mesh does.
    flags = [
        HealthFlag(f.node_key, f.severity, f.code, _resolve_rloc_mentions(f.message, nodes))
        for f in flags
    ]

    disagreements: list[str] = []
    if majority_partition is not None:
        for diag in diags:
            if diag.partition_id is not None and diag.partition_id != majority_partition:
                label = "live" if diag.source == "live" else "cached"
                disagreements.append(
                    f"0x{diag.node_id:X} reports partition {diag.partition_id} ({label}); "
                    f"majority is {majority_partition}"
                )
    elif len(partition_ids) > 1:
        # No majority at all (a tie) — every node's reading is listed, since
        # there is no majority for any of them to be an "outlier" from (B3.4).
        for diag in diags:
            if diag.partition_id is not None:
                label = "live" if diag.source == "live" else "cached"
                disagreements.append(f"0x{diag.node_id:X} reports partition {diag.partition_id} ({label})")

    unreadable = [(d.node_id, d.read_error) for d in diags if d.read_error]

    warnings: list[tuple[int, str]] = []
    for diag in diags:
        for parse_warning in diag.parse_warnings:
            warnings.append((diag.node_id, parse_warning))
    warnings.extend(mesh_warnings)

    return Mesh(
        network_name=network_name, channel=channel, pan_id=pan_id,
        partition_ids=partition_ids, majority_partition=majority_partition, majority_leader=majority_leader,
        nodes=nodes, links=links, flags=flags, unreadable=unreadable, disagreements=disagreements,
        warnings=warnings,
    )


def summary_states(diag: NodeDiag) -> dict[str, Any]:
    """The ``matterNode`` Thread states (ADR-0008 — only what a trigger would
    bind to; channel/network/partition live on the mesh report and page instead).

    When :attr:`NodeDiag.neighbours_known` is False, returns ONLY ``threadRole``
    — writing ``threadHealth``/``threadNeighbourCount``/``threadLinkRssi`` from
    an unknown neighbour table would either invent "0 neighbours" or invent
    "ok", and both are worse than leaving the state untouched until priming
    catches up (#334 finding B2.1). Likewise ``threadLinkRssi`` is omitted
    (never written as a sentinel) whenever no rssi reading exists at all, known
    table or not (B2.9) — a magic ``-128`` used to trip numeric triggers.
    """
    if not diag.neighbours_known:
        return {"threadRole": diag.role_name}

    flags = [f for f in health_flags(diag) if f.severity != "info"]
    if any(f.severity == "bad" for f in flags):
        health = "bad"
    elif any(f.severity == "warn" for f in flags):
        health = "warn"
    else:
        health = "ok"

    if diag.is_router:
        rssi = diag.best_rssi
    else:
        parent = diag.parent
        rssi = parent.rssi if parent else None

    states: dict[str, Any] = {
        "threadRole": diag.role_name,
        "threadNeighbourCount": len(diag.neighbours),
        "threadHealth": health,
    }
    if rssi is not None:
        states["threadLinkRssi"] = rssi
    return states


def partition_header_text(mesh: Mesh, diags: Iterable[NodeDiag]) -> str:
    """The "Partition(s)" summary line shared by the event-log report and the
    IWS page (#334 finding A1). The OLD text listed every partition id ever
    seen, which reads as a three-way split even when six of seven nodes agree
    and the outliers are simply stale cache — the exact misreading a live read
    disproved on 2026-09-01. Says "split" only when the disagreement is real:
    either no majority exists at all (a tie), or two-plus LIVE-read nodes
    themselves disagree; a cached node contradicting a live majority is
    staleness, not a split, and is reported as a count instead.
    """
    diags = list(diags)
    live_partition_ids = sorted({
        d.partition_id for d in diags if d.source == "live" and d.partition_id is not None
    })

    if mesh.majority_partition is None:
        if not mesh.partition_ids:
            return "Partition(s): unknown"
        ids = ", ".join(str(p) for p in mesh.partition_ids)
        return (f'Partition(s): split — no majority among {len(mesh.partition_ids)} partitions: {ids}, '
                f'see "Unreadable / stale"')
    if len(live_partition_ids) >= 2:
        ids = ", ".join(str(p) for p in live_partition_ids)
        return (f'Partition(s): split — {len(live_partition_ids)} live-read partitions disagree: {ids}, '
                f'see "Unreadable / stale"')

    leader_text = f"leader router {mesh.majority_leader}" if mesh.majority_leader is not None else "leader unknown"
    disagreement_count = len(mesh.disagreements)
    if disagreement_count:
        return (f"Partition {mesh.majority_partition} ({leader_text}) — {disagreement_count} cached node(s) "
                f'report a different partition, see "Unreadable / stale"')
    return f"Partition {mesh.majority_partition} ({leader_text})"


def _node_lines(diag: NodeDiag, mesh: Mesh) -> list[str]:
    lines = [f"-- {diag.name} (0x{diag.node_id:X}) -- role={diag.role_name} available={diag.available}"]
    if diag.router_id is not None:
        lines.append(f"   router_id={diag.router_id} rloc16=0x{diag.rloc16:04X}")
    lines.append(f"   partition={diag.partition_id} leader_router_id={diag.leader_router_id}")
    if diag.counters:
        for name, value in sorted(diag.counters.items()):
            lines.append(f"   counter {name}={value}")
    for neighbour in diag.neighbours:
        role = "child" if neighbour.is_child else "peer"
        ext = format_ext_address(neighbour.ext_address)
        name = rloc_label(mesh, neighbour.rloc16)
        target = f"{name} (0x{neighbour.rloc16:04X})" if name else f"0x{neighbour.rloc16:04X}"
        lines.append(
            f"   neighbour {target} ({role}): rssi={neighbour.rssi} lqi={neighbour.lqi} "
            f"frame_err={neighbour.frame_error_pct}% age={neighbour.age_s}s ext={ext}"
        )
    if not diag.neighbours:
        lines.append("   neighbour table: empty" if diag.neighbours_known else "   neighbour table: unknown")
    for warning in diag.parse_warnings:
        lines.append(f"   ! {warning}")
    return lines


def render_report(mesh: Mesh, diags: list[NodeDiag], *, page_url: Optional[str] = None) -> list[str]:
    """Event-log lines: header, one block per owned node, foreign routers, then
    FLAGS, then UNREADABLE, then (#339) an optional footer linking the visual
    map. Fixed-width, no colour, kept to <=120 chars/line.

    ``page_url`` is passed in rather than resolved here, keeping this module
    pure (no ``indigo`` import, no I/O — its own module docstring's whole
    point). The only caller is ``diagnostics_menu_mixin.menuReportThreadMesh``
    (via ``HttpApiMixin._iws_page_url``); the IWS Thread page never calls this
    function at all — it renders its own HTML (``thread_page.py``) — so
    omitting ``page_url`` (``None``, the default) is what every other test of
    this function already does, and simply omits the footer line.
    """
    lines = [
        f"Thread mesh: {mesh.network_name or 'unknown'}  "
        f"channel {mesh.channel if mesh.channel is not None else '?'}  "
        f"PAN {f'0x{mesh.pan_id:04X}' if mesh.pan_id is not None else '?'}",
        partition_header_text(mesh, diags),
    ]

    for diag in diags:
        lines.append("")
        lines.extend(_node_lines(diag, mesh))

    foreign = [n for n in mesh.nodes if n.foreign]
    if foreign:
        lines.append("")
        lines.append("-- Foreign routers --")
        for node in foreign:
            lines.append(f"   {node.name} (router_id={node.router_id}) children={node.children_count}")

    lines.append("")
    lines.append("-- Flags --")
    if mesh.flags:
        for flag in sorted(mesh.flags, key=lambda f: {"bad": 0, "warn": 1, "info": 2}.get(f.severity, 3)):
            lines.append(f"   [{flag.severity.upper():4s}] {flag.code}: {flag.message}")
    else:
        lines.append("   none")

    if mesh.disagreements:
        lines.append("")
        lines.append("-- Partition disagreements --")
        for disagreement in mesh.disagreements:
            lines.append(f"   {disagreement}")

    if mesh.unreadable:
        lines.append("")
        lines.append("-- Unreadable --")
        for node_id, reason in mesh.unreadable:
            lines.append(f"   0x{node_id:X}: {reason}")

    if page_url:
        lines.append("")
        lines.append(f"Visual map: {page_url}  (add ?live=1 for a live refresh)")

    return lines
