"""Tests for the table generator — `tools/enumerate_settings.py` (#191, #197).

Nothing imported this module before #197, which is how FOUR parser defects
reached the checked-in artefact. The shipped table claimed 110 writable
attributes; the model has **137**.

* `parse_command_fields` located a `Field(`'s object literal by a fixed
  character distance, so it skipped the multi-line form — 53 sites, 39 names.
  Harmless by luck: none collided with a writable attribute on its own cluster.
* `parse_cluster` had the same window on `Attribute(` — 265 sites, **14 of them
  writable**, so the report was blind to `FanControl.RockSetting`,
  `Thermostat.Presets` and ten others from the day #191 shipped.
* Writability was tested as `"RW" in access`, which misses matter.js's `R[W]`
  (conditionally writable) form — **13 more**, twelve of them DoorLock settings
  including `AutoRelockTime`, the working analogue of the feature #197 retired.
* `_props` was a flat `findall` into a dict comprehension, so a nested
  `default: {type, name}` overwrote the attribute's own — five attributes came
  out under the WRONG NAME, and the report printed those names to users. The
  only one of the four that produced wrong output rather than missing output.

**That is the failure mode these tests exist for, and why they are worth having
even though the generator is a build script.** The repo already carries the same
argument in `test_settings_report.test_the_parse_that_backs_the_parity_check_actually_finds_something`
("Guards the guard… the exact shape of the tautology #186 shipped") — a text
parse that quietly matches nothing is indistinguishable from a clean result.

`KNOWN_COMMAND_FIELD_ATTRIBUTES` is a floor, not a substitute for these: it only
fires when a human runs the generator (CI has no `@matter/model` to run it
against), and it only asserts the four pairs it already knows. Neither property
catches a parse that under-reports everything else.

Everything here is synthetic input written in the model's own shape. Nothing
reads `bridge-node/node_modules`, so these run in CI where the model is absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import enumerate_settings as es  # noqa: E402


# ---------------------------------------------------------------------------
# _balanced_parens — the scan that reaches a Command's sibling Field arguments
# ---------------------------------------------------------------------------

def test_balanced_parens_returns_the_whole_call():
    text = 'Command({ name: "X" }, Field({ name: "Y" }))'
    assert es._balanced_parens(text, text.index("(")) == '({ name: "X" }, Field({ name: "Y" }))'


def test_balanced_parens_spans_newlines():
    """The form the whole bug was about — the model pretty-prints any command
    carrying fields."""
    text = 'Command(\n  { name: "X", id: 1 },\n  Field({ name: "Y", id: 0 })\n)'
    assert es._balanced_parens(text, text.index("(")).count("Field(") == 1


def test_balanced_parens_ignores_parens_inside_strings():
    """A `)` in a description or an xref must not truncate the block early —
    truncation is silent and drops every field after the cut."""
    text = 'Command({ name: "X", details: "see (a) and (b" }, Field({ name: "Kept" }))'
    assert "Kept" in es._balanced_parens(text, text.index("("))


def test_balanced_parens_ignores_an_escaped_quote():
    text = 'Command({ name: "a \\" ) b" }, Field({ name: "Kept" }))'
    assert "Kept" in es._balanced_parens(text, text.index("("))


def test_balanced_parens_returns_empty_on_an_unclosed_call():
    """Better to yield nothing for one command than to run to end-of-file and
    swallow every later cluster's fields into it."""
    assert es._balanced_parens('Command({ name: "X" }', 7) == ""


# ---------------------------------------------------------------------------
# parse_command_fields
# ---------------------------------------------------------------------------

def _element(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "thing.element.js"
    path.write_text(body, encoding="utf-8")
    return path


def test_command_field_names_are_found(tmp_path):
    path = _element(tmp_path, '''
export const x = Cluster({ name: "OnOff", id: 6 },
  Attribute({ name: "OnTime", id: 0x4001, access: "RW VO", conformance: "LT" }),
  Command(
    { name: "OnWithTimedOff", id: 66, conformance: "LT" },
    Field({ name: "OnOffControl", id: 0, conformance: "M" }),
    Field({ name: "OnTime", id: 1, conformance: "M" }),
    Field({ name: "OffWaitTime", id: 2, conformance: "M" })
  )
)''')
    assert es.parse_command_fields(path) == {"OnOffControl", "OnTime", "OffWaitTime"}


def test_a_multiline_field_is_found(tmp_path):
    """THE regression. A list-typed field is printed with its own nested Field on
    separate lines; a fixed-width window skips the outer name and captures only
    the inner one. Here that would return {"entry"} and lose "Arl"."""
    path = _element(tmp_path, '''
export const x = Cluster({ name: "AccessControl", id: 31 },
  Command(
    { name: "ReviewFabricRestrictions", id: 0 },
    Field(
      { name: "Arl", id: 0, type: "list" },
      Field({ name: "entry", type: "SomeStruct" })
    )
  )
)''')
    assert "Arl" in es.parse_command_fields(path)


def test_a_field_whose_first_argument_is_not_an_object_is_skipped(tmp_path):
    """The check the window was standing in for. `Field(SOME_CONST, {...})` must
    not have the trailing object read as its own properties."""
    path = _element(tmp_path, '''
export const x = Cluster({ name: "Thing", id: 1 },
  Command({ name: "Do", id: 0 }, Field(SHARED_FIELD, { name: "NotMine" }))
)''')
    assert es.parse_command_fields(path) == set()


def test_fields_outside_a_command_are_not_command_fields(tmp_path):
    """A Datatype's or an Attribute's own fields are not command parameters —
    conflating them would exclude real settings from the report."""
    path = _element(tmp_path, '''
export const x = Cluster({ name: "OnOff", id: 6 },
  Datatype({ name: "StartUpOnOffEnum", type: "enum8" },
    Field({ name: "NotACommandField", id: 0, conformance: "M" })
  )
)''')
    assert es.parse_command_fields(path) == set()


def test_a_file_with_no_commands_yields_nothing(tmp_path):
    assert es.parse_command_fields(_element(tmp_path, "export const x = 1;")) == set()


# ---------------------------------------------------------------------------
# parse_cluster — the same window bug, on the attributes themselves
# ---------------------------------------------------------------------------

def test_a_multiline_attribute_is_found(tmp_path):
    """THE bigger regression. A list-typed attribute carries its entry Field on
    its own lines, and a fixed-width window skipped the whole thing — 14 writable
    attributes missing from the shipped table, so the report could never name
    them however plainly a device implemented them."""
    path = _element(tmp_path, '''
export const x = Cluster({ name: "FanControl", id: 514 },
  Attribute(
    { name: "RockSetting", id: 0x8, type: "map8", access: "RW VO", conformance: "O" },
    Field({ name: "RockLeftRight", constraint: "0" })
  ),
  Attribute({ name: "FanMode", id: 0x0, access: "RW VO", conformance: "M" })
)''')
    _, attributes = es.parse_cluster(path)
    assert {a["name"] for a in attributes} == {"RockSetting", "FanMode"}
    rock = next(a for a in attributes if a["name"] == "RockSetting")
    assert "RW" in rock["access"], "and its access must survive the parse, or it is not writable"


def test_an_attribute_whose_first_argument_is_not_an_object_is_skipped(tmp_path):
    """The check the window stood in for — kept, so the fix does not simply
    widen the net."""
    path = _element(tmp_path, '''
export const x = Cluster({ name: "Thing", id: 1 },
  Attribute(SHARED_ATTRIBUTE, { name: "NotMine", id: 0, access: "RW VO" })
)''')
    _, attributes = es.parse_cluster(path)
    assert attributes == []


# ---------------------------------------------------------------------------
# command_parameters — the derivation and its floor
# ---------------------------------------------------------------------------

#: The four the pinned model really contains, as `command_parameters` needs them.
_REAL = {
    "writable": {0x0003: {0x0000}, 0x0006: {0x4001, 0x4002, 0x4003}, 0x0030: {0x0000}},
    "names": {
        0x0003: {0x0000: "IdentifyTime"},
        0x0006: {0x4001: "OnTime", 0x4002: "OffWaitTime", 0x4003: "StartUpOnOff"},
        0x0030: {0x0000: "Breadcrumb"},
    },
    "fields": {
        0x0003: {"IdentifyTime", "EffectIdentifier"},
        0x0006: {"OnOffControl", "OnTime", "OffWaitTime"},
        0x0030: {"Breadcrumb", "ExpiryLengthSeconds"},
    },
}


def test_a_writable_attribute_named_like_a_command_field_is_a_parameter():
    assert es.command_parameters(_REAL["writable"], _REAL["names"], _REAL["fields"]) == {
        0x0003: {0x0000},
        0x0006: {0x4001, 0x4002},
        0x0030: {0x0000},
    }


def test_a_real_setting_is_not_swept_up():
    """StartUpOnOff sits on the same cluster as two parameters and is the one
    setting OnOff genuinely has. If the match were per-cluster rather than
    per-name it would vanish from the plugin's only OnOff setting."""
    result = es.command_parameters(_REAL["writable"], _REAL["names"], _REAL["fields"])
    assert 0x4003 not in result[0x0006]


def test_a_command_field_matching_a_READ_ONLY_attribute_is_ignored():
    """30 attributes in the pinned model share a name with a command field on
    their own cluster and are saved only by being read-only — OpenDuration,
    CookTime, six TemperatureAlarm thresholds. They must not appear, because a
    non-writable attribute is never a candidate setting in the first place.

    Carries the four real clusters alongside the valve because the floor check is
    deliberately inseparable from the derivation — you cannot derive a set without
    it being validated, which is the property that makes the guard unskippable.
    """
    result = es.command_parameters(
        {**_REAL["writable"], 0x0081: {0x0002}},          # only 0x0002 is writable
        {**_REAL["names"],
         0x0081: {0x0001: "OpenDuration", 0x0002: "DefaultOpenDuration"}},
        {**_REAL["fields"], 0x0081: {"OpenDuration"}},
    )
    assert 0x0081 not in result, "OpenDuration is read-only, so never a candidate"


def test_the_floor_rejects_a_parse_that_found_nothing():
    """The scenario the guard exists for: a wrong scan yields no fields, the
    table comes out empty, and the report silently goes back to naming command
    parameters as settings."""
    with pytest.raises(SystemExit) as raised:
        es.command_parameters(_REAL["writable"], _REAL["names"], {})
    assert "0x0006/0x4001" in str(raised.value)


def test_the_floor_rejects_a_PARTIAL_parse(tmp_path):
    """A truncated block or a model reshuffle can drop one cluster's fields while
    leaving the rest — which still yields a non-empty table. The message has to
    distinguish the two, because "all four missing" and "one missing" send a
    maintainer to different places."""
    fields = dict(_REAL["fields"])
    fields[0x0006] = {"OnOffControl"}          # OnTime/OffWaitTime lost
    with pytest.raises(SystemExit) as raised:
        es.command_parameters(_REAL["writable"], _REAL["names"], fields)
    message = str(raised.value)
    assert "2 of 4 missing" in message
    assert "SOME missing" in message


def test_the_floor_matches_the_documented_four():
    """`KNOWN_COMMAND_FIELD_ATTRIBUTES` is quoted in ADR-0005, in the generated
    table's docstring and in the report's own tests. It is the one copy the
    generator enforces, so it must be the four the ADR names."""
    assert es.KNOWN_COMMAND_FIELD_ATTRIBUTES == {
        (0x0003, 0x0000),  # Identify.IdentifyTime
        (0x0006, 0x4001),  # OnOff.OnTime
        (0x0006, 0x4002),  # OnOff.OffWaitTime
        (0x0030, 0x0000),  # GeneralCommissioning.Breadcrumb
    }


# ---------------------------------------------------------------------------
# _props — first-match-wins on a nested object literal (#197 review)
# ---------------------------------------------------------------------------

def test_props_first_match_wins_on_a_nested_default_reference():
    """THE regression this branch's first edit fixed. `_PROP` is a flat
    ``findall`` over the whole object literal, so an attribute whose ``default``
    nests another object literal — the exact shape below, lifted from the real
    model — yields TWO ``name``/``type`` pairs from one ``findall``: the
    attribute's own, then the nested reference's. Last-match-wins gave every
    such attribute the NESTED name, so 0x0015 (``MinHeatSetpointLimit``) and
    0x0003 (``AbsMinHeatSetpointLimit``) both printed as ``AbsMinHeatSetpointLimit``
    — one writable attribute wrongly reported under a different, read-only
    attribute's name. First-match-wins is correct because an element's own
    properties always precede any nested literal."""
    obj = '''{
        name: "MinHeatSetpointLimit", id: 21, type: "temperature",
        access: "RW VM",
        default: { type: "reference", name: "AbsMinHeatSetpointLimit" },
        quality: "N"
    }'''
    props = es._props(obj)
    assert props["name"] == "MinHeatSetpointLimit"
    assert props["type"] == "temperature"


# ---------------------------------------------------------------------------
# is_writable — the R[W] gap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("access, expected", [
    ("RW VM", True),
    ("RW VO", True),
    ("RW VA", True),
    ("R[W] VM", True),
    ("R[W] VA", True),
    ("R V", False),
    ("R [V]", False),
    ("", False),
    (None, False),
])
def test_is_writable(access, expected):
    """``"RW" in access`` alone misses ``R[W]`` — matter.js's conditionally/
    optionally-writable form (``Access.js``'s ``ReadWriteOption``, parsed from
    the ``[W]`` DSL token). That gap dropped 13 spec-writable attributes from
    ``WRITABLE_ATTRIBUTES`` on the pinned model — twelve DoorLock settings
    (including ``AutoRelockTime``, DoorLock's working analogue of the OnOff
    "auto-off after" feature #197 retired) and Thermostat's
    ``MinSetpointDeadBand``. ``"R [V]"`` (a bracketed privilege, not a
    bracketed write flag) must stay non-writable — the bracket alone is not
    the signal, ``[W]`` specifically is."""
    assert es.is_writable(access) is expected


def test_an_r_bracket_w_attribute_reaches_the_writable_set(tmp_path):
    """The same classification `emit_table` applies at its per-attribute step
    (``if is_writable(attr.get("access")): writable...add(attr_id)``), run here
    as `parse_cluster` + `is_writable` directly rather than through
    `emit_table()` itself: `emit_table` also runs `command_parameters`'s
    `KNOWN_COMMAND_FIELD_ATTRIBUTES` floor, which demands the real model's four
    command-parameter pairs be present and would abort on a synthetic
    single-attribute fixture for a reason unrelated to what this test checks."""
    path = _element(tmp_path, '''
export const x = Cluster({ name: "DoorLock", id: 257 },
  Attribute({ name: "AutoRelockTime", id: 0x23, type: "uint32", access: "R[W] VM", conformance: "O" })
)''')
    _, attributes = es.parse_cluster(path)
    writable_ids = {a["id"] for a in attributes if es.is_writable(a.get("access"))}
    assert writable_ids == {0x23}
