#!/usr/bin/env python3
"""Render the Thread mesh page (#346) against the real test fixture, to a file
on disk — the fastest way to eyeball a layout/CSS change without a live
matter-server or an Indigo install.

Loads ``tests/fixtures/thread_mesh/nodes.json`` the same way
``tests/test_thread_page.py`` does, builds the :class:`~thread_mesh.Mesh`, and
writes :func:`thread_page.render_thread_page`'s HTML to the path given on the
command line.

    python3 tools/render_thread_fixture.py out.html
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SERVER_PLUGIN_DIR = REPO_ROOT / "indigo-matter.indigoPlugin" / "Contents" / "Server Plugin"
sys.path.insert(0, str(SERVER_PLUGIN_DIR))

from thread_mesh import build_mesh, parse_node_diag  # noqa: E402  pylint: disable=wrong-import-position
from thread_page import render_thread_page  # noqa: E402  pylint: disable=wrong-import-position

FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "thread_mesh" / "nodes.json"
PLUGIN_ID = "com.simons-plugins.indigo-matter"


def _fixture_diags() -> list:
    raw_nodes = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    diags = []
    for raw in raw_nodes:
        attrs = raw["attributes"]
        name = attrs.get("0/40/3") or attrs.get("0/40/1") or ""
        diag = parse_node_diag(raw["node_id"], name, raw["available"], attrs)
        assert diag is not None
        diags.append(diag)
    return diags


def main() -> None:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <out.html>", file=sys.stderr)
        raise SystemExit(2)

    diags = _fixture_diags()
    mesh = build_mesh(diags)
    html = render_thread_page(mesh, diags, generated_at="2026-09-02T12:00:00Z",
                              live=False, plugin_id=PLUGIN_ID)

    out_path = Path(sys.argv[1])
    out_path.write_text(html, encoding="utf-8")
    print(f"wrote {out_path} ({len(html)} bytes)")


if __name__ == "__main__":
    main()
