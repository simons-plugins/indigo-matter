#!/usr/bin/env python3
"""Enumerate every writable Matter attribute from matter.js's data model.

Answers "what settings COULD this plugin offer, and what would each declaration
cost?" — the input to batching the work behind issue #186, rather than guessing
a roadmap from cluster names.

**Why the model rather than the spec PDF.** `@matter/model` carries the Matter
data model as generated JS, one file per cluster, and each attribute records
everything a :class:`DeviceSetting` declaration needs:

    Attribute({
      name: "HoldTime", id: 3, type: "uint16",
      access: "RW VM",
      conformance: "O",
      constraint: "holdTimeLimits.holdTimeMin to holdTimeLimits.holdTimeMax",
      quality: "N"
    })

The `constraint` names its own bounds source, so a setting's cluster, attribute,
type, writability AND where its limits come from are all derivable. The two
constraint grammars this script classifies as ``limits-struct`` and ``count``
are exactly the two bounds parsers `matter_handlers/settings.py` implements by
hand for the FP300 — which is the evidence that generating the rest is viable.

**Where the model lives.** It is a dependency of matter-server / the bridge
node, not something this repo vendors. On jarvis:

    ~/indigo-matter/node_modules/@matter/model/dist/esm/standard/elements

Copy that directory locally (or point --elements at a mount) and run:

    python3 tools/enumerate_settings.py --elements <dir> --out settings-coverage.md

The elements directory also ships with the bridge node's own dependencies, so
in a checkout with `bridge-node/node_modules` populated the path is simply
`bridge-node/node_modules/@matter/model/dist/esm/standard/elements`.

**Two outputs, and only one of them is for a human.** ``--out`` writes the
markdown report above — the roadmap input. ``--emit-table`` writes
``matter_handlers/writable_attributes.py``, a generated Python module the
PLUGIN imports at runtime (issue #191): the settable-attribute report has to
turn a device's AttributeList into "…and 4 of these are settable", and
writability is spec metadata that never appears on the wire. That table is a
**build artefact, checked in**, not computed at runtime — the plugin must never
import ``@matter/model`` (only the bridge node may; workspace ADR-0006), and
the Python side has no Node available to it in the first place.

    python3 tools/enumerate_settings.py --elements <dir> --emit-table

**Regenerate it when the pinned matter.js version moves** — i.e. when
``bridge-node/package.json``'s ``@matter/*`` pin changes. The generated header
records the version it came from so the two can be compared; a test asserts the
file parses and is non-empty, not that it is current, because "current" is only
knowable with the model in hand.

This reads only; it writes the files you ask for and touches nothing else.

**What it deliberately does NOT do.** It does not decide what is a *setting*.
Plenty of writable attributes are controls the plugin already drives (a level, a
setpoint), and privilege does not separate them: `HoldTime` is `RW VM` while
`CurrentSensitivityLevel` is `RW VO`, and both are settings. So the report ranks
and annotates; a human still picks. It also cannot tell you whether a device
implements an attribute — only the device can, which is what the runtime
AttributeList (0xFFFB) check is for.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

# Clusters this plugin has an inbound handler for, parsed from the handler
# sources rather than imported: importing them drags in `protocol` and the
# handler registry, and this script must run without the plugin's environment.
HANDLER_DIR = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
               / "Contents" / "Server Plugin" / "matter_handlers")

#: Attribute props we care about, as they appear in the generated JS.
_PROP = re.compile(
    r'(name|id|type|access|conformance|constraint|quality|default)\s*:\s*'
    r'("(?:[^"\\]|\\.)*"|0x[0-9a-fA-F]+|\d+|true|false)')


def _unquote(raw: str):
    if raw.startswith('"'):
        return raw[1:-1].replace('\\"', '"').replace("\\xA7", "§")
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw, 0)
    except ValueError:
        return raw


def _balanced_object(text: str, start: int) -> tuple[str, int]:
    """Return the ``{...}`` beginning at *start*, respecting strings."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
    return "", len(text)


def _balanced_parens(text: str, start: int) -> str:
    """Return the ``(...)`` beginning at *start*, respecting strings.

    Distinct from :func:`_balanced_object` because a ``Command(...)``'s ``Field``
    blocks are SIBLING ARGUMENTS to its property object, not nested inside it::

        Command(
            { name: "OnWithTimedOff", id: 66, … },   <- _balanced_object stops here
            Field({ name: "OnTime", … }),            <- but the fields are out here
        )

    Scanning braces therefore finds no fields at all and yields an empty table,
    which would silently disable the filter that depends on it (#197). Hence
    KNOWN_COMMAND_FIELD_ATTRIBUTES: the generator refuses to write a table that
    does not contain every pair it already knows about.
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _props(obj: str) -> dict:
    return {k: _unquote(v) for k, v in _PROP.findall(obj)}


def parse_cluster(path: Path) -> tuple[dict, list[dict]]:
    """Return ``(cluster_props, [attribute_props, …])`` for one element file."""
    text = path.read_text(encoding="utf-8")
    cluster: dict = {}
    marker = "Cluster(" if "Cluster(" in text else None
    if marker:
        idx = text.index(marker) + len(marker)
        brace = text.find("{", idx)
        if brace != -1:
            obj, _ = _balanced_object(text, brace)
            cluster = _props(obj)

    attributes = []
    for match in re.finditer(r"Attribute\(", text):
        brace = text.find("{", match.end())
        if brace == -1 or brace > match.end() + 4:
            continue  # `Attribute(` not immediately followed by its object
        obj, _ = _balanced_object(text, brace)
        props = _props(obj)
        if props.get("name"):
            attributes.append(props)
    return cluster, attributes


def parse_command_fields(path: Path) -> set:
    """Every ``Field`` NAME appearing inside a ``Command(...)`` in one element file.

    Names rather than ids because a command field's id is its position in the
    command's payload and has nothing to do with the attribute id it adjusts —
    ``OnWithTimedOff``'s ``OnTime`` is field 1 and attribute 0x4001. The name is
    the only thing the two share, and the model spells it identically.
    """
    text = path.read_text(encoding="utf-8")
    fields: set = set()
    for match in re.finditer(r"Command\(", text):
        paren = text.find("(", match.start())
        block = _balanced_parens(text, paren)
        for field in re.finditer(r"Field\(", block):
            brace = block.find("{", field.end())
            # Reject on non-whitespace rather than on DISTANCE. A fixed window
            # ("the brace is within N characters") silently skips every field
            # the model pretty-prints across lines — which is exactly the
            # list-typed form, `Field(\n  { name: "Arl", … },\n  Field({…})\n)`.
            # On 0.17.8 that dropped 53 sites and 39 names. None of them
            # collided with a writable attribute, so the emitted table was
            # right by luck; a model bump that moves a real command field into
            # that form would put a command parameter back into the report with
            # KNOWN_COMMAND_FIELD_ATTRIBUTES still green, because that floor
            # only asserts the pairs it already knows.
            if brace == -1 or block[field.end():brace].strip():
                continue  # `Field(` whose first argument is not its own object
            obj, _ = _balanced_object(block, brace)
            name = _props(obj).get("name")
            if name:
                fields.add(str(name))
    return fields


# --- constraint grammars -----------------------------------------------------
# Each maps to a bounds strategy a DeviceSetting would declare. The first two
# are already implemented for the FP300 (settings.parse_limits_struct /
# parse_level_count); the rest are what the next batch would need.

_C_STRUCT = re.compile(r"^(\w+)\.(\w+) to (\w+)\.(\w+)$")
_C_COUNT = re.compile(r"^max (\w+) - 1$")
_C_ATTRS = re.compile(r"^(\w+) to (\w+)$")
_C_STATIC = re.compile(r"^(?:(\d+) to (\d+)|max (\d+)|min (\d+))$")


def classify_constraint(constraint) -> tuple[str, str]:
    """(strategy, note) for a constraint expression."""
    if not constraint or not isinstance(constraint, str):
        return "none", "no declared bounds — needs a spec/type range or no validation"
    c = constraint.strip()
    m = _C_STRUCT.match(c)
    if m:
        return "limits-struct", f"bounds from {m.group(1)} (struct fields {m.group(2)}/{m.group(4)})"
    m = _C_COUNT.match(c)
    if m:
        return "count", f"bounds from {m.group(1)} (a count: 0..n-1)"
    m = _C_STATIC.match(c)
    if m:
        return "static", f"fixed range in the spec: {c}"
    m = _C_ATTRS.match(c)
    if m:
        return "two-attrs", f"bounds from attributes {m.group(1)} and {m.group(2)}"
    if c in ("desc", "all"):
        return "none", "spec says 'desc' — the device defines it; no machine-readable bounds"
    return "other", f"unparsed constraint: {c}"


def handled_clusters() -> dict:
    """``{cluster_id: handler_file}`` for clusters with an inbound handler."""
    found = {}
    if not HANDLER_DIR.is_dir():
        return found
    for path in sorted(HANDLER_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*cluster_id\s*=\s*(0x[0-9a-fA-F]+|\d+)",
                                 source, re.M):
            value = int(match.group(1), 0)
            if value:
                found.setdefault(value, path.name)
        # Non-primary handlers and the settings registry name clusters as
        # module constants rather than a cluster_id class attribute.
        for match in re.finditer(r"^CLUSTER_\w+\s*=\s*(0x[0-9a-fA-F]+)", source, re.M):
            found.setdefault(int(match.group(1), 0), path.name)
    return found


def declared_settings() -> set:
    """``{(cluster_id, attribute_id)}`` already declared in settings.py.

    Resolved to NUMBERS, not names. Matching a model attribute by name against
    the declaration's expression text marks the wrong thing declared as soon as
    two clusters share an attribute name (``CurrentSensitivityLevel`` and
    ``Occupancy`` are both ``id 0``), and misses any declaration written with a
    numeric literal. The constants are read from the same file, so this stays a
    pure text parse with no plugin import.
    """
    path = HANDLER_DIR / "settings.py"
    if not path.is_file():
        return set()
    source = path.read_text(encoding="utf-8")
    consts = {name: int(value, 0) for name, value in
              re.findall(r"^([A-Z][A-Z0-9_]*)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*$",
                         source, re.M)}

    def resolve(token):
        token = token.strip()
        if token in consts:
            return consts[token]
        try:
            return int(token, 0)
        except ValueError:
            return None

    pairs = set()
    for block in re.finditer(r"DeviceSetting\((.*?)\n    \)", source, re.S):
        body = block.group(1)
        cluster = re.search(r"cluster=([\w.]+)", body)
        attribute = re.search(r"attribute=([\w.]+)", body)
        if not (cluster and attribute):
            continue
        cluster_id, attribute_id = resolve(cluster.group(1)), resolve(attribute.group(1))
        if cluster_id is not None and attribute_id is not None:
            pairs.add((cluster_id, attribute_id))
    return pairs


#: Where ``--emit-table`` writes its generated module by default.
TABLE_PATH = HANDLER_DIR / "writable_attributes.py"


def model_version(elements: Path) -> str:
    """The ``@matter/model`` version the *elements* directory belongs to.

    ``.../@matter/model/dist/esm/standard/elements`` → four levels up is the
    package root. Returns ``"unknown"`` rather than failing: a table generated
    from a directory copied out of its package is still a correct table, it just
    cannot say what produced it, and refusing to emit would be the wrong trade.
    """
    try:
        raw = (elements.parents[3] / "package.json").read_text(encoding="utf-8")
        match = re.search(r'"version"\s*:\s*"([^"]+)"', raw)
        if match:
            return match.group(1)
    except (OSError, IndexError):
        pass
    return "unknown"


#: The command-parameter attributes known to exist in the pinned model, asserted
#: before the table is written. A parser that finds none is the failure mode this
#: guards: ``Field`` blocks sit outside the ``Command``'s property object, so the
#: obvious brace scan returns nothing, the emitted table comes out empty, and the
#: report silently goes back to naming command parameters as settings (#197).
#:
#: Not a hard-coded answer — the generator derives the whole set. This is the
#: floor it must reach. If a matter.js bump legitimately removes one, this list
#: is what forces the removal to be noticed rather than absorbed.
KNOWN_COMMAND_FIELD_ATTRIBUTES = {
    (0x0003, 0x0000),  # Identify.IdentifyTime      <- Identify()
    (0x0006, 0x4001),  # OnOff.OnTime               <- OnWithTimedOff()
    (0x0006, 0x4002),  # OnOff.OffWaitTime          <- OnWithTimedOff()
    (0x0030, 0x0000),  # GeneralCommissioning.Breadcrumb <- ArmFailSafe(), SetRegulatoryConfig()
}


def command_parameters(writable: dict, names: dict, command_fields: dict) -> dict:
    """``{cluster_id: {attribute_id}}`` for writable attributes a command owns.

    Matched by NAME within one cluster: a command field's id is its position in
    the payload and says nothing about the attribute it adjusts (OnWithTimedOff's
    ``OnTime`` is field 1 and attribute 0x4001), but the model spells the two
    identically.

    Raises rather than returning a short answer. An empty or partial result means
    the Command/Field parse failed, and a table written from it would silently
    put command parameters back into the settable-attribute report.
    """
    parameters: dict[int, set] = {}
    for cluster_id, field_names in command_fields.items():
        for attr_id in writable.get(cluster_id, ()):
            if names.get(cluster_id, {}).get(attr_id) in field_names:
                parameters.setdefault(cluster_id, set()).add(attr_id)

    found = {(c, a) for c, ids in parameters.items() for a in ids}
    missing = KNOWN_COMMAND_FIELD_ATTRIBUTES - found
    if missing:
        raise SystemExit(
            "refusing to write a table missing known command-parameter attributes: "
            + ", ".join(f"0x{c:04X}/0x{a:04X}" for c, a in sorted(missing))
            + f" ({len(missing)} of {len(KNOWN_COMMAND_FIELD_ATTRIBUTES)} missing). "
            + "ALL four missing means the Command/Field parse found nothing — start at "
            + "_balanced_parens. SOME missing means the model's shape changed for that "
            + "cluster; check it against the spec before touching this list.")
    return parameters


def emit_table(elements: Path, path: Path) -> tuple[int, int, dict]:
    """Write the generated attribute table. Returns ``(clusters, writable, parameters)``.

    Deliberately covers EVERY cluster in the model, not only the ones this
    plugin handles: the report this feeds exists to describe devices the plugin
    does NOT support yet, so restricting it to handled clusters would blind it
    to precisely the devices it is for.

    Names as well as ids, and for every attribute rather than only the writable
    ones, because the explorer (issue #191 deliverable A) dumps what a device
    implements and a bare ``0x4003`` is no more use to a user than the raw
    ``get_node`` JSON they already could not read.
    """
    clusters: dict[int, str] = {}
    names: dict[int, dict[int, str]] = {}
    writable: dict[int, set] = {}
    command_fields: dict[int, set] = {}

    for source in sorted(elements.glob("*.element.js")):
        cluster, attributes = parse_cluster(source)
        name, cluster_id = cluster.get("name"), cluster.get("id")
        if not name or not isinstance(cluster_id, int):
            continue
        # Unioned across files, where `clusters` below is first-writer-wins. The
        # asymmetry is deliberate: for an exclusion list the conservative answer is
        # the larger set. Moot on 0.17.8 — no cluster id appears in two files — but
        # the two rules sit three lines apart and the difference should be stated.
        command_fields.setdefault(cluster_id, set()).update(parse_command_fields(source))
        # A cluster id can appear twice across the model's element files (an
        # alias or a revision split). First writer wins, deterministically,
        # because the files are walked in sorted order.
        clusters.setdefault(cluster_id, str(name))
        for attr in attributes:
            attr_id, attr_name = attr.get("id"), attr.get("name")
            if not isinstance(attr_id, int) or not attr_name:
                continue
            names.setdefault(cluster_id, {}).setdefault(attr_id, str(attr_name))
            if "RW" in (attr.get("access") or ""):
                writable.setdefault(cluster_id, set()).add(attr_id)

    parameters = command_parameters(writable, names, command_fields)
    version = model_version(elements)
    lines = [
        '"""Matter attribute names and writability, generated from `@matter/model`.',
        "",
        "DO NOT EDIT. Regenerate with:",
        "",
        "    python3 tools/enumerate_settings.py \\",
        "        --elements bridge-node/node_modules/@matter/model/dist/esm/standard/elements \\",
        "        --emit-table",
        "",
        "**Why this is a checked-in build artefact rather than a runtime lookup.**",
        "Writability is spec metadata: a device's AttributeList (0xFFFB) says which",
        "attributes it *implements* and never which are writable, so the settable-",
        "attribute report (issue #191) can only answer that from the data model. The",
        "data model lives in `@matter/model`, which is a Node package — and the Python",
        "plugin must never import matter.js (workspace ADR-0006 confines it to the",
        "bridge node). So it is generated, checked in, and regenerated when the pinned",
        "matter.js version moves.",
        "",
        "The table is the SPEC's answer, not any device's. It says what an attribute",
        "would be if implemented; whether a given unit implements it is a question only",
        "that unit's AttributeList can answer.",
        '"""',
        "from __future__ import annotations",
        "",
        f'#: The ``@matter/model`` release this was generated from.',
        f'MODEL_VERSION = "{version}"',
        "",
        "#: ``{cluster_id: cluster_name}`` for every cluster in the data model.",
        "CLUSTER_NAMES: dict[int, str] = {",
    ]
    for cluster_id in sorted(clusters):
        lines.append(f'    0x{cluster_id:04X}: "{clusters[cluster_id]}",')
    lines += [
        "}",
        "",
        "#: ``{cluster_id: {attribute_id: attribute_name}}`` — every attribute the",
        "#: model defines, writable or not. Global attributes (0xFFF8-0xFFFD) are",
        "#: implicit on every cluster and are named in ``settings_report`` instead.",
        "ATTRIBUTE_NAMES: dict[int, dict[int, str]] = {",
    ]
    for cluster_id in sorted(names):
        lines.append(f"    0x{cluster_id:04X}: {{")
        for attr_id in sorted(names[cluster_id]):
            lines.append(f'        0x{attr_id:04X}: "{names[cluster_id][attr_id]}",')
        lines.append("    },")
    lines += [
        "}",
        "",
        "#: ``{cluster_id: frozenset(attribute_ids)}`` — the attributes the spec",
        "#: declares WRITABLE (model access contains ``RW``). Ids only: the names",
        "#: live in ATTRIBUTE_NAMES, so nothing is stored twice.",
        "WRITABLE_ATTRIBUTES: dict[int, frozenset] = {",
    ]
    for cluster_id in sorted(writable):
        ids = ", ".join(f"0x{a:04X}" for a in sorted(writable[cluster_id]))
        lines.append(f"    0x{cluster_id:04X}: frozenset({{{ids}}}),")
    lines += [
        "}",
        "",
        "#: ``{cluster_id: frozenset(attribute_ids)}`` — writable attributes that",
        "#: are ALSO a field of a command on their OWN cluster. These are set BY",
        "#: that command and are not configuration: the value has no meaning until",
        "#: the command runs, and the command's own implementation owns it. OnOff's",
        "#: OnTime is the case that proved it — writable, settable, verified by",
        "#: read-back, and completely inert, because a WRITE never starts the",
        "#: countdown; only OnWithTimedOff does (matter.js says so in as many words",
        "#: at OnOffServer.js:38-40). Off then zeroes it, under the Lighting-feature",
        "#: guard that every device carrying the attribute satisfies (issue #197).",
        "COMMAND_FIELD_ATTRIBUTES: dict[int, frozenset] = {",
    ]
    for cluster_id in sorted(parameters):
        ids = ", ".join(f"0x{a:04X}" for a in sorted(parameters[cluster_id]))
        lines.append(f"    0x{cluster_id:04X}: frozenset({{{ids}}}),")
    lines += ["}", ""]

    path.write_text("\n".join(lines), encoding="utf-8")
    return len(clusters), sum(len(v) for v in writable.values()), parameters


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--elements", required=True, type=Path,
                    help="@matter/model .../standard/elements directory")
    ap.add_argument("--out", type=Path, help="write the markdown report here")
    ap.add_argument("--all-clusters", action="store_true",
                    help="include clusters this plugin has no handler for")
    ap.add_argument("--emit-table", nargs="?", const=TABLE_PATH, type=Path,
                    metavar="PATH", default=None,
                    help="write the generated writable_attributes.py the plugin "
                         f"imports (default: {TABLE_PATH.name} beside the handlers)")
    args = ap.parse_args()

    if not args.elements.is_dir():
        print(f"not a directory: {args.elements}", file=sys.stderr)
        return 2

    if args.emit_table is not None:
        clusters, writable, parameters = emit_table(args.elements, args.emit_table)
        print(f"wrote {args.emit_table} ({clusters} clusters, {writable} writable attributes, "
              f"@matter/model {model_version(args.elements)})")
        # Name the exclusions rather than counting them. This set decides what the
        # settable-attribute report will never mention again, so a change to it is
        # the single most consequential thing a regeneration can do — and the least
        # visible, since the diff is three lines at the end of a 1300-line file.
        print(f"  command parameters excluded from the settable report "
              f"({sum(len(v) for v in parameters.values())}):")
        for cluster_id in sorted(parameters):
            for attr_id in sorted(parameters[cluster_id]):
                print(f"    0x{cluster_id:04X}/0x{attr_id:04X}")
        # Emitting the table is a build step, not a report request: only fall
        # through to the markdown when one was actually asked for.
        if args.out is None:
            return 0

    handled = handled_clusters()
    declared = declared_settings()
    rows = defaultdict(list)
    totals = defaultdict(int)
    cluster_count = 0

    for path in sorted(args.elements.glob("*.element.js")):
        cluster, attributes = parse_cluster(path)
        name, cluster_id = cluster.get("name"), cluster.get("id")
        if not name or not isinstance(cluster_id, int):
            continue
        cluster_count += 1
        is_handled = cluster_id in handled
        if not is_handled and not args.all_clusters:
            continue
        for attr in attributes:
            access = attr.get("access") or ""
            if "RW" not in access:
                continue
            strategy, note = classify_constraint(attr.get("constraint"))
            totals[strategy] += 1
            totals["writable"] += 1
            rows[(name, cluster_id, is_handled)].append({
                "name": attr.get("name"), "id": attr.get("id"),
                "type": attr.get("type"), "access": access,
                "conformance": attr.get("conformance") or "",
                "strategy": strategy, "note": note,
            })

    lines = ["# Writable Matter attributes — candidate device settings", "",
             f"Generated by `tools/enumerate_settings.py` from {cluster_count} clusters "
             f"in `@matter/model`.", "",
             "`strategy` is how a `DeviceSetting` would get its bounds. "
             "**limits-struct** and **count** are already implemented "
             "(`parse_limits_struct` / `parse_level_count`), so those cost a "
             "declaration and nothing else.", "",
             "Writable ≠ setting: many of these are controls the plugin already "
             "drives. Privilege does not separate them — `HoldTime` is `RW VM` and "
             "`CurrentSensitivityLevel` is `RW VO`, and both are settings.", ""]

    lines.append("## Summary\n")
    lines.append(f"- **{totals['writable']} writable attributes** on the clusters shown")
    for strategy in ("limits-struct", "count", "static", "two-attrs", "none", "other"):
        if totals[strategy]:
            ready = " ← parser already exists" if strategy in ("limits-struct", "count") else ""
            lines.append(f"- `{strategy}`: {totals[strategy]}{ready}")
    lines.append("")

    for (name, cluster_id, is_handled), attrs in sorted(rows.items()):
        flag = "" if is_handled else "  _(no inbound handler yet)_"
        lines.append(f"## {name} (0x{cluster_id:04X}){flag}\n")
        lines.append("| Attribute | Id | Type | Access | Conf | Bounds strategy | Notes |")
        lines.append("|---|---|---|---|---|---|---|")
        for a in sorted(attrs, key=lambda x: x["id"] if isinstance(x["id"], int) else 0):
            done = " ✅ declared" if (cluster_id, a["id"]) in declared else ""
            lines.append(
                f"| {a['name']}{done} | {a['id']} | {a['type']} | {a['access']} | "
                f"{a['conformance']} | {a['strategy']} | {a['note']} |")
        lines.append("")

    report = "\n".join(lines)
    if args.out:
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out} ({totals['writable']} writable attributes)")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
