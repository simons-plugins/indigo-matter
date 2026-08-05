"""XAC10 — no matter.js import exists anywhere in the Python.

The PRD names this as an acceptance criterion enforced by a test
(`docs/PRD-indigo-matter-export.md` §8, XAC10, guarding XG6). The architecture
it protects: matter.js lives **only** in `bridge-node/`, behind the WebSocket
contract in `docs/BRIDGE_PROTOCOL.md`. The moment a Python module reaches for a
matter.js package directly — via a Node shim, a transpiler bridge, a
`js2py`-style loader — the plugin has two Matter stacks, two versions of the
spec, and a second place for endpoint identity to drift.

**Why AST and not grep.** A raw text search for "matter.js" fires on every
sentence of prose in this repo that explains the split ("matter.js FanControl is
a stub", and so on), and a search for "matter" hits `matter_client`,
`matter_model`, `matter_handlers`, `matter_protocol` — all of which are OURS.
So the check parses each file and inspects only the module names in real
`import` / `from ... import` statements, against a denylist of the shapes an
npm package would take. That is precise enough to be trusted, which is the
whole point of an acceptance-criterion test: one that cries wolf gets deleted.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SERVER_PLUGIN_DIR = (
    Path(__file__).parent.parent
    / "indigo-matter.indigoPlugin" / "Contents" / "Server Plugin"
)

#: Top-level module names that would mean a matter.js package. `matter` itself
#: is here because `from matter.js import ...` and `import matter` both root
#: there; every module WE own is prefixed (`matter_client`, `matter_model`,
#: `matter_handlers`, …) and so never matches.
DENIED_ROOTS = frozenset({
    "matter", "matterjs", "matter_js", "matternode", "matter_node",
    "node_matter", "matterbridge", "chip", "chip_tool",
})

#: Substrings that can only occur in an npm-style specifier.
DENIED_SUBSTRINGS = ("@matter", "matter.js", "matter-js")


def _python_files() -> list[Path]:
    return sorted(SERVER_PLUGIN_DIR.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> list[str]:
    """Every module named by an import statement in ``tree``."""
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # node.module is None for `from . import x` — a relative import,
            # which by construction cannot name an npm package.
            if node.module:
                modules.append(node.module)
    return modules


def test_there_are_python_files_to_check():
    """A silent zero-file sweep would pass XAC10 forever without checking it."""
    assert len(_python_files()) > 5


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_xac10_no_matter_js_import(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for module in _imported_modules(tree):
        root = module.split(".")[0]
        assert root not in DENIED_ROOTS, (
            f"{path.name} imports {module!r}: matter.js belongs in bridge-node/, "
            "behind the BRIDGE_PROTOCOL WebSocket contract (XAC10/XG6)."
        )
        lowered = module.lower()
        for needle in DENIED_SUBSTRINGS:
            assert needle not in lowered, (
                f"{path.name} imports {module!r}, which names an npm matter.js "
                "package (XAC10/XG6)."
            )


def test_our_own_matter_prefixed_modules_are_not_flagged():
    """The denylist must not be so broad it bans the plugin's own modules.

    `matter_client`, `matter_model` and `matter_handlers` are ours and are
    imported all over `plugin.py`; a check that flagged them would be turned
    off within a week.
    """
    ours = ["matter_client", "matter_model", "matter_handlers.registry",
            "matter_protocol", "bridge_client", "export_catalog"]
    for module in ours:
        assert module.split(".")[0] not in DENIED_ROOTS, module
        assert not any(needle in module for needle in DENIED_SUBSTRINGS), module


def test_the_denylist_would_actually_catch_a_matter_js_import(tmp_path):
    """Pin the check itself: a planted violation must fail."""
    planted = tmp_path / "planted.py"
    for source in ("import matter_js", "from matter.js import Endpoint",
                   "import matter", "from matterbridge import x"):
        planted.write_text(source, encoding="utf-8")
        with pytest.raises(AssertionError):
            test_xac10_no_matter_js_import(planted)
