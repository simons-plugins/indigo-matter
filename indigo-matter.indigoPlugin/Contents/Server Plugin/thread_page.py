"""The Thread mesh IWS page's HTML (#334) — pure rendering, no I/O, no ``indigo``
import. Mirrors :mod:`pairing_page`'s shape: the only caller is
:meth:`HttpApiMixin.http_thread_page`, which builds the live
:class:`~thread_mesh.Mesh` (via :func:`thread_survey.run_survey` +
:func:`thread_mesh.build_mesh`) and hands it here to render.

Self-contained HTML + CSS + an inline SVG map computed entirely in Python — no
JS, no external assets, so it renders in Safari on a phone over the Reflector.
The map is a simple layered layout: the fabric's leader (owned or foreign) on
its own row, every other router in a row beneath it, and every non-router
placed under whichever router it links to (its parent, for a SED/ED/REED; the
router it is an anonymous child of, for a "child:0x.." node) — never geometry
computed anywhere but here, and never a write of any kind (ADR-0004: this page
only ever reads).

Everything rendered here — node names above all, which come straight from
Indigo device names — is treated as hostile input and passed through
:func:`_escape`.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["render_thread_page"]

# RSSI colour bands (ground truth, SPEC "Ground truth" section / thread_mesh's
# own RSSI_WEAK/RSSI_BAD constants — duplicated as plain numbers here rather
# than imported, because this module must stay import-free of thread_mesh's
# *logic*, only its *data shapes*; the bands are a presentation choice, not a
# health-flag decision, and the two are allowed to diverge without either
# breaking the other).
_RSSI_GOOD = -70
_RSSI_WEAK = -80

_COLOR_GOOD = "#2e8b45"
_COLOR_WEAK = "#b8860b"
_COLOR_BAD = "#c0392b"
_COLOR_UNKNOWN = "#888888"

_BOX_W = 168
_BOX_H = 56
_GAP_X = 26
_GAP_Y = 92
_MARGIN = 28


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


def _edge_color(rssi: Optional[int]) -> str:
    if rssi is None:
        return _COLOR_UNKNOWN
    if rssi >= _RSSI_GOOD:
        return _COLOR_GOOD
    if rssi >= _RSSI_WEAK:
        return _COLOR_WEAK
    return _COLOR_BAD


def _severity_rank(severity: str) -> int:
    return {"bad": 0, "warn": 1, "info": 2}.get(severity, 3)


# ---------------------------------------------------------------------------
# Link merging — a link between two routers is reported from BOTH sides
# (build_mesh adds one MeshLink per reporting node), so the page shows ONE
# edge per pair, using the worse of the two sides' readings for colour and
# both readings in the label when they disagree (SPEC #334 §4).
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
# Layout — leader on its own row, every other router beneath it, every
# non-router placed under whichever router it links to.
# ---------------------------------------------------------------------------

def _is_router_node(node) -> bool:
    return node.foreign or node.role_name in ("Router", "Leader")


def _layout(mesh) -> tuple[dict[str, tuple[float, float]], float, float]:  # pylint: disable=too-many-locals
    nodes = list(mesh.nodes)
    leader = next((n for n in nodes if n.is_leader), None)
    routers = [n for n in nodes if _is_router_node(n) and n is not leader]
    non_routers = [n for n in nodes if not _is_router_node(n)]

    positions: dict[str, tuple[float, float]] = {}
    y_leader = _MARGIN
    if leader is not None:
        positions[leader.key] = (_MARGIN + _BOX_W / 2, y_leader + _BOX_H / 2)

    y_routers = y_leader + (_BOX_H + _GAP_Y if leader is not None else 0)
    for index, node in enumerate(sorted(routers, key=lambda n: (n.foreign, n.name))):
        x = _MARGIN + index * (_BOX_W + _GAP_X) + _BOX_W / 2
        positions[node.key] = (x, y_routers + _BOX_H / 2)

    # Every non-router keyed by whichever router-layer node its own link (kind
    # "parent" or "child") points at — never a phantom parent guess: a
    # non-router with no such link (a detached node, or a link to a node this
    # page could not place) becomes an orphan, laid out after the rest.
    linked_parent: dict[str, str] = {}
    for link in mesh.links:
        if link.kind in ("parent", "child") and link.a in {n.key for n in non_routers}:
            linked_parent[link.a] = link.b

    by_parent: dict[str, list] = {}
    orphans = []
    for node in non_routers:
        parent_key = linked_parent.get(node.key)
        if parent_key is not None and parent_key in positions:
            by_parent.setdefault(parent_key, []).append(node)
        else:
            orphans.append(node)

    y_children = y_routers + (_BOX_H + _GAP_Y if routers or leader is not None else 0)
    for parent_key in sorted(by_parent, key=lambda k: positions[k][0]):
        children = sorted(by_parent[parent_key], key=lambda n: n.name)
        parent_x, _parent_y = positions[parent_key]
        start_x = parent_x - ((len(children) - 1) * (_BOX_W + _GAP_X)) / 2
        for index, child in enumerate(children):
            positions[child.key] = (start_x + index * (_BOX_W + _GAP_X), y_children + _BOX_H / 2)

    next_x = max((x + _BOX_W / 2 + _GAP_X for x, _y in positions.values()), default=_MARGIN)
    for node in sorted(orphans, key=lambda n: n.name):
        positions[node.key] = (next_x + _BOX_W / 2, y_children + _BOX_H / 2)
        next_x += _BOX_W + _GAP_X

    xs = [x for x, _y in positions.values()] or [_MARGIN + _BOX_W / 2]
    ys = [y for _x, y in positions.values()] or [_MARGIN + _BOX_H / 2]
    width = max(xs) + _BOX_W / 2 + _MARGIN
    height = max(ys) + _BOX_H / 2 + _MARGIN
    return positions, width, height


def _node_box_svg(node, x: float, y: float) -> str:
    left, top = x - _BOX_W / 2, y - _BOX_H / 2
    if node.foreign:
        css_class = "box-foreign"
        title = f"Router {_escape(node.router_id)}"
        lines = [title]
        if node.is_leader:
            lines.append("probably your border router")
    elif node.role_name == "Child":
        css_class = "box-child"
        lines = [_escape(node.name)]
    else:
        css_class = "box-owned"
        role_bits = [node.role_name]
        if node.router_id is not None:
            role_bits.append(f"router {node.router_id}")
        lines = [_escape(node.name), " · ".join(_escape(b) for b in role_bits)]

    text_lines = "".join(
        f'<tspan x="{x}" dy="{0 if i == 0 else "1.2em"}">{line}</tspan>'
        for i, line in enumerate(lines)
    )
    return (
        f'<g class="node {css_class}">'
        f'<rect x="{left:.1f}" y="{top:.1f}" width="{_BOX_W}" height="{_BOX_H}" rx="8"/>'
        f'<text x="{x:.1f}" y="{y - (len(lines) - 1) * 8:.1f}" text-anchor="middle">{text_lines}</text>'
        f'</g>'
    )


def _link_svg(link: dict, positions: dict[str, tuple[float, float]]) -> str:
    if link["a"] not in positions or link["b"] not in positions:
        return ""
    ax, ay = positions[link["a"]]
    bx, by = positions[link["b"]]
    color = _edge_color(link["overall_rssi"])
    mid_x, mid_y = (ax + bx) / 2, (ay + by) / 2
    label = _escape(_link_label(link))
    return (
        f'<g class="edge">'
        f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{bx:.1f}" y2="{by:.1f}" stroke="{color}" stroke-width="2"/>'
        f'<text x="{mid_x:.1f}" y="{mid_y:.1f}" text-anchor="middle" class="edge-label">{label}</text>'
        f'</g>'
    )


def _legend_svg(x: float, y: float) -> str:
    rows = [
        (_COLOR_GOOD, f"≥ {_RSSI_GOOD} dBm"),
        (_COLOR_WEAK, f"{_RSSI_WEAK}…{_RSSI_GOOD} dBm"),
        (_COLOR_BAD, f"< {_RSSI_WEAK} dBm"),
        (_COLOR_UNKNOWN, "unknown"),
    ]
    parts = [f'<text x="{x}" y="{y - 8}" class="legend-title">Link RSSI</text>']
    for index, (color, label) in enumerate(rows):
        row_y = y + index * 18
        parts.append(
            f'<line x1="{x}" y1="{row_y - 4}" x2="{x + 24}" y2="{row_y - 4}" stroke="{color}" stroke-width="3"/>'
            f'<text x="{x + 32}" y="{row_y}" class="legend-label">{_escape(label)}</text>'
        )
    return "".join(parts)


def _render_map(mesh) -> str:
    if not mesh.nodes:
        return '<p class="msg">No Thread nodes to draw.</p>'
    positions, width, height = _layout(mesh)
    legend_height = 100
    total_height = height + legend_height

    edges_svg = "".join(_link_svg(link, positions) for link in _merged_links(mesh.links))
    nodes_svg = "".join(_node_box_svg(node, *positions[node.key])
                        for node in mesh.nodes if node.key in positions)
    legend_svg = _legend_svg(_MARGIN, height + 24)

    return (
        f'<svg viewBox="0 0 {width:.0f} {total_height:.0f}" style="width:100%;height:auto" '
        f'role="img" aria-label="Thread mesh map">'
        f'<style>'
        f'.node text {{ font: 12px -apple-system, sans-serif; }}'
        f'.box-owned rect {{ fill: var(--box-owned-fill); stroke: var(--box-owned-stroke); stroke-width: 1.5; }}'
        f'.box-owned text {{ fill: var(--fg); }}'
        f'.box-foreign rect {{ fill: var(--box-foreign-fill); stroke: var(--box-foreign-stroke); '
        f'stroke-width: 1.5; stroke-dasharray: 5 3; }}'
        f'.box-foreign text {{ fill: var(--muted); }}'
        f'.box-child rect {{ fill: var(--box-child-fill); stroke: var(--box-child-stroke); }}'
        f'.box-child text {{ fill: var(--muted); font-size: 10px; }}'
        f'.edge-label {{ font: 10px ui-monospace, Menlo, monospace; fill: var(--muted); }}'
        f'.legend-title {{ font: 700 12px -apple-system, sans-serif; fill: var(--fg); }}'
        f'.legend-label {{ font: 11px -apple-system, sans-serif; fill: var(--fg); }}'
        f'</style>'
        f'{edges_svg}{nodes_svg}{legend_svg}'
        f'</svg>'
    )


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


def _best_link_text(diag) -> str:
    if diag.is_router:
        rssi = diag.best_rssi
    else:
        parent = diag.parent
        rssi = parent.rssi if parent is not None else None
    return _fmt_opt(rssi, suffix=" dBm")


def _render_table(diags: list) -> str:
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
            f"<td>{_escape(diag.source)}</td>"
            f"<td>{len(diag.neighbours)}</td>"
            f"<td>{_escape(_best_link_text(diag))}</td>"
            f"<td>{_escape(counters)}</td>"
            "</tr>"
        )
    header = (
        "<tr><th>Name</th><th>Role</th><th>Router id (rloc16)</th><th>Partition</th>"
        "<th>Source</th><th>Neighbours</th><th>Best link</th><th>Counters present</th></tr>"
    )
    return (
        '<div class="table-wrap"><table class="nodes">'
        f'<thead>{header}</thead><tbody>{"".join(rows)}</tbody>'
        '</table></div>'
    )


def _render_unreadable(mesh, diags: list) -> str:
    # mesh.unreadable is BUILT from the same diags' read_error fields
    # (thread_mesh.build_mesh), so the two lists are normally identical — both
    # are listed anyway (SPEC #334 §4) so this section stays correct even if a
    # future caller ever builds the mesh from a different diags list than the
    # one it renders here.
    seen: set[int] = set()
    items = []
    for node_id, reason in mesh.unreadable:
        seen.add(node_id)
        items.append(f'<li>0x{node_id:X}: {_escape(reason)}</li>')
    for diag in diags:
        if diag.read_error and diag.node_id not in seen:
            items.append(f'<li>{_escape(diag.name)} (0x{diag.node_id:X}): {_escape(diag.read_error)}</li>')
    for disagreement in mesh.disagreements:
        items.append(f'<li>{_escape(disagreement)}</li>')
    if not items:
        return ""
    return (
        '<h2>Unreadable / stale</h2>'
        f'<ul class="unreadable">{"".join(items)}</ul>'
    )


def render_thread_page(mesh, diags: list, *, generated_at: str, live: bool,  # pylint: disable=too-many-locals
                        plugin_id: str, error: Optional[str] = None) -> str:
    """The Thread mesh page (SPEC #334 §4). Self-contained: no scripts, no
    external assets. ``mesh``/``diags`` come from
    :func:`thread_mesh.build_mesh`/:func:`thread_survey.run_survey`; when
    ``error`` is set (matter-server unreachable, ``get_nodes()`` failing, or the
    live-read round trip timing out) both are expected to be empty and the page
    shows the banner instead of a map — an unusable read must never look like a
    real but empty mesh (root workspace CLAUDE.md degradation-path convention).
    """
    live_url = f"/message/{_escape(plugin_id)}/thread?live=1"
    cached_url = f"/message/{_escape(plugin_id)}/thread"
    badge = "LIVE" if live else "CACHED"
    badge_class = "badge-live" if live else "badge-cached"

    partitions = sorted({p for p in mesh.partitions.values() if p is not None})
    partition_text = ", ".join(str(p) for p in partitions) if partitions else "unknown"
    leader = next((n for n in mesh.nodes if n.is_leader), None)
    leader_text = _fmt_opt(leader.router_id) if leader is not None else "unknown"

    header = f"""
<h1>Thread mesh</h1>
<table class="header-facts">
<tr><th>Network</th><td>{_escape(mesh.network_name or "unknown")}</td></tr>
<tr><th>Channel</th><td>{_escape(_fmt_opt(mesh.channel))}</td></tr>
<tr><th>PAN</th><td>{_escape(f"0x{mesh.pan_id:04X}") if mesh.pan_id is not None else "unknown"}</td></tr>
<tr><th>Partition(s)</th><td>{_escape(partition_text)}</td></tr>
<tr><th>Leader router id</th><td>{_escape(leader_text)}</td></tr>
<tr><th>Generated</th><td>{_escape(generated_at)} <span class="badge {badge_class}">{badge}</span></td></tr>
</table>
<p class="links">
<a href="{live_url}">Refresh (live)</a> &middot;
<a href="{cached_url}">Cached (fast)</a>
</p>"""

    if error:
        banner = f'<p class="warn"><strong>Could not read the Thread mesh:</strong> {_escape(error)}</p>'
        body = f"{header}{banner}"
    else:
        body = f"""{header}
{_render_map(mesh)}
<h2>Flags</h2>
{_render_flags(mesh)}
<h2>Nodes</h2>
{_render_table(diags)}
{_render_unreadable(mesh, diags)}"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Indigo Matter bridge — Thread mesh</title>
<style>
 :root {{
   --fg: #222; --muted: #666; --bg: #fff; --border: #ddd;
   --box-owned-fill: #eaf3ea; --box-owned-stroke: #2e8b45;
   --box-foreign-fill: #f2f2f2; --box-foreign-stroke: #888;
   --box-child-fill: #f6f6f6; --box-child-stroke: #aaa;
 }}
 @media (prefers-color-scheme: dark) {{
   :root {{
     --fg: #e6e6e6; --muted: #aaa; --bg: #16171a; --border: #333;
     --box-owned-fill: #1e2d20; --box-owned-stroke: #4caf6b;
     --box-foreign-fill: #26272b; --box-foreign-stroke: #888;
     --box-child-fill: #202124; --box-child-stroke: #666;
   }}
 }}
 body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0 auto; max-width: 60rem; padding: 1.5rem; color: var(--fg); background: var(--bg); }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1rem; margin: 1.4rem 0 .3rem; color: var(--muted); }}
 .header-facts {{ border-collapse: collapse; margin: .5rem 0; }}
 .header-facts th {{ text-align: left; color: var(--muted); font-weight: 400; padding: .1rem .8rem .1rem 0; }}
 .header-facts td {{ padding: .1rem 0; }}
 .badge {{ display: inline-block; padding: .1rem .5rem; border-radius: .3rem; font-size: .75rem;
          font-weight: 700; letter-spacing: .04em; }}
 .badge-live {{ background: #d6f5df; color: #1e6b34; }}
 .badge-cached {{ background: #eee; color: #555; }}
 .links a {{ margin-right: 1rem; }}
 .msg {{ background: #fff6d6; border: 1px solid #e8d48a; padding: .7rem; border-radius: .4rem; }}
 .warn {{ background: #fdeaea; border: 1px solid #d99; padding: .7rem; border-radius: .4rem; }}
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
not be treated as exact; this page keys every node by its router id / RLOC16 instead,
which is not lossy.</footer>
</body></html>"""
