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
    """The byte-identity guarantee the un-trapdoor rule preserves: with NONE
    of the four stored CT keys present, the wire stays empty exactly as it
    did before issue #293 — this is the one case the presence gate still
    omits."""
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


def test_a_seed_equal_to_generic_still_reaches_the_wire_when_stored():
    """The gate is "a stored key is present", not "the pair differs from
    generic" — a seed that happens to equal 153/500 still reaches the wire,
    because the un-trapdoor rule (issue #293's 2026-08-24 revision) has no
    way to tell "seeded to exactly generic" apart from "re-widened back to
    exactly generic" other than by key presence. See
    ``test_un_trapdoor_a_bound_re_widened_back_to_generic_still_reaches_the_wire``
    for the re-widen case this exists to fix."""
    options = {ct_bounds.OPTION_CT_MIN_MIREDS: 153, ct_bounds.OPTION_CT_MAX_MIREDS: 500}
    assert ct_bounds.wire_options(options) == {"ctMinMireds": 153, "ctMaxMireds": 500}


def test_un_trapdoor_a_bound_re_widened_back_to_generic_still_reaches_the_wire():
    """THE fix pin: a device learned back to the full generic range must not
    fall silent on the wire. Before this change, a learned pair resolving to
    exactly (153, 500) produced empty wire options — indistinguishable from
    a device that never published bounds at all — and the node's own
    never-clobber-on-absence rule left a stale narrow pair live forever,
    with an endpoint recreate as the only escape. Presence of the stored
    learned key, not the resolved value, is what decides now."""
    options = {ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 500}
    assert ct_bounds.wire_options(options) == {"ctMinMireds": 153, "ctMaxMireds": 500}


def test_ct_roles_are_the_two_colour_temperature_roles():
    assert set(ct_bounds.CT_ROLES) == {"colorTemperatureLight", "extendedColorLight"}


# ---------------------------------------------------------------------------
# Provenance (2026-08-25 — the dialog has to say which side was measured)
# ---------------------------------------------------------------------------
def test_sources_follow_the_same_precedence_as_the_values():
    """Two walks of one rule is a drift risk, so this pins them together: the
    source reported for a side must be the source the value came from."""
    options = {ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 400,
               ct_bounds.OPTION_CT_MIN_MIREDS: 200,
               ct_bounds.OPTION_CT_MAX_MIREDS: 450}
    assert ct_bounds.effective_ct_bounds(options) == (200, 400)
    assert ct_bounds.effective_ct_sources(options) == (ct_bounds.SOURCE_SEED,
                                                       ct_bounds.SOURCE_LEARNED)


def test_sources_are_generic_when_nothing_is_stored():
    assert ct_bounds.effective_ct_sources({}) == (ct_bounds.SOURCE_GENERIC,
                                                  ct_bounds.SOURCE_GENERIC)
    assert ct_bounds.effective_ct_sources(None) == (ct_bounds.SOURCE_GENERIC,
                                                    ct_bounds.SOURCE_GENERIC)


def test_a_hand_edited_non_integer_bound_is_not_reported_as_its_source():
    """`is_valid_ct_bound` is what the VALUE walk uses to skip a hand-edited
    ``.indiPref`` entry; the source walk has to skip the same one, or the
    dialog would credit the learner for a value it is not publishing."""
    options = {ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: "400"}
    assert ct_bounds.effective_ct_bounds(options)[1] == ct_bounds.GENERIC_MAX_MIREDS
    assert ct_bounds.effective_ct_sources(options)[1] == ct_bounds.SOURCE_GENERIC
