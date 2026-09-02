"""The Thread mesh IWS page's HTML (#334; radial map #346) — pure rendering,
no I/O, no ``indigo`` import. Mirrors :mod:`pairing_page`'s shape: the only
caller is :meth:`HttpApiMixin.http_thread_page`, which builds the live
:class:`~thread_mesh.Mesh` (via :func:`thread_survey.run_survey` +
:func:`thread_mesh.build_mesh`) and hands it here to render.

Self-contained HTML + CSS + an inline SVG map computed entirely in Python — no
JS, no external assets, so it renders in Safari on a phone over the Reflector.
The map (:func:`_layout`) is radial, styled after Apple's Thread Doctor app
(Simon's own reference, approved 2026-09-02): the fabric's leader (owned or
foreign) sits at the centre, every other router-layer node — owned routers,
foreign routers, and any non-router one hop off the leader — forms ring 1
around it, and every remaining non-router clusters on ring 2 beside whichever
ring-1 node it links to. Geometry is computed nowhere but here, and never a
write of any kind (ADR-0004: this page only ever reads).

Everything rendered here — node names above all, which come straight from
Indigo device names — is treated as hostile input and passed through
:func:`_escape`.
"""
from __future__ import annotations

import itertools
import math
from datetime import datetime, timezone
from typing import Any, Optional

from thread_mesh import _aware, freshness_text, humanise_age, partition_header_text

__all__ = ["render_thread_page"]

#: #344 UX (Simon's call, 2026-09-02): CACHED stays the page's default — old
#: data is HIGHLIGHTED, not hidden. An interview baseline older than this is
#: "stale enough to flag": the per-node cell gets the amber chip, and (when
#: it is the oldest such baseline on the page) the header/banner note it.
_STALE_AGE_SECONDS = 3600.0

_MARGIN = 28
#: Extra room left outside ring 2 for its nodes' outward-drawn names (#346).
_LABEL_ROOM = 140.0
_RING1_MIN_R = 150.0
_RING1_TO_RING2_GAP = 130.0
#: The "2r + 8" spacing floor from the layout spec — the extra 8 units is
#: pure breathing room between two adjacent glyphs' edges, on top of the
#: `+4` the overlap test itself demands (#346).
_MIN_GLYPH_GAP = 8.0

#: Glyph sizes (#346 §2) — radius for a circle, half-side for a square. This
#: IS the ``r`` a :func:`_layout` caller gets back, used both for edge
#: shortening and the overlap/spacing maths above.
_GLYPH_R_LEADER = 30.0
_GLYPH_R_ROUTER = 26.0
_GLYPH_R_FOREIGN = 24.0
_GLYPH_R_REED = 16.0
_GLYPH_R_ED = 12.0
_GLYPH_R_CHILD = 10.0
_GLYPH_R_UNKNOWN = 12.0

#: Ring-1 ordering (#346 §1): exhaustive permutation search below this many
#: nodes (<= 5040 permutations with the first node fixed), a greedy walk
#: above it.
_EXHAUSTIVE_RING1_LIMIT = 8

# Display bands (#346) — the MAP's own thresholds for colouring an edge line,
# deliberately separate from thread_mesh.RSSI_WEAK/RSSI_BAD (-80/-90). Those
# two still drive `threadHealth` and every health_flags a node actually
# gets; these four exist only to make the lines readable at a glance, the
# same four bands Apple's Thread Doctor uses. The two scales are allowed to
# disagree — a -78 dBm link is still "good" for health (>= RSSI_WEAK) but
# reads "fair" here — because they answer different questions: "should this
# raise a flag" versus "how does this look on the map".
_BAND_EXCELLENT = -65
_BAND_GOOD = -77
_BAND_FAIR = -87

_BAND_COLOR_VAR = {
    "excellent": "var(--edge-excellent)",
    "good": "var(--edge-good)",
    "fair": "var(--edge-fair)",
    "poor": "var(--edge-poor)",
    "unknown": "var(--edge-unread)",
}


def _escape(text: Any) -> str:
    """Minimal HTML escaping, identical to :func:`pairing_page._escape`: a
    ``None`` becomes "", never the literal word "None". Node names on this page
    come straight from Indigo device names and are hostile input."""
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _fmt_opt(value: Any, *, suffix: str = "") -> str:
    return "—" if value is None else f"{value}{suffix}"


def _rssi_band(rssi: Optional[int]) -> str:
    if rssi is None:
        return "unknown"
    if rssi >= _BAND_EXCELLENT:
        return "excellent"
    if rssi >= _BAND_GOOD:
        return "good"
    if rssi >= _BAND_FAIR:
        return "fair"
    return "poor"


def _edge_color(rssi: Optional[int]) -> str:
    return _BAND_COLOR_VAR[_rssi_band(rssi)]


def _severity_rank(severity: str) -> int:
    return {"bad": 0, "warn": 1, "info": 2}.get(severity, 3)


# ---------------------------------------------------------------------------
# Link merging — a link between two routers is reported from BOTH sides
# (build_mesh adds one MeshLink per reporting node), so the page shows ONE
# edge per pair, using the worse of the two sides' readings for colour and
# both readings in the label when they disagree (#334).
# ---------------------------------------------------------------------------

def _link_worst_reading(avg_rssi: Optional[int], last_rssi: Optional[int]) -> Optional[int]:
    values = [v for v in (avg_rssi, last_rssi) if v is not None]
    return min(values) if values else None


def _merged_links(links) -> list[dict]:
    groups: dict[frozenset, list] = {}
    for link in links:
        groups.setdefault(frozenset((link.a, link.b)), []).append(link)

    merged = []
    for entries in groups.values():
        readings = []
        for entry in entries:
            worst = _link_worst_reading(entry.avg_rssi, entry.last_rssi)
            if worst is not None:
                readings.append(worst)
        distinct = sorted(set(readings), reverse=True)
        overall = min(readings) if readings else None
        # The lqi/frame-error shown come from whichever side reported the
        # overall-worst rssi — the SAME side the colour is judged against —
        # falling back to the first entry when every side's rssi is unknown.
        representative = entries[0]
        for entry in entries:
            if _link_worst_reading(entry.avg_rssi, entry.last_rssi) == overall:
                representative = entry
                break
        merged.append({
            "a": entries[0].a,
            "b": entries[0].b,
            "kind": entries[0].kind,
            "rssi_values": distinct,
            "overall_rssi": overall,
            "lqi": representative.lqi,
            "frame_error_pct": representative.frame_error_pct,
        })
    return merged


def _link_label(link: dict) -> str:
    if link["rssi_values"]:
        rssi_text = "/".join(str(v) for v in link["rssi_values"]) + " dBm"
    else:
        rssi_text = "RSSI unknown"
    return f'{rssi_text} · LQI {link["lqi"]} · {link["frame_error_pct"]}% err'


# ---------------------------------------------------------------------------
# Layout — radial (#346): the leader at the centre, every other router-layer
# node on ring 1 around it, every remaining non-router on ring 2 beside
# whichever ring-1 node it links to. Pure function of the mesh alone.
# ---------------------------------------------------------------------------

def _is_router_node(node) -> bool:
    return node.foreign or node.role_name in ("Router", "Leader")


_ROLE_GLYPH_R = {
    "Router": _GLYPH_R_ROUTER, "Reed": _GLYPH_R_REED, "EndDevice": _GLYPH_R_ED,
    "SleepyEndDevice": _GLYPH_R_ED, "Child": _GLYPH_R_CHILD,
}


def _glyph_r(node) -> float:
    if node.foreign:
        return _GLYPH_R_FOREIGN
    if node.is_leader:
        return _GLYPH_R_LEADER
    return _ROLE_GLYPH_R.get(node.role_name, _GLYPH_R_UNKNOWN)


def _ring1_and_ring2(mesh):
    """``(leader, ring1_members, ring2, linked_parent)`` — ring 1 here is
    UNORDERED (see :func:`_order_ring1` for the crossing-minimised order).

    Every non-router keyed by whichever router-layer node its own link (kind
    "parent" or "child") points at — never a phantom parent guess: a
    non-router with no such link (a detached node, or a link to a node this
    page could not place) becomes an orphan, laid out in its own trailing
    ring-2 sector.

    A non-router whose parent IS the leader (a REED/SED one hop off the
    leader, same as a router is) belongs on ring 1 beside the routers, not
    clustered on ring 2 (#334 finding 2, kept here): one hop off the leader
    is one hop regardless of whether the far end is a router or not.
    """
    nodes = list(mesh.nodes)
    leader = next((n for n in nodes if n.is_leader), None)
    routers = [n for n in nodes if _is_router_node(n) and n is not leader]
    non_routers = [n for n in nodes if not _is_router_node(n)]
    non_router_keys = {n.key for n in non_routers}

    linked_parent: dict[str, str] = {}
    for link in mesh.links:
        if link.kind in ("parent", "child") and link.a in non_router_keys:
            linked_parent[link.a] = link.b

    leader_children = [n for n in non_routers if leader is not None and linked_parent.get(n.key) == leader.key]
    leader_child_keys = {n.key for n in leader_children}
    ring1_members = routers + leader_children
    ring2 = [n for n in non_routers if n.key not in leader_child_keys]
    return leader, ring1_members, ring2, linked_parent


def _chord_pairs(ring1_keys: set, links) -> list[tuple[str, str]]:
    """Merged router-kind links between two ring-1 nodes — the "chords" ring-1
    ordering tries to minimise the crossings of. A link to the leader (radial,
    not on the ring) or to a ring-2 node is never a chord."""
    pairs = []
    for merged in _merged_links(links):
        if merged["kind"] != "router":
            continue
        a, b = merged["a"], merged["b"]
        if a in ring1_keys and b in ring1_keys:
            pairs.append((a, b))
    return pairs


def _chords_cross(chord_a: tuple[int, int], chord_b: tuple[int, int]) -> bool:
    """Two chords of a ring, given as index pairs, cross iff their endpoints
    interleave going around the ring — the standard circle-chord test."""
    a, b = sorted(chord_a)
    c, d = sorted(chord_b)
    return a < c < b < d or c < a < d < b


def _count_crossings(order: list, chords: list[tuple[str, str]]) -> int:
    position = {node.key: i for i, node in enumerate(order)}
    indexed = [(position[a], position[b]) for a, b in chords]
    total = 0
    for i, chord_i in enumerate(indexed):
        for chord_j in indexed[i + 1:]:
            if _chords_cross(chord_i, chord_j):
                total += 1
    return total


def _greedy_ring1_order(canonical: list, chords: list[tuple[str, str]]) -> list:
    """Rings too big for exhaustive search (#346 §1): start from the
    most-connected node, then repeatedly append whichever unplaced node has
    the most links to the already-placed set, tie-breaking `(foreign, name)`
    — the same tie-break the exhaustive path uses, so both are deterministic
    for the same reason."""
    by_key = {n.key: n for n in canonical}
    degree: dict[str, int] = {n.key: 0 for n in canonical}
    for a, b in chords:
        degree[a] += 1
        degree[b] += 1
    start = sorted(canonical, key=lambda n: (-degree[n.key], n.foreign, n.name))[0]

    order = [start]
    placed = {start.key}
    remaining = [n.key for n in canonical if n.key != start.key]
    while remaining:
        def _score(key: str) -> tuple:
            linked = sum(1 for a, b in chords if (a == key and b in placed) or (b == key and a in placed))
            node = by_key[key]
            return -linked, node.foreign, node.name

        remaining.sort(key=_score)
        next_key = remaining.pop(0)
        order.append(by_key[next_key])
        placed.add(next_key)
    return order


def _order_ring1(members: list, chords: list[tuple[str, str]]) -> list:
    """Deterministic ring-1 order minimising chord crossings (#346 §1). Same
    mesh, same chords, always yields the same order — no randomness anywhere."""
    if len(members) <= 1:
        return list(members)
    canonical = sorted(members, key=lambda n: (n.foreign, n.name))
    if len(canonical) > _EXHAUSTIVE_RING1_LIMIT:
        return _greedy_ring1_order(canonical, chords)

    first, rest = canonical[0], canonical[1:]
    best_order, best_crossings = None, None
    for perm in itertools.permutations(rest):
        order = [first, *perm]
        crossings = _count_crossings(order, chords)
        if best_crossings is None or crossings < best_crossings:
            best_crossings, best_order = crossings, order
    return best_order


def _required_ring_radius(triples: list[tuple[float, float, float]], floor: float) -> float:
    """The smallest radius at which every ``(r_i, r_j, angular_gap)`` triple's
    chord is at least ``r_i + r_j + _MIN_GLYPH_GAP`` apart, never less than
    ``floor`` (#346 §1: "grow ring N's radius until it fits")."""
    needed = floor
    for r_i, r_j, delta in triples:
        sin_half = abs(math.sin(delta / 2))
        if sin_half <= 1e-9:
            continue
        needed = max(needed, (r_i + r_j + _MIN_GLYPH_GAP) / (2 * sin_half))
    return needed


def _layout_full(mesh):  # pylint: disable=too-many-locals
    """The real work behind :func:`_layout` — also returns each ring-2 node's
    angle (needed to place its name radially, #346 §2) and the viewBox side,
    computed once here so :func:`_render_map` never re-derives ring
    membership and risks disagreeing with the positions actually drawn.

    (measured: `pylint --rcfile pyproject.toml ".../thread_page.py"` flags
    this at 33/15 locals — genuine, not a shortcut: ring 1's sector width,
    ring 2's per-parent grouping, and both rings' radius growth are one
    coherent computation that must see each other's results; splitting it
    into helpers would only turn these into more function-call arguments,
    not fewer variables, while risking two pieces of the SAME layout
    drifting apart.)
    """
    if not mesh.nodes:
        return {}, {}, 0.0

    leader, ring1_members, ring2, linked_parent = _ring1_and_ring2(mesh)
    ring1_keys = {n.key for n in ring1_members}
    ring1 = _order_ring1(ring1_members, _chord_pairs(ring1_keys, mesh.links))
    ring1_index = {node.key: i for i, node in enumerate(ring1)}

    # Orphans: a ring-2 node whose link points nowhere placeable on ring 1
    # (no link at all, or a link to a ring-2/unplaced node) — one trailing
    # sector for all of them, rather than a phantom parent guess (#346 §1).
    has_orphans = any(ring1_index.get(linked_parent.get(n.key)) is None for n in ring2)

    n_sectors = len(ring1) + (1 if has_orphans else 0)
    sector_width = (2 * math.pi / n_sectors) if n_sectors else 0.0
    ring1_angle = {node.key: i * sector_width for i, node in enumerate(ring1)}
    orphan_angle = len(ring1) * sector_width if has_orphans else None

    r1 = _RING1_MIN_R
    if len(ring1) >= 2:
        pairs = list(zip(ring1, ring1[1:]))
        if not has_orphans:
            pairs.append((ring1[-1], ring1[0]))
        r1 = _required_ring_radius(
            [(_glyph_r(a), _glyph_r(b), sector_width) for a, b in pairs], r1)

    # Ring-2: each ring-1 node's children spread evenly inside its own
    # sector (a single child sits on the parent's own angle); orphans spread
    # the same way inside the trailing orphan sector (#346 §1).
    groups: dict[Optional[int], list] = {}
    for node in ring2:
        groups.setdefault(ring1_index.get(linked_parent.get(node.key)), []).append(node)

    ring2_angle: dict[str, float] = {}
    for index, members in groups.items():
        center_angle = ring1_angle[ring1[index].key] if index is not None else orphan_angle
        ordered = sorted(members, key=lambda n: (n.foreign, n.name))
        count = len(ordered)
        for j, node in enumerate(ordered):
            offset = 0.0 if count == 1 else (j - (count - 1) / 2) * (sector_width / count)
            ring2_angle[node.key] = center_angle + offset

    r2 = _RING1_TO_RING2_GAP + r1
    if len(ring2) >= 2:
        ordered_ring2 = sorted(ring2, key=lambda n: ring2_angle[n.key])
        triples = [
            (_glyph_r(a), _glyph_r(b), ring2_angle[b.key] - ring2_angle[a.key])
            for a, b in zip(ordered_ring2, ordered_ring2[1:])
        ]
        wrap_delta = (ring2_angle[ordered_ring2[0].key] + 2 * math.pi) - ring2_angle[ordered_ring2[-1].key]
        triples.append((_glyph_r(ordered_ring2[-1]), _glyph_r(ordered_ring2[0]), wrap_delta))
        r2 = _required_ring_radius(triples, r2)

    side = 2 * r2 + 2 * _MARGIN + _LABEL_ROOM
    center = side / 2

    positions: dict[str, tuple[float, float, float]] = {}
    if leader is not None:
        positions[leader.key] = (center, center, _glyph_r(leader))
    for node in ring1:
        theta = ring1_angle[node.key]
        positions[node.key] = (center + r1 * math.sin(theta), center - r1 * math.cos(theta), _glyph_r(node))
    for node in ring2:
        theta = ring2_angle[node.key]
        positions[node.key] = (center + r2 * math.sin(theta), center - r2 * math.cos(theta), _glyph_r(node))

    return positions, ring2_angle, side


def _layout(mesh) -> dict[str, tuple[float, float, float]]:
    """``MeshNode.key -> (cx, cy, r)`` in SVG user units — a pure function of
    ``mesh`` alone (no I/O, no randomness), so a caller (or a test) can assert
    layout properties purely from this dict, without rendering any SVG."""
    positions, _ring2_angle, _side = _layout_full(mesh)
    return positions


# ---------------------------------------------------------------------------
# Node glyphs (#346 §2) — vocabulary mirrors Apple's Thread Doctor app.
# ---------------------------------------------------------------------------

def _truncate(name: str, limit: int = 18) -> str:
    return name if len(name) <= limit else f"{name[:limit - 1]}…"


def _inner_text(node) -> str:
    """The RLOC16 shown inside a glyph — the SAME 4-hex-digit address a
    neighbour table uses (a foreign router's rloc16 is `router_id << 10`, so
    this is what a real fixture actually shows); ``R{router_id}`` only when a
    foreign router's rloc16 is unknown, else nothing."""
    if node.rloc16 is not None:
        return f"{node.rloc16:04X}"
    if node.foreign and node.router_id is not None:
        return f"R{node.router_id}"
    return ""


def _name_and_role_lines(node) -> list[str]:
    """Name (truncated) plus, for an owned non-child node, a second line with
    the role (and router id) — the same information the old box rendering
    showed. A foreign node's second line is only ever the border-router
    caption; an anonymous child gets no second line at all."""
    name = _truncate(node.name)
    if node.foreign:
        lines = [name]
        if node.is_leader:
            lines.append("probably your border router")
        return lines
    if node.role_name == "Child":
        return [name]
    role_bits = [node.role_name]
    if node.router_id is not None:
        role_bits.append(f"router {node.router_id}")
    return [name, " · ".join(role_bits)]


def _text_anchor_for_angle(angle: float) -> str:
    deg = math.degrees(angle) % 360
    if deg <= 15 or deg >= 345 or abs(deg - 180) <= 15:
        return "middle"
    return "start" if deg < 180 else "end"


def _label_svg(cx: float, cy: float, r: float, lines: list[str], outward_angle: Optional[float]) -> str:
    if outward_angle is None:
        anchor, x, y = "middle", cx, cy + r + 14
    else:
        anchor = _text_anchor_for_angle(outward_angle)
        pad = r + 10
        x = cx + pad * math.sin(outward_angle)
        y = cy - pad * math.cos(outward_angle)
    tspans = f'<tspan x="{x:.1f}">{_escape(lines[0])}</tspan>'
    if len(lines) > 1:
        tspans += f'<tspan x="{x:.1f}" dy="1.15em" class="role">{_escape(lines[1])}</tspan>'
    return f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" class="node-name">{tspans}</text>'


def _worst_flag_severity(flags_for_node: list) -> Optional[str]:
    severities = {f.severity for f in flags_for_node}
    if "bad" in severities:
        return "bad"
    if "warn" in severities:
        return "warn"
    return None


def _badge_svg(cx: float, cy: float, r: float, severity: str) -> str:
    color = "var(--badge-bad)" if severity == "bad" else "var(--badge-warn)"
    bx, by = cx + r * 0.8, cy - r * 0.8
    points = f"{bx:.1f},{by - 8:.1f} {bx - 7:.1f},{by + 6:.1f} {bx + 7:.1f},{by + 6:.1f}"
    return f'<polygon class="badge-flag" points="{points}" fill="{color}"/>'


_ROLE_GLYPH_KIND = {
    "Router": "router", "Reed": "reed", "EndDevice": "ed", "SleepyEndDevice": "ed", "Child": "child",
}


def _glyph_kind(node) -> str:
    if node.foreign and node.is_leader:
        return "foreign_leader"
    if node.foreign:
        return "foreign"
    if node.is_leader:
        return "leader"
    return _ROLE_GLYPH_KIND.get(node.role_name, "unknown")


_GLYPH_FILL_VAR = {
    "leader": "var(--glyph-leader)", "router": "var(--glyph-router)",
    "ed": "var(--glyph-ed)", "child": "var(--glyph-child)",
}


def _glyph_shape_svg(kind: str, cx: float, cy: float, r: float) -> str:
    if kind == "foreign_leader":
        # Split square: left half border-router blue, right half leader
        # purple — the BR-that-is-also-the-leader glyph (#346 §2).
        return (
            f'<path d="M {cx - r:.1f} {cy - r:.1f} h {r:.1f} v {2 * r:.1f} h {-r:.1f} Z" fill="var(--glyph-br)"/>'
            f'<path d="M {cx:.1f} {cy - r:.1f} h {r:.1f} v {2 * r:.1f} h {-r:.1f} Z" fill="var(--glyph-leader)"/>'
        )
    if kind == "foreign":
        return (f'<rect x="{cx - r:.1f}" y="{cy - r:.1f}" width="{2 * r:.1f}" height="{2 * r:.1f}" '
                 f'rx="8" fill="var(--glyph-foreign)"/>')
    if kind == "reed":
        return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
                 f'stroke="var(--glyph-ed)" stroke-width="3"/>')
    if kind == "unknown":
        return (f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" stroke="var(--glyph-foreign)" '
                 f'stroke-width="2" stroke-dasharray="3 3"/>')
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{_GLYPH_FILL_VAR[kind]}"/>'


def _node_glyph_svg(node, circle: tuple[float, float, float], flags_for_node: list,
                     *, outward_angle: Optional[float] = None) -> str:
    """One node's glyph, inner RLOC16, name/role labels and warning badge.

    ``outward_angle`` is ``None`` for the centre/ring-1 nodes (name centred
    below the glyph) and the node's own angle for a ring-2 node (name drawn
    radially outward, #346 §2) — threaded in rather than re-derived here so
    ring membership has exactly one source of truth (:func:`_layout_full`).
    """
    cx, cy, r = circle
    kind = _glyph_kind(node)
    shape_svg = _glyph_shape_svg(kind, cx, cy, r)

    inner_svg = ""
    inner = _inner_text(node)
    if inner and r > 12:
        inner_svg = (f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" dominant-baseline="central" '
                     f'class="glyph-text">{_escape(inner)}</text>')

    label_svg = _label_svg(cx, cy, r, _name_and_role_lines(node), outward_angle)

    severity = _worst_flag_severity(flags_for_node)
    badge_svg = _badge_svg(cx, cy, r, severity) if severity else ""

    title = "\n".join(_escape(t) for t in (node.name, *(f.message for f in flags_for_node)))

    return (
        f'<g class="node node-{kind}">'
        f'<title>{title}</title>'
        f'{shape_svg}{inner_svg}{badge_svg}{label_svg}'
        f'</g>'
    )


# ---------------------------------------------------------------------------
# Edges (#346 §3) — straight lines, glyph-edge to glyph-edge, quiet by
# default. A hover/long-press title always carries the reading; a visible
# label is drawn only on a "poor" edge.
# ---------------------------------------------------------------------------

def _shorten_to_glyph_edges(circle_a: tuple[float, float, float],
                             circle_b: tuple[float, float, float]) -> Optional[tuple[float, float, float, float]]:
    """Move each endpoint in from the glyph centre to its own edge (or square
    half-side) by that glyph's own r — so a chord never overlaps a third
    node's glyph the way a raw centre-to-centre line would (#346 §3).
    ``None`` for two coincident points (should not happen; defensive)."""
    ax, ay, ar = circle_a
    bx, by, br = circle_b
    dx, dy = bx - ax, by - ay
    dist = math.hypot(dx, dy)
    if dist < 1e-6:
        return None
    ux, uy = dx / dist, dy / dist
    return ax + ux * ar, ay + uy * ar, bx - ux * br, by - uy * br


def _poor_edge_label(link: dict, segment: tuple[float, float, float, float], label_text: str) -> str:
    """A visible label is drawn only on a "poor" edge (#346 §3) — every other
    band's reading lives in the edge's ``<title>`` only."""
    if _rssi_band(link["overall_rssi"]) != "poor":
        return ""
    sx, sy, ex, ey = segment
    mid_x, mid_y = (sx + ex) / 2, (sy + ey) / 2
    return f'<text x="{mid_x:.1f}" y="{mid_y:.1f}" text-anchor="middle" class="edge-label">{_escape(label_text)}</text>'


def _link_svg(link: dict, positions: dict, nodes_by_key: dict) -> str:
    if link["a"] not in positions or link["b"] not in positions:
        return ""
    shortened = _shorten_to_glyph_edges(positions[link["a"]], positions[link["b"]])
    if shortened is None:
        return ""
    sx, sy, ex, ey = shortened

    color = _edge_color(link["overall_rssi"])
    label_text = _link_label(link)
    title = (f'{_escape(nodes_by_key[link["a"]].name)} ↔ {_escape(nodes_by_key[link["b"]].name)}: '
             f'{_escape(label_text)}')
    visible_label = _poor_edge_label(link, shortened, label_text)

    return (
        f'<g class="edge">'
        f'<title>{title}</title>'
        f'<line x1="{sx:.1f}" y1="{sy:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="{color}" stroke-width="3"/>'
        f'{visible_label}'
        f'</g>'
    )


_MAP_CSS = (
    '.node-name { font: 12px -apple-system, sans-serif; fill: var(--fg); }'
    '.role { font-size: 10px; fill: var(--muted); }'
    '.glyph-text { font: 700 11px ui-monospace, Menlo, monospace; fill: #fff; }'
    '.badge-flag { stroke: var(--bg); stroke-width: 1; }'
    # Text halo (#334 finding A4, kept): paint-order draws the background-
    # coloured stroke BEHIND the glyph fill so a label over a line reads
    # without needing a separate background rect.
    '.edge-label { font: 10px ui-monospace, Menlo, monospace; fill: var(--muted); '
    'paint-order: stroke; stroke: var(--bg); stroke-width: 4px; }'
)


# ---------------------------------------------------------------------------
# Legend (#346 §4) — moved out of the SVG into a collapsible HTML block
# directly under the map.
# ---------------------------------------------------------------------------

def _legend_swatch(kind: str) -> str:
    shapes = {
        "leader": '<circle cx="10" cy="10" r="8" fill="var(--glyph-leader)"/>',
        "router": '<circle cx="10" cy="10" r="8" fill="var(--glyph-router)"/>',
        "reed": '<circle cx="10" cy="10" r="7" fill="none" stroke="var(--glyph-ed)" stroke-width="3"/>',
        "ed": '<circle cx="10" cy="10" r="6" fill="var(--glyph-ed)"/>',
        "border_router": '<rect x="2" y="2" width="16" height="16" rx="4" fill="var(--glyph-br)"/>',
        "foreign": '<rect x="2" y="2" width="16" height="16" rx="4" fill="var(--glyph-foreign)"/>',
        "warn": '<polygon points="10,3 2,17 18,17" fill="var(--badge-warn)"/>',
    }
    return f'<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">{shapes[kind]}</svg>'


#: (glyph kind, display name, plain-English one-liner) — Thread Doctor's own
#: wording as the model (#346 §4).
_LEGEND_NODE_ROWS = (
    ("leader", "Leader", "the router currently coordinating the mesh"),
    ("router", "Router", "relays traffic for other devices"),
    ("reed", "REED", "mains-powered, radio always on, but the mesh hasn't promoted it to router"),
    ("ed", "End device / Sleepy end device",
     "non-routing device (plug, bulb, sensor); sleepy ones only wake to report"),
    ("border_router", "Border router",
     "HomePod, Apple TV or other gateway (drawn split when it is also the leader)"),
    ("foreign", "Unidentified router",
     "a router this plugin doesn't own; drawn from what your devices report about it"),
    ("warn", "Warning triangle", "this device has a warn (amber) or bad (red) finding; see the flags list"),
)

_LEGEND_BAND_ROWS = (
    ("excellent", "Excellent", "-65 dBm or stronger"),
    ("good", "Good", "-77 to -66 dBm"),
    ("fair", "Fair", "-87 to -78 dBm"),
    ("poor", "Poor", "-88 dBm or weaker"),
    ("unknown", "Unknown", "signal not reported"),
)


def _render_legend() -> str:
    node_items = "".join(
        f'<li>{_legend_swatch(kind)}<strong>{_escape(title)}</strong> — {_escape(desc)}</li>'
        for kind, title, desc in _LEGEND_NODE_ROWS
    )
    band_items = "".join(
        f'<li><svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true">'
        f'<line x1="1" y1="10" x2="19" y2="10" stroke="{_BAND_COLOR_VAR[kind]}" stroke-width="3"/></svg>'
        f'<strong>{_escape(title)}</strong> — {_escape(desc)}</li>'
        for kind, title, desc in _LEGEND_BAND_ROWS
    )
    return (
        '<details class="legend msg"><summary>What do the colours mean?</summary>'
        '<h3>Nodes</h3>'
        f'<ul>{node_items}</ul>'
        '<h3>Lines (Thread link signal strength)</h3>'
        f'<ul>{band_items}</ul>'
        '<h3>Terms</h3>'
        '<p><strong>RLOC16</strong> — the 4-hex-digit address inside each glyph, the id neighbour '
        'tables use.</p>'
        '<p><strong>Partition</strong> — the group of routers currently agreeing on one leader; '
        "nodes in different partitions can't reach each other yet.</p>"
        '<p><strong>Frame error</strong> — the percentage of radio frames to a neighbour that failed '
        'to decode.</p>'
        '<p class="legend-note">If the leader fails, the mesh re-elects one in roughly 30 s; devices '
        'may briefly stall.</p>'
        "<p class=\"legend-note\">Border routers and other routers this plugin doesn't own can't be "
        'read directly — the map shows them from what your own devices report.</p>'
        '</details>'
    )


def _render_map(mesh) -> str:
    if not mesh.nodes:
        return '<p class="msg">No Thread nodes to draw.</p>'
    positions, ring2_angle, side = _layout_full(mesh)
    nodes_by_key = {node.key: node for node in mesh.nodes if node.key in positions}
    flags_by_node: dict[str, list] = {}
    for flag in mesh.flags:
        flags_by_node.setdefault(flag.node_key, []).append(flag)

    edges_svg = "".join(_link_svg(link, positions, nodes_by_key) for link in _merged_links(mesh.links))
    nodes_svg = "".join(
        _node_glyph_svg(node, positions[node.key], flags_by_node.get(node.key, []),
                        outward_angle=ring2_angle.get(node.key))
        for node in mesh.nodes if node.key in positions
    )

    svg = (
        f'<svg viewBox="0 0 {side:.0f} {side:.0f}" style="width:100%;height:auto" '
        f'role="img" aria-label="Thread mesh map">'
        f'<style>{_MAP_CSS}</style>'
        f'{edges_svg}{nodes_svg}'
        f'</svg>'
    )
    return svg + _render_legend()


# ---------------------------------------------------------------------------
# Flags / table / unreadable sections
# ---------------------------------------------------------------------------

def _render_flags(mesh) -> str:
    if not mesh.flags:
        return '<p class="msg">No flags — nothing here looks unhealthy.</p>'
    ordered = sorted(mesh.flags, key=lambda f: _severity_rank(f.severity))
    rows = "".join(
        f'<li class="flag-{_escape(flag.severity)}">'
        f'<span class="sev">{_escape(flag.severity.upper())}</span> '
        f'<span class="code">{_escape(flag.code)}</span> — {_escape(flag.message)}</li>'
        for flag in ordered
    )
    return f'<ul class="flags">{rows}</ul>'


def _interview_age_seconds(diag, now: datetime) -> Optional[float]:
    if diag.last_interview is None:
        return None
    # #344 review, C4: _aware normalises a naive last_interview (a direct
    # test-writer, never thread_survey._parse_timestamp itself — see
    # NodeDiag's own field comment) so this subtraction never raises.
    return (now - _aware(diag.last_interview)).total_seconds()


def _oldest_interview_age(diags: list, now: datetime) -> tuple[Optional[float], int]:
    """``(oldest_age, unknown_count)`` (#344; unknown_count added #344 review,
    A3): the MAX interview age across every diag with a known
    ``last_interview``, ``None`` when none is known at all, alongside how
    many diags have NO baseline at all. Used for the header's "oldest node
    baseline" note and the stale banner's threshold — before A3, that note
    silently said nothing about the unknown diags, which could make "oldest
    node baseline: 10m" read as though the whole fabric were freshly
    surveyed when most of it was actually unread."""
    ages: list[float] = []
    unknown_count = 0
    for diag in diags:
        age = _interview_age_seconds(diag, now)
        if age is None:
            unknown_count += 1
        else:
            ages.append(age)
    return (max(ages) if ages else None), unknown_count


def _freshness_cell(diag, now: datetime) -> str:
    """The Nodes table's freshness cell (#344): ``"cached · interviewed 6d
    ago · last 0x35 update 2h ago"`` / ``"live · interviewed 6d ago"`` — the
    source word plus :func:`freshness_text`, escaped, and wrapped in an amber
    ``chip-stale`` span when this is CACHED data whose interview baseline is
    older than an hour (Simon's UX call — cached stays the page default; old
    data is highlighted, not hidden)."""
    source_word = "live" if diag.source == "live" else "cached"
    text = _escape(f"{source_word} · {freshness_text(diag, now)}")
    age = _interview_age_seconds(diag, now)
    if diag.source != "live" and age is not None and age > _STALE_AGE_SECONDS:
        return f'<span class="chip-stale">{text}</span>'
    return text


def _best_link_text(diag) -> str:
    if diag.is_router:
        rssi = diag.best_rssi
    else:
        parent = diag.parent
        rssi = parent.rssi if parent is not None else None
    return _fmt_opt(rssi, suffix=" dBm")


def _render_table(diags: list, now: datetime) -> str:
    if not diags:
        return '<p class="msg">No Thread devices are commissioned.</p>'
    rows = []
    for diag in diags:
        router_bits = _fmt_opt(diag.router_id)
        if diag.router_id is not None:
            router_bits = f"{diag.router_id} (0x{diag.rloc16:04X})"
        counters = ", ".join(sorted(diag.counters)) or "none"
        rows.append(
            "<tr>"
            f"<td>{_escape(diag.name)}</td>"
            f"<td>{_escape(diag.role_name)}</td>"
            f"<td>{_escape(router_bits)}</td>"
            f"<td>{_escape(_fmt_opt(diag.partition_id))}</td>"
            f"<td>{_freshness_cell(diag, now)}</td>"
            f"<td>{len(diag.neighbours)}</td>"
            f"<td>{_escape(_best_link_text(diag))}</td>"
            f"<td>{_escape(counters)}</td>"
            "</tr>"
        )
    header = (
        "<tr><th>Name</th><th>Role</th><th>Router id (rloc16)</th><th>Partition</th>"
        # #344 review, C1: "· cache age" — the column already carries the
        # freshness cell (source word + interview/event ages); the bare
        # "Source" header undersold what it actually shows.
        "<th>Source · cache age</th><th>Neighbours</th><th>Best link</th><th>Counters present</th></tr>"
    )
    return (
        '<div class="table-wrap"><table class="nodes">'
        f'<thead>{header}</thead><tbody>{"".join(rows)}</tbody>'
        '</table></div>'
    )


def _render_unreadable(mesh) -> str:
    """The "Unreadable / stale" section: ``mesh.unreadable`` (per-node
    read_error), ``mesh.disagreements`` (partition disagreements), and
    ``mesh.warnings`` (#334 finding B2.5 — parse warnings plus mesh-level
    notes like an unattributed-children group, B5.2).

    No re-derivation from ``diags`` (#334 finding B1.6): ``mesh.unreadable``
    is built from those same diags' ``read_error`` fields inside
    :func:`thread_mesh.build_mesh`, so the two lists are the SAME list, not
    two that could drift — walking ``diags`` a second time here bought
    nothing but the appearance of a second source of truth.
    """
    items = [f'<li>0x{node_id:X}: {_escape(reason)}</li>' for node_id, reason in mesh.unreadable]
    items.extend(f'<li>{_escape(disagreement)}</li>' for disagreement in mesh.disagreements)
    items.extend(f'<li>0x{node_id:X}: {_escape(note)}</li>' for node_id, note in mesh.warnings)
    if not items:
        return ""
    return (
        '<h2>Unreadable / stale</h2>'
        f'<ul class="unreadable">{"".join(items)}</ul>'
    )


def render_thread_page(mesh, diags: list, *, generated_at: str, live: bool,  # pylint: disable=too-many-locals
                        plugin_id: str, error: Optional[str] = None,
                        now: Optional[datetime] = None) -> str:
    """The Thread mesh page (#334; cache-age display added #344; radial map
    #346). Self-contained: no scripts, no external assets. ``mesh``/``diags``
    come from :func:`thread_mesh.build_mesh`/:func:`thread_survey.run_survey`;
    when ``error`` is set (matter-server unreachable, ``get_nodes()`` failing,
    or both a live AND a cache-fallback read failing) both are expected to be
    empty and the page shows the banner instead of a map — an unusable read
    must never look like a real but empty mesh (root workspace CLAUDE.md
    degradation-path convention). A live-only timeout does NOT reach this
    banner path any more (#334 finding B3.2): it falls back to a cached
    render with ``error`` naming the live timeout instead.

    ``now`` (#344) is resolved once, here, and threaded to every age
    computed on this render — the caller (``HttpApiMixin.http_thread_page``)
    passes the SAME value it derived ``generated_at`` from, so the header's
    timestamp and every cache age on the page agree with each other.
    """
    resolved_now = now or datetime.now(timezone.utc)
    live_url = f"/message/{_escape(plugin_id)}/thread?live=1"
    cached_url = f"/message/{_escape(plugin_id)}/thread"
    badge = "LIVE" if live else "CACHED"
    badge_class = "badge-live" if live else "badge-cached"

    # #344 UX (Simon's call), rewritten #344 review B3 to match A3: the
    # header note is purely informational and appears on any CACHED render
    # where at least one diag's interview baseline is known — now also
    # naming how many diags have NO baseline at all (A3), rather than
    # silently implying the whole fabric is that fresh. The BANNER — the
    # thing pointing at "Refresh (live)" — is gated separately, on the
    # oldest KNOWN baseline actually being stale (> 1 h). Neither ever
    # appears on a live render.
    oldest_age, unknown_baseline_count = (None, 0) if live else _oldest_interview_age(diags, resolved_now)
    badge_note = ""
    stale_banner = ""
    if oldest_age is not None:
        # #344 review, A3: "(N unknown)" only when there IS an unknown diag —
        # wording is unchanged when every diag's baseline is known.
        unknown_note = f" ({unknown_baseline_count} unknown)" if unknown_baseline_count else ""
        badge_note = (
            f' <span class="badge-note">oldest node baseline: '
            f'{_escape(humanise_age(oldest_age))}{unknown_note}</span>'
        )
        if oldest_age > _STALE_AGE_SECONDS:
            # #344 review, A3: named BEFORE the "Use Refresh (live)" sentence
            # — a reader deciding whether to bother re-reading needs to know
            # some nodes have no baseline to trust at all before being told
            # how to fix the ones that do.
            unknown_sentence = (
                f' {unknown_baseline_count} node(s) have no interview baseline at all.'
                if unknown_baseline_count else ""
            )
            stale_banner = (
                f'<p class="msg">Some of this data is matter-server\'s cache — oldest node baseline '
                f'{_escape(humanise_age(oldest_age))} ago. Baselines are a lower bound: a value may be '
                f'staler than its interview.{unknown_sentence} Use <a href="{live_url}">Refresh (live)</a> '
                f'to re-read sleepy devices.</p>'
            )

    header = f"""
<h1>Thread mesh</h1>
<table class="header-facts">
<tr><th>Network</th><td>{_escape(mesh.network_name or "unknown")}</td></tr>
<tr><th>Channel</th><td>{_escape(_fmt_opt(mesh.channel))}</td></tr>
<tr><th>PAN</th><td>{_escape(f"0x{mesh.pan_id:04X}") if mesh.pan_id is not None else "unknown"}</td></tr>
<tr><th>Generated</th><td>{_escape(generated_at)} <span class="badge {badge_class}">{badge}</span>{badge_note}</td></tr>
</table>
<p class="partition-summary">{_escape(partition_header_text(mesh, diags))}</p>
<p class="links">
<a href="{live_url}">Refresh (live)</a> &middot;
<a href="{cached_url}">Cached (fast)</a>
</p>
{stale_banner}"""

    if error:
        banner = f'<p class="warn"><strong>Could not read the Thread mesh:</strong> {_escape(error)}</p>'
        body = f"{header}{banner}"
        if not mesh.nodes:
            # A hard failure (matter-server unreachable, get_nodes() failing,
            # or a cache-fallback ALSO failing): no map, same as before.
            return _document(body)
        # #334 finding B3.2: a live timeout that fell back to a cache-only
        # render still has real data — show it, banner and all, rather than
        # discarding it the way a hard failure has to.
        body += f"""
{_render_map(mesh)}
<h2>Flags</h2>
{_render_flags(mesh)}
<h2>Nodes</h2>
{_render_table(diags, resolved_now)}
{_render_unreadable(mesh)}"""
        return _document(body)

    body = f"""{header}
{_render_map(mesh)}
<h2>Flags</h2>
{_render_flags(mesh)}
<h2>Nodes</h2>
{_render_table(diags, resolved_now)}
{_render_unreadable(mesh)}"""
    return _document(body)


def _document(body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Indigo Matter bridge — Thread mesh</title>
<style>
 :root {{
   --fg: #222; --muted: #666; --bg: #fff; --border: #ddd;
   --chip-stale-bg: #fff3cd; --chip-stale-fg: #7a5b00;
   --glyph-leader: #a844c4; --glyph-router: #3fb44f; --glyph-ed: #2f7ff3;
   --glyph-foreign: #8a8f98; --glyph-br: #4fb8e8; --glyph-child: #a3a8b0;
   --edge-excellent: #3fb44f; --edge-good: #e6c229; --edge-fair: #f0932b;
   --edge-poor: #e0443a; --edge-unread: #9a9a9a;
   --badge-warn: #d9a406; --badge-bad: #c0392b;
 }}
 @media (prefers-color-scheme: dark) {{
   :root {{
     --fg: #e6e6e6; --muted: #aaa; --bg: #16171a; --border: #333;
     --chip-stale-bg: #4a3b00; --chip-stale-fg: #e8c76a;
     --glyph-leader: #c07ad9; --glyph-router: #5cc96a; --glyph-ed: #5a9bf5;
     --glyph-foreign: #a7abb3; --glyph-br: #7cc9ea; --glyph-child: #b7bcc3;
     --edge-excellent: #5cc96a; --edge-good: #e8cf57; --edge-fair: #f2a35c;
     --edge-poor: #e56f66; --edge-unread: #b0b0b0;
     --badge-warn: #e0b73a; --badge-bad: #d9695f;
   }}
 }}
 body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0 auto; max-width: 60rem; padding: 1.5rem; color: var(--fg); background: var(--bg); }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1rem; margin: 1.4rem 0 .3rem; color: var(--muted); }}
 .header-facts {{ border-collapse: collapse; margin: .5rem 0; }}
 .header-facts th {{ text-align: left; color: var(--muted); font-weight: 400; padding: .1rem .8rem .1rem 0; }}
 .header-facts td {{ padding: .1rem 0; }}
 .partition-summary {{ margin: .3rem 0 .8rem; }}
 .badge {{ display: inline-block; padding: .1rem .5rem; border-radius: .3rem; font-size: .75rem;
          font-weight: 700; letter-spacing: .04em; }}
 .badge-live {{ background: #d6f5df; color: #1e6b34; }}
 .badge-cached {{ background: #eee; color: #555; }}
 .badge-note {{ margin-left: .5rem; font-size: .75rem; color: var(--muted); }}
 .chip-stale {{ background: var(--chip-stale-bg); color: var(--chip-stale-fg);
               padding: .05rem .4rem; border-radius: .3rem; font-weight: 600; }}
 .links a {{ margin-right: 1rem; }}
 .msg {{ background: #fff6d6; border: 1px solid #e8d48a; padding: .7rem; border-radius: .4rem; }}
 .warn {{ background: #fdeaea; border: 1px solid #d99; padding: .7rem; border-radius: .4rem; }}
 details.legend {{ margin: .8rem 0; }}
 details.legend summary {{ font-weight: 700; cursor: pointer; }}
 details.legend h3 {{ margin: .8rem 0 .3rem; font-size: .85rem; }}
 details.legend ul {{ list-style: none; padding: 0; margin: 0 0 .4rem; }}
 details.legend li {{ display: flex; align-items: center; gap: .5rem; padding: .2rem 0; }}
 details.legend svg {{ flex: none; }}
 details.legend .legend-note {{ font-size: .85rem; color: var(--muted); margin: .4rem 0 0; }}
 .flags {{ list-style: none; padding: 0; margin: 0; }}
 .flags li {{ padding: .3rem 0; border-bottom: 1px solid var(--border); }}
 .flag-bad .sev {{ color: #c0392b; font-weight: 700; }}
 .flag-warn .sev {{ color: #b8860b; font-weight: 700; }}
 .flag-info .sev {{ color: var(--muted); font-weight: 700; }}
 .flags .code {{ font: .8rem ui-monospace, Menlo, monospace; color: var(--muted); }}
 .table-wrap {{ overflow-x: auto; }}
 table.nodes {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
 table.nodes th, table.nodes td {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid var(--border); }}
 .unreadable {{ padding-left: 1.2rem; }}
 footer {{ margin-top: 2rem; font-size: .85rem; color: var(--muted); }}
</style></head><body>
{body}
<footer>This page is read-only — no Matter attribute is ever written from it (ADR-0004).
An ext(ended) address shown elsewhere in these diagnostics is flagged "≈" when
matter-server's JSON serialisation has lost precision (values ≥ 2⁵³) and should
not be treated as exact. Owned nodes are keyed by their Matter node id; only
foreign routers and unattributed children are keyed by router id / RLOC16
instead, which is not lossy. "Last 0x35 update" counts only live
ThreadNetworkDiagnostics events seen since the plugin last started — after a
restart it reads "—" until the next event arrives.</footer>
</body></html>"""
