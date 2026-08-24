"""Effective per-device colour-temperature bounds (issue #293).

The physical warm/cool limits published to the fabric for a
``colorTemperatureLight``/``extendedColorLight`` export come from up to three
sources, in order: the ``export_bridge`` LEARNER's own observed-clamp record
(``ct_learner.py``), the user's SEEDED range from the export dialog
(``export_dialog_mixin.py``), or — when neither exists — §4.2's generic
declared domain. **Declared values are only ever a seed; the learner is the
one that converges on hardware truth** (issue #293's design revision,
2026-08-23: even a device's own reported range is just a claim, and nothing
guarantees a claim matches the silicon — the MiBoxer strip that trigged this
issue claimed 2200K while it physically clamps at 2500K).

This module is the one place that combines the three into the pair that
actually matters, and the one place that decides what of that reaches the
wire (:func:`wire_options`). ``export_store.py`` (validation), ``ct_learner.py``
(the learner) and ``export_bridge.py`` (the wire transform) all read from
here rather than each keeping its own copy of "which key wins" — the
precedence is the one fact issue #293 cannot afford to have said twice.

No Indigo import, matching ``export_store``'s own discipline: everything here
is pure and unit-tests against plain dicts.
"""
from __future__ import annotations

from typing import Optional

#: §4.2's declared domain (``docs/BRIDGE_PROTOCOL.md``) — the floor every
#: effective bound falls back to when neither a seed nor a learned value
#: exists. Identical to ``export_handlers.MIREDS_MIN``/``MIREDS_MAX``
#: (pinned by ``tests/test_ct_bounds.py``) but deliberately NOT imported from
#: there: that module imports ``indigo`` at load time (it calls
#: ``indigo.device.turnOn`` and friends), and this module is used by
#: ``export_store``, which promises to unit-test against a plain dict with no
#: Indigo mock anywhere in sight. Two constants kept in lockstep by a test
#: beat an import that would drag a runtime dependency into a module that
#: has never needed one.
GENERIC_MIN_MIREDS = 153
GENERIC_MAX_MIREDS = 500

#: The two roles a colour-temperature bound means anything for (§4.2). A
#: literal tuple, not an import from ``export_catalog`` — the same "spelled
#: here, not imported" choice ``export_handlers.OPTION_INVERT`` already
#: makes, so that ``export_store`` (no-Indigo-import) and ``ct_learner``
#: (command-worker code) can both use this without acquiring a dependency on
#: the export catalog's own role table.
CT_ROLES = ("colorTemperatureLight", "extendedColorLight")

#: ``ExportEntry.options`` keys (issue #293). ``ctMinMireds``/``ctMaxMireds``
#: are the user's SEED from the export dialog; ``ctLearnedMinMireds``/
#: ``ctLearnedMaxMireds`` are the learner's own observed-clamp record. Owned
#: here — not in ``export_store`` — for the same reason ``export_catalog``
#: owns ``OPTION_STATE_KEY``/``OPTION_STATE_INVERT`` rather than the store:
#: ``export_bridge`` (home of both the wire transform and the learner) needs
#: them too, and must not import ``export_store`` to get them — the store
#: takes an *injected*, duck-typed entry, never a concrete dependency on the
#: module that happens to produce one today.
OPTION_CT_MIN_MIREDS = "ctMinMireds"
OPTION_CT_MAX_MIREDS = "ctMaxMireds"
OPTION_CT_LEARNED_MIN_MIREDS = "ctLearnedMinMireds"
OPTION_CT_LEARNED_MAX_MIREDS = "ctLearnedMaxMireds"

#: Every stored key this module understands — what :func:`wire_options` strips.
_STORED_KEYS = (OPTION_CT_MIN_MIREDS, OPTION_CT_MAX_MIREDS,
                OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_LEARNED_MAX_MIREDS)


def _is_ct_int(value: object) -> bool:
    """A stored bound is a plain ``int`` — a ``bool`` is not one (Python
    quirk: ``isinstance(True, int)`` is ``True``), and neither is anything
    that merely looks numeric. ``export_store._validate_options`` is what
    refuses a bad TYPE at write time; this is the second, defensive read of
    a value store validation has (almost always) already let through — a
    hand-edited ``.indiPref`` is the one path that can still reach this
    without ever passing through the store's own guard.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _side(options: dict, learned_key: str, seed_key: str, generic: int) -> int:
    """One side's effective bound: learned, else seed, else generic."""
    for key in (learned_key, seed_key):
        value = options.get(key)
        if _is_ct_int(value):
            return value
    return generic


def effective_ct_bounds(options: Optional[dict]) -> tuple[int, int]:
    """(min, max) mireds for one export — learned else seed else generic, per side.

    Resolved independently per side on purpose: a device can be seeded on
    both ends and only ever prove a warm-side clamp through the learner, and
    the cool side has to keep the seed (or the generic floor) until its own
    evidence arrives — the two sides are never required to agree on which
    source they came from.

    ``options`` is the export entry's own stored options — never the
    wire-transformed dict :func:`wire_options` produces, which has already
    thrown the two source keys away by the time anything downstream could
    ask this question of it.
    """
    options = options or {}
    return (
        _side(options, OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_MIN_MIREDS, GENERIC_MIN_MIREDS),
        _side(options, OPTION_CT_LEARNED_MAX_MIREDS, OPTION_CT_MAX_MIREDS, GENERIC_MAX_MIREDS),
    )


def wire_options(options: Optional[dict]) -> dict:
    """The §4.1 ``options`` an export actually SENDS, derived from its stored ones.

    Two things happen, and both are load-bearing:

    * the four STORED keys (seed + learned) never leave the plugin — they
      are this plugin's own record of where a bound came from, which the
      node has no use for and no vocabulary to receive; it only ever wants
      the two effective numbers.
    * the effective pair is added back to the wire options ONLY when it
      differs from the generic 153/500 domain. An ordinary export — no seed,
      nothing learned yet — must produce a byte-identical wire frame to
      before this feature existed (the exact round-trip tests pin this): the
      node already assumes 153/500 the moment the keys are absent (§4.2), so
      sending them anyway would be a pure no-op that costs bytes and, worse,
      diffs every existing golden frame for no behavioural gain.
    """
    result = {key: value for key, value in (options or {}).items() if key not in _STORED_KEYS}
    min_mireds, max_mireds = effective_ct_bounds(options)
    if (min_mireds, max_mireds) != (GENERIC_MIN_MIREDS, GENERIC_MAX_MIREDS):
        result[OPTION_CT_MIN_MIREDS] = min_mireds
        result[OPTION_CT_MAX_MIREDS] = max_mireds
    return result
