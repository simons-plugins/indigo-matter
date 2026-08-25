"""Reciprocal mireds ↔ Kelvin conversion, and nothing else.

A module of its own because these two are the only thing the plugin's two
otherwise-disjoint directions genuinely share. They lived on
``matter_handlers/color_control.py`` — an INBOUND cluster handler — and were
imported from there by four OUTBOUND modules (``export_handlers``,
``export_dialog_mixin``, ``ct_learner``, ``ct_calibration``), which made a
rename or a class-ification of the inbound handler silently break the export
side with nothing in the import graph to suggest it would.

``ct_bounds`` was the obvious alternative home and is deliberately NOT used:
it is Indigo-free, but it also owns the export-side bound PRECEDENCE and the
``ctMinMireds``/``ctLearnedMinMireds`` option vocabulary, none of which an
inbound colour handler has any business importing. Moving the pair there
would have reversed the direction of the violation rather than removing it.

Pure arithmetic, no imports, no Indigo, no matter-server — the reciprocal is
its own inverse, so ``mireds_to_kelvin(kelvin_to_mireds(k))`` round-trips to
within the rounding of one unit in each domain.
"""
from __future__ import annotations

__all__ = ["mireds_to_kelvin", "kelvin_to_mireds"]


def mireds_to_kelvin(mireds: int) -> int:
    """Mireds → Kelvin. Zero maps to zero: a missing reading is not 1,000,000K."""
    return round(1_000_000 / mireds) if mireds else 0


def kelvin_to_mireds(kelvin: float) -> int:
    """Kelvin → mireds. Zero maps to zero, for the same reason."""
    return round(1_000_000 / kelvin) if kelvin else 0
