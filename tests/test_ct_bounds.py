"""Effective colour-temperature bounds (issue #293): precedence and the wire
transform. No Indigo import — ``ct_bounds`` promises pure functions over
plain dicts, same discipline as ``export_store``.
"""
from __future__ import annotations

import importlib

import ct_bounds


def test_generic_bounds_match_the_handler_constants(mock_indigo_base):
    """Duplicated rather than imported (see the module docstring) — pinned here
    so the two literal pairs cannot silently drift apart. ``export_handlers``
    imports ``indigo`` at module load time, so it is only ever imported here
    behind the mock, exactly like every other test that touches it."""
    import export_handlers
    importlib.reload(export_handlers)
    assert ct_bounds.GENERIC_MIN_MIREDS == export_handlers.MIREDS_MIN == 153
    assert ct_bounds.GENERIC_MAX_MIREDS == export_handlers.MIREDS_MAX == 500


# ---------------------------------------------------------------------------
# effective_ct_bounds — precedence
# ---------------------------------------------------------------------------
def test_no_options_falls_back_to_generic():
    assert ct_bounds.effective_ct_bounds({}) == (153, 500)


def test_none_options_falls_back_to_generic():
    assert ct_bounds.effective_ct_bounds(None) == (153, 500)


def test_seed_wins_over_generic():
    options = {ct_bounds.OPTION_CT_MIN_MIREDS: 200, ct_bounds.OPTION_CT_MAX_MIREDS: 400}
    assert ct_bounds.effective_ct_bounds(options) == (200, 400)


def test_learned_wins_over_seed():
    options = {
        ct_bounds.OPTION_CT_MIN_MIREDS: 200, ct_bounds.OPTION_CT_MAX_MIREDS: 400,
        ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 180, ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 420,
    }
    assert ct_bounds.effective_ct_bounds(options) == (180, 420)


def test_each_side_resolves_independently():
    """A learned max beside only a seeded min — the two sides never have to
    agree on which source they came from."""
    options = {ct_bounds.OPTION_CT_MIN_MIREDS: 200, ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 420}
    assert ct_bounds.effective_ct_bounds(options) == (200, 420)


def test_a_lone_learned_side_leaves_the_other_at_generic():
    options = {ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 420}
    assert ct_bounds.effective_ct_bounds(options) == (153, 420)


def test_a_non_int_stored_value_is_treated_as_absent():
    """A hand-edited `.indiPref` is the one write path store validation never
    sees at read time from this function's point of view — a bool or a float
    must not be trusted as a bound."""
    options = {ct_bounds.OPTION_CT_MIN_MIREDS: True, ct_bounds.OPTION_CT_MAX_MIREDS: 420.0}
    assert ct_bounds.effective_ct_bounds(options) == (153, 500)


# ---------------------------------------------------------------------------
# wire_options — the transform `_spec_for` applies
# ---------------------------------------------------------------------------
def test_an_ordinary_export_produces_an_empty_wire_options():
    assert ct_bounds.wire_options({}) == {}
    assert ct_bounds.wire_options(None) == {}


def test_unrelated_options_pass_through_untouched():
    assert ct_bounds.wire_options({"invert": True}) == {"invert": True}


def test_seed_bounds_reach_the_wire():
    options = {ct_bounds.OPTION_CT_MIN_MIREDS: 200, ct_bounds.OPTION_CT_MAX_MIREDS: 400}
    assert ct_bounds.wire_options(options) == {"ctMinMireds": 200, "ctMaxMireds": 400}


def test_learned_keys_never_reach_the_wire_only_the_effective_pair_does():
    options = {ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 200,
              ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 400}
    wire = ct_bounds.wire_options(options)
    assert wire == {"ctMinMireds": 200, "ctMaxMireds": 400}
    assert "ctLearnedMinMireds" not in wire
    assert "ctLearnedMaxMireds" not in wire


def test_seed_keys_are_stripped_when_a_learned_value_supersedes_them():
    options = {
        ct_bounds.OPTION_CT_MIN_MIREDS: 250, ct_bounds.OPTION_CT_MAX_MIREDS: 450,
        ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 400,
    }
    wire = ct_bounds.wire_options(options)
    assert wire == {"ctMinMireds": 250, "ctMaxMireds": 400}


def test_bounds_that_equal_generic_are_omitted_from_the_wire():
    """A seed that happens to equal 153/500 must produce the SAME empty wire
    options as no seed at all — the gate is "differs from generic", not
    "a key exists"."""
    options = {ct_bounds.OPTION_CT_MIN_MIREDS: 153, ct_bounds.OPTION_CT_MAX_MIREDS: 500}
    assert ct_bounds.wire_options(options) == {}


def test_ct_roles_are_the_two_colour_temperature_roles():
    assert set(ct_bounds.CT_ROLES) == {"colorTemperatureLight", "extendedColorLight"}
