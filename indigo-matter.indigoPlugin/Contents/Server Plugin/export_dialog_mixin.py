"""The Matter export allow-list — the "Manage Matter Exports…" dialog
(PRD-indigo-matter-export §5.1 UI-D; roles per BRIDGE_PROTOCOL §4.2). Owns the
picker/status callbacks and the reconcile/save machinery ``startup`` calls into.
See issue #146.
"""
from __future__ import annotations

from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import export_catalog
import export_handlers
from bridge_protocol import next_generation, published_id_for
from export_store import (CT_BOUNDS_ROLES, ExportEntry, MAPPABLE_ROLES, OPTION_CT_LEARNED_MAX_MIREDS,
                          OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_MAX_MIREDS, OPTION_CT_MIN_MIREDS,
                          OPTION_INVERT, OPTION_STATE_INVERT, OPTION_STATE_KEY)
from ct_bounds import (GENERIC_MAX_MIREDS, GENERIC_MIN_MIREDS, SOURCE_GENERIC,
                       SOURCE_LEARNED, SOURCE_SEED, effective_ct_bounds,
                       effective_ct_sources, has_stored_ct_bounds)
from mired_units import kelvin_to_mireds, mireds_to_kelvin
from plugin_constants import (
    EXCLUDED_OPTION_PREFIX, EXPORT_PICKER_LIMIT,
    MENU_MANAGE_EXPORTS, MENU_MIGRATE_EXPORT, MENU_READOPT_EXPORT, MENU_UNPAIR_ECOSYSTEM,
    NO_MATCH_OPTION, NO_SELECTION_ID, NO_SELECTION_LABEL, ROW_ERROR_LABEL, TRUNCATED_OPTION,
    degrades_to_list_error,
)


class ExportDialogMixin:
    """The export allow-list dialog: pickers, add/remove, and startup reconcile.

    Composed into ``Plugin`` alongside the other three mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146. (``self.pluginId`` is
    # supplied by ``indigo.PluginBase`` itself, not by ``Plugin.__init__``, and
    # is read via ``getattr`` below — it needs no annotation here.)
    exports: Any
    export_bridge: Any
    logger: Any

    # ------------------------------------------------------------------
    # Matter export allow-list — the "Manage Matter Exports…" dialog
    # (PRD-indigo-matter-export §5.1 UI-D; roles per BRIDGE_PROTOCOL §4.2)
    # ------------------------------------------------------------------
    def _export_plugin_id(self) -> str:
        """This plugin's id, for the loop guard (XNG3/XAC6).

        Read from the running plugin rather than hardcoded, so the guard can
        never drift from the bundle it is protecting; the catalog constant is
        only the fallback for a plugin object built without one (tests).
        """
        return getattr(self, "pluginId", "") or export_catalog.DEFAULT_PLUGIN_ID

    @staticmethod
    def _truthy(value) -> bool:
        """Indigo checkboxes arrive as bools or as "true"/"false" strings.

        Cross-band helper: also called from ``PairingMenuMixin`` and
        ``ServerMenuMixin`` via MRO — its home here is correct only because
        ``ExportDialogMixin`` is always composed into ``Plugin`` (issue #146).
        """
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)

    @staticmethod
    def _indigo_device(device_id):
        """``indigo.devices[device_id]`` or None — a stale id is never fatal."""
        try:
            return indigo.devices[int(device_id)]
        except Exception:  # pylint: disable=broad-except  # KeyError/ValueError/Indigo's own
            return None

    def _export_selection(self, values_dict) -> tuple[str, int]:
        """Decode the picker value into ``(kind, device_id)``.

        ``kind`` is ``"none"`` (nothing chosen, or one of the informational
        rows — the "select a device" seed, the truncation tail, the no-match
        note), ``"excluded"`` (an ``x-`` row the user may see but not pick), or
        ``"device"``.
        """
        raw = str((values_dict or {}).get("exportDevice", "") or "")
        if not raw or raw == NO_SELECTION_ID or raw in (TRUNCATED_OPTION[0], NO_MATCH_OPTION[0]):
            return ("none", 0)
        excluded = raw.startswith(EXCLUDED_OPTION_PREFIX)
        if excluded:
            raw = raw[len(EXCLUDED_OPTION_PREFIX):]
        try:
            device_id = int(raw)
        except (TypeError, ValueError):
            return ("none", 0)
        return ("excluded" if excluded else "device", device_id)

    def _save_plugin_prefs(self) -> None:
        """Flush pluginPrefs to Indigo's database (the store's commit step)."""
        indigo.server.savePluginPrefs()

    def _reject_unexportable_entry(self, entry) -> str | None:
        """Validator for entries restored from prefs — the loop guard, re-run.

        Load is the one write path the dialog's guards never see: a blob
        restored from a backup, or hand-edited in the ``.indiPref``, can name a
        device this plugin created. Only the loop guard is enforced here.
        Ordinary ineligibility is *reported* by the startup reconcile and left
        alone, because a device can be temporarily odd (a plugin still
        starting) and silently deleting the user's export would be worse than
        an accessory that fails to build.
        """
        dev = self._indigo_device(entry.indigo_device_id)
        if dev is None:
            return None
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if isinstance(verdict, export_catalog.Excluded) \
                and verdict.reason == export_catalog.REASON_LOOP_GUARD:
            return export_catalog.REASON_LOOP_GUARD
        return None

    def _reconcile_exports(self) -> None:
        """Report-only startup sweep of the allow-list (never edits it).

        An export whose device has been deleted, or which no longer classifies
        as exportable, is a real problem the user should hear about at startup
        rather than discovering as a missing accessory. It is NOT auto-removed:
        the allow-list is the user's declaration, and E3 re-classifies at
        endpoint-build time anyway.
        """
        if self.exports is None:
            return
        try:
            plugin_id = self._export_plugin_id()
            for entry in self.exports.all():
                dev = self._indigo_device(entry.indigo_device_id)
                if dev is None:
                    self.logger.warning(
                        "Matter export allow-list: device %s is exported as %s but no longer "
                        "exists in Indigo — it will not be bridged. Remove it in "
                        "'Manage Matter Exports…'.",
                        entry.indigo_device_id, entry.role)
                    continue
                # eligible_target is the same classify-then-check-role dance
                # the migrate/re-adopt pickers need (export_catalog.py); this
                # site's own wording is a log line, not a dialog refusal.
                result = export_catalog.eligible_target(dev, plugin_id, entry.options, entry.role)
                if isinstance(result, export_catalog.TargetRefusal):
                    if result.role_mismatch:
                        self.logger.warning(
                            "Matter export allow-list: %s (id %s) is exported as %s, which this "
                            "device no longer offers (%s). Re-pick its role in "
                            "'Manage Matter Exports…'.",
                            getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                            ", ".join(result.verdict.eligible_roles))
                    else:
                        self.logger.warning(
                            "Matter export allow-list: %s (id %s) is exported as %s but is no longer "
                            "exportable: %s. It will not be bridged.",
                            getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                            getattr(result.verdict, "reason", "unknown"))
        except Exception as exc:  # pylint: disable=broad-except
            # A diagnostic sweep must never be the thing that fails startup.
            self.logger.exception(exc)

    def _export_summary(self) -> str:
        if self.exports is None:
            return "Plugin still starting — reopen this dialog in a moment."
        count = len(self.exports)
        # A load failure has to lead. Reporting "Nothing is exported yet." over
        # a blob we could not read invites the user to rebuild the list from
        # scratch, and the rebuild's first save overwrites the rescue copy.
        error = self.exports.load_error
        if error:
            return error if not count else \
                f"{error} {count} device(s) exported.{self._export_bridge_note()}"
        if not count:
            return "Nothing is exported yet."
        summary = f"{count} device(s) exported."
        # An export whose role this version cannot bridge is silently absent
        # from every ecosystem otherwise — the dialog is the only place the user
        # would ever look for the reason.
        pending = sum(1 for entry in self.exports.all()
                      if not export_handlers.is_bridgeable(entry.role))
        if pending:
            # E4 completed the v1 role table, so this can now only mean an
            # allow-list written by a NEWER plugin than the one running — the
            # export blob lives in plugin prefs and survives a downgrade.
            summary += (f" {pending} of them use a role this version cannot bridge "
                        "and will not appear in any ecosystem — they were most likely "
                        "added by a newer version of this plugin.")
        return summary + self._export_bridge_note()

    def _export_bridge_note(self) -> str:
        """One sentence when the exports exist but are not actually live.

        "3 device(s) exported." is true and useless while the bridge client is
        halted on a version skew: the user is looking at this dialog precisely
        because a light is missing from the Home app, and every state below
        answers that question. Reported as a suffix so a load error — which is
        about rescuing the user's list, and outranks everything — still leads.
        """
        bridge = self.export_bridge
        if bridge is None or not bridge.active:
            # No client is the CORRECT state for an empty allow-list (XG5), and
            # the count above already says the list is not empty — so this is a
            # plugin still starting, which its own log line covers.
            return ""
        client = bridge.client
        if client.halted:
            return (f" Bridge client halted ({client.halted_reason or 'no reason recorded'}) "
                    "— restart the bridge node.")
        if client.recovery:
            return (" The bridge node is waiting for an endpoint-map rebuild — exports are not "
                    "live until it is done. Use 'Rebuild Matter Endpoint Map…' in the plugin "
                    "menu.")
        if not client.attached:
            return " Not connected to the bridge node — exports are not live."
        return self._export_health_note(client.status)

    @staticmethod
    def _export_health_note(status) -> str:
        """The §4.3 facts the dialog is the only place a user would look for.

        `drift` and `warnings` were parsed and then read by nobody: an endpoint
        number that had moved, or a map the node could not write, showed up in
        the log at the moment it happened and nowhere at all afterwards. This
        dialog is where somebody goes when an accessory is behaving oddly.

        Since issues #219/#240 the §4.3 ``warnings`` channel also carries the
        node's one-shot NOTICES (``EndpointMapStore``'s ``#notices``: a
        re-adoptable orphan spotted, a re-adopt landed, an entry this bridge
        version cannot rebuild), so the wording below says "reports" rather
        than naming them all persistence problems — several of them are not
        problems at all.
        """
        if status is None:
            return ""
        if status.warnings:
            return (f" The bridge node reports {len(status.warnings)} thing(s) worth reading: "
                    f"{'; '.join(status.warnings)}")
        if status.drift:
            return (f" WARNING: {len(status.drift)} exported accessory number(s) have DRIFTED — "
                    "they may have swapped identities in paired ecosystems. See the log; this is "
                    "never repaired automatically.")
        if not status.drift_checked:
            return (" Endpoint numbers have not been checked against a saved map yet — that "
                    "happens on the first reconcile.")
        return ""

    def get_menu_action_config_ui_values(self, menu_id):
        """Seed the export and unpair dialogs (menu dialogs never remember values).

        Only those two are seeded — this callback fires for EVERY menu item that
        has a ConfigUI, and returning values for another one would overwrite its
        defaults.
        """
        values = indigo.Dict()
        if menu_id == MENU_UNPAIR_ECOSYSTEM:
            # The picker leads with a no-selection row; seed the field to match
            # it, or Indigo renders the seeded-but-unmatched value as a blank
            # first item and the user is one click from unpairing whatever
            # happens to be second.
            values["fabric"] = NO_SELECTION_ID
            return values
        if menu_id == MENU_READOPT_EXPORT:
            # Same reasoning as the unpair dialog above: both pickers lead
            # with a no-selection row, so both fields are seeded to match it.
            values["readoptOrphan"] = NO_SELECTION_ID
            values["readoptDevice"] = NO_SELECTION_ID
            values["readoptConfirm"] = False
            return values
        if menu_id == MENU_MIGRATE_EXPORT:
            # Issue #246 — same reasoning again: both pickers lead with a
            # no-selection row.
            values["migrateSource"] = NO_SELECTION_ID
            values["migrateDevice"] = NO_SELECTION_ID
            values["migrateConfirm"] = False
            return values
        if menu_id != MENU_MANAGE_EXPORTS:
            return values
        values["exportFilter"] = ""
        values["exportDevice"] = NO_SELECTION_ID
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportCtWarmestK"] = ""
        values["exportCtCoolestK"] = ""
        values["exportCtWarmestKShown"] = ""
        values["exportCtCoolestKShown"] = ""
        values["exportCtBoundsSource"] = ""
        values["exportStatus"] = self._export_summary()
        return values

    def _log_row_failure(self, exc, first: bool) -> None:
        """Log one unreadable picker row — stack for the first, one line after.

        A database with fifty broken proxies must not write fifty tracebacks
        into the event log, but the first one has to carry enough to debug.
        """
        if first:
            self.logger.exception(exc)
        else:
            self.logger.error("Matter bridge: another device could not be read — %s", exc)

    # No longer a staticmethod: an exported device's row depends on its saved
    # mapping (ADR-0012), which only the store can answer for.
    def _candidate_row(self, dev, name: str, plugin_id: str,
                       exported) -> Optional[tuple[str, str]]:
        """One picker row for ``dev``, or None to omit it. May raise — the caller contains it.

        Loop-guard devices (created by this plugin) return None: XAC6 requires
        them ABSENT from the picker, not merely unpickable — every one of them
        shadows a device the user already sees, so listing them as excluded
        would only add noise. Every OTHER exclusion is listed with its reason
        (XAC9); hiding those would leave a user hunting for a device that never
        appears.
        """
        device_id = dev.id
        # An excluded device that IS exported keeps its marker: the pair
        # "excluded" + "exported" is exactly the state the user has to know
        # about, and hiding half of it reads as a picker bug rather than the
        # stale export it actually is.
        mark = "● " if device_id in exported else ""
        # An exported device is classified WITH its saved mapping (ADR-0012),
        # or a device already being bridged would be listed as one still
        # waiting to be set up.
        verdict = export_catalog.classify(dev, plugin_id, self._saved_options(device_id))
        if isinstance(verdict, export_catalog.Excluded):
            # export_catalog.excluded_row is the same loop-guard-omit (XAC6) /
            # not-exportable-reason row ServerMenuMixin._readopt_device_row
            # builds — shared because the two pickers word this one outcome
            # identically.
            return export_catalog.excluded_row(verdict, device_id, name, mark, EXCLUDED_OPTION_PREFIX)
        if isinstance(verdict, export_catalog.MappableDevice):
            # Selectable, unlike an exclusion: the whole point is that the user
            # CAN export it, once they say which state is the reading. Marked
            # so the row is honest about the extra step rather than looking
            # identical to a device that needs nothing.
            return (str(device_id), f"{mark}{name} — {verdict.reason}")
        return (str(device_id), f"{mark}{name}")

    @degrades_to_list_error
    def getExportCandidates(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument, too-many-locals
        """Picker rows: every Indigo device, exportable or not (XAC9).

        Excluded devices are listed **with the reason in the label** and an
        ``x-``-prefixed id so the callbacks can reject the pick cleanly —
        hiding them would leave a user hunting for a device that will never
        appear. ``filter`` here is the XML's static filter attribute, NOT the
        user's text: textfields have no callbacks, so the typed filter arrives
        in ``valuesDict`` and the Apply-filter button drives the reload.

        One device that cannot be read costs one row, not the whole list: the
        try/except is INSIDE the loop, because the alternative is a dialog that
        renders empty the moment any device in the database misbehaves. That
        promise holds below :data:`EXPORT_PICKER_LIMIT`; past it, unreadable
        devices are counted in the truncation tail like any other row, and the
        log still carries every one.

        Ordering: the seeded ``(select a device)`` row is always first, then
        every already-exported device (database order), then everything else
        (also database order) — never alphabetised. This dialog is the only
        place a user can remove an export, so an exported device buried past
        :data:`EXPORT_PICKER_LIMIT` would be effectively stuck there; exported
        rows are therefore classified and kept unconditionally, and the cap is
        applied only to the rest, at the end, once the exported count is known.
        """
        text = str((valuesDict or {}).get("exportFilter", "") or "").strip().lower()
        exported = self.exports.ids() if self.exports is not None else frozenset()
        plugin_id = self._export_plugin_id()
        # Always a real row for the seeded value, and always first.
        options: list[tuple[str, str]] = [(NO_SELECTION_ID, NO_SELECTION_LABEL)]
        # One pass, two row lists: exported rows are never truncated (see
        # docstring), so the cap is applied only to `other_rows`, after the
        # loop, once `len(exported_rows)` is known.
        exported_rows: list[tuple[str, str]] = []
        other_rows: list[tuple[str, str]] = []
        truncated = 0
        failures = 0
        for dev in indigo.devices:
            try:
                name = str(getattr(dev, "name", "") or "")
                if text and text not in name.lower():
                    continue
                is_exported = dev.id in exported
                if not is_exported and len(other_rows) >= EXPORT_PICKER_LIMIT:
                    # Upper bound on what the cap below could ever keep —
                    # exported rows only shrink that allowance, never
                    # raise it — so it's safe to stop building rows here.
                    # Still counted, so the tail stays honest.
                    truncated += 1
                    continue
                row = self._candidate_row(dev, name, plugin_id, exported)
                if row is None:               # loop guard: absent, not excluded (XAC6)
                    continue
                (exported_rows if is_exported else other_rows).append(row)
            except Exception as exc:  # pylint: disable=broad-except
                self._log_row_failure(exc, first=not failures)
                failures += 1
                # `dev.id` may be exactly what failed to read, so an
                # unreadable device can't be safely tested for membership
                # in `exported` — it always lands with the others.
                other_rows.append((f"{EXCLUDED_OPTION_PREFIX}err{failures}",
                                   f"— {ROW_ERROR_LABEL}"))
        allowance = max(EXPORT_PICKER_LIMIT - len(exported_rows), 0)
        truncated += max(len(other_rows) - allowance, 0)
        options.extend(exported_rows)
        options.extend(other_rows[:allowance])
        if truncated:
            options.append(TRUNCATED_OPTION)
        if len(options) == 1:
            options.append(NO_MATCH_OPTION)
        return options

    @degrades_to_list_error
    def getExportRoles(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Roles the picked device may legitimately be exported as (§5.2).

        Empty for no selection or an excluded pick — an empty role menu is the
        honest rendering of "there is nothing you may choose here". An outright
        failure is NOT empty: it says so, so the user does not read a broken
        callback as "this device offers no roles".
        """
        kind, device_id = self._export_selection(valuesDict)
        if kind != "device":
            return []
        dev = self._indigo_device(device_id)
        if dev is None:
            return []
        # The mapping being composed in the dialog is part of the
        # question (ADR-0012): a custom device offers no roles until its
        # state is chosen, and offers all the binary ones the moment it is.
        verdict = export_catalog.classify(dev, self._export_plugin_id(),
                                          self._mapping_from_values(valuesDict or {}))
        if not isinstance(verdict, export_catalog.EligibleDevice):
            return []
        options = []
        for role in verdict.eligible_roles:
            try:
                options.append((role, export_catalog.role_label(role)))
            except Exception as exc:  # pylint: disable=broad-except
                self._log_row_failure(exc, first=not options)
        return options

    @degrades_to_list_error
    def getCurrentExports(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Read-only summary of the allow-list (one row per export)."""
        if self.exports is None:
            return [(NO_SELECTION_ID, "(plugin still starting)")]
        options = []
        failures = 0
        for entry in self.exports.all():
            try:
                dev = self._indigo_device(entry.indigo_device_id)
                name = str(getattr(dev, "name", "") or "") if dev is not None else ""
                if not name:
                    name = f"(deleted device {entry.indigo_device_id})"
                label = f"{name} → {export_catalog.role_label(entry.role)}"
                if entry.name_override:
                    label += f' · shown as "{entry.name_override}"'
                if entry.options.get(OPTION_INVERT):
                    label += " · inverted"
                label += self._ct_bounds_row_note(entry)
                options.append((str(entry.indigo_device_id), label))
            except Exception as exc:  # pylint: disable=broad-except
                self._log_row_failure(exc, first=not failures)
                failures += 1
                options.append((f"{EXCLUDED_OPTION_PREFIX}err{len(options)}",
                                f"— {ROW_ERROR_LABEL}"))
        return options or [(NO_SELECTION_ID, "(nothing exported yet)")]

    @classmethod
    def _ct_bounds_row_note(cls, entry) -> str:
        """One export row's colour-temperature range, or ``""``.

        Shown in the list so the answer to "did the calibration take?" does
        not require opening each device in turn — the question that surfaced
        this (2026-08-25: six devices on the live server had learned bounds
        that nothing in the UI displayed).

        Silent only when NOTHING is stored — :func:`ct_bounds.
        has_stored_ct_bounds`, the same "is any bound recorded here" test
        :func:`ct_bounds.wire_options` uses to decide what reaches the wire,
        deliberately not "does the pair differ from generic". A lamp the
        calibration sweep measured as reaching the full 153-500 stores
        exactly the generic pair, and that row has something to say: the range
        was measured. An un-swept lamp stores nothing and stays silent, so the
        rows still differ where it matters.

        Deliberately NOT ``wire_options(entry.options)`` truthiness (issue
        #293 review): that dict carries every non-CT option too, so it is
        also truthy for an entry with an unrelated option and no CT bound at
        all, which made that row announce the generic 2000-6535K as if it
        had been measured. ``has_stored_ct_bounds`` is exported from
        ``ct_bounds`` specifically so this check and :func:`wire_options`'s
        own gate share the one test rather than risking the two drifting
        apart again.
        """
        if entry.role not in CT_BOUNDS_ROLES or not has_stored_ct_bounds(entry.options):
            return ""
        eff_min, eff_max = effective_ct_bounds(entry.options)
        # Through the same clamp the dialog fields use, so one lamp never
        # reads 6536K in the list and 6535K in the field above it.
        note = f" · {cls._kelvin_shown(eff_max)}-{cls._kelvin_shown(eff_min)}K"
        if SOURCE_LEARNED in effective_ct_sources(entry.options):
            note += " measured"
        return note

    # ----------------------------------------------------------------
    # Custom-state mapping (ADR-0012, issue #252)
    # ----------------------------------------------------------------
    def _saved_options(self, device_id: int) -> dict:
        """The saved options for ``device_id``, or ``{}``.

        Every classify call about an EXPORTED device has to pass these, or a
        mapped export classifies as merely mappable and the dialog reports the
        device it is currently bridging as not yet set up.
        """
        entry = self.exports.get(device_id) if self.exports is not None else None
        return dict(entry.options) if entry is not None else {}

    def _mapping_from_values(self, values: dict) -> dict:
        """The mapping the dialog currently has on screen, as an options dict.

        Empty when no state is chosen, which is what makes an unmapped device
        classify as :class:`~export_catalog.MappableDevice` and keeps the role
        menu empty until the user has answered the question that unlocks it.
        """
        key = str(values.get("exportStateKey", "") or "").strip()
        if not key:
            return {}
        options = {OPTION_STATE_KEY: key}
        if self._truthy(values.get("exportStateInvert")):
            options[OPTION_STATE_INVERT] = True
        return options

    #: The Kelvin range the two colour-temperature seed fields accept.
    #: 2000K/6535K rather than a rounder 2000/6500: `kelvin_to_mireds(6535)`
    #: is 153 (the fabric's exact floor) after rounding, while 6500 rounds to
    #: 154 — a user typing the fabric's own advertised "6500K" would then be
    #: refused by the post-conversion domain check below for reasons that
    #: read as a plugin bug. 6535 is the largest Kelvin value that still
    #: converts to a legal mired, so the field's own bound and the domain
    #: check downstream can never disagree.
    _CT_SEED_KELVIN_MIN = 2000
    _CT_SEED_KELVIN_MAX = 6535

    def _ct_seed_from_values(self, values: dict,
                             previous_options: Optional[dict] = None) -> tuple[dict, Optional[str]]:
        """The ``ctMinMireds``/``ctMaxMireds`` seed pair the dialog fields describe.

        Returns ``(options, None)`` on success — ``options`` is ``{}`` when
        both Kelvin fields are empty (no seed; also how an existing seed is
        DELETED, since :meth:`exportDeviceChanged` pre-fills these fields
        from any saved one) or the two-key pair when both are filled — or
        ``({}, reason)`` when what is on screen cannot become a seed at all.

        **A value the user did not touch is not a declaration.** The fields
        are pre-filled with the EFFECTIVE bounds
        (:meth:`_apply_ct_bounds_fields`), which for a calibrated device are
        the LEARNER's, so an unedited save would otherwise copy measured
        evidence into ``ctMinMireds``/``ctMaxMireds`` and leave it there after
        the learner re-widened or forgot the bound it came from. When both
        fields still hold exactly what was put on screen, this therefore
        carries ``previous_options``' own seed keys forward untouched — no
        seed where there was none, the same seed where there was one — and
        only a genuinely EDITED pair is read as the user declaring a range.

        **Reciprocal by design.** The WARMEST Kelvin the user can name is the
        HIGHEST mireds value (§4.2's ``colorTempMireds`` runs cool-to-warm as
        low-to-high mireds), so it becomes ``ctMaxMireds``; the coolest
        becomes ``ctMinMireds``. Get the two fields backwards and the export
        would silently declare an inverted range that ``export_store``
        would then refuse to load back — this function is the one place
        that mapping happens, so it cannot be gotten backwards in two places
        that disagree.
        """
        warm_raw = str(values.get("exportCtWarmestK", "") or "").strip()
        cool_raw = str(values.get("exportCtCoolestK", "") or "").strip()
        shown_warm = str(values.get("exportCtWarmestKShown", "") or "").strip()
        shown_cool = str(values.get("exportCtCoolestKShown", "") or "").strip()
        # Absent shadow keys (issue #293 review) mean this call has nothing
        # to compare the fields against — fail SAFE by treating the save as
        # UNEDITED rather than inferring an edit from a comparison it cannot
        # make. Both directions of the bug this avoids are real: prefilled
        # fields + absent shadows used to compare unequal, silently writing
        # the learner's own DISPLAYED value back as a user seed — precisely
        # what this whole shadow mechanism exists to prevent; and cleared
        # fields + absent shadows made ("", "") == ("", "") true by
        # accident, turning the documented "clear both deletes the seed"
        # gesture into a silent no-op.
        #
        # A shadow key PRESENT but EMPTY is a different, meaningful state —
        # both boxes were genuinely blank the last time the dialog showed
        # them — and keeps comparing exactly as before; only the TOTAL
        # absence of both keys is ambiguous enough to fail safe on.
        shadows_absent = ("exportCtWarmestKShown" not in values
                          and "exportCtCoolestKShown" not in values)
        if shadows_absent or (warm_raw, cool_raw) == (shown_warm, shown_cool):
            stored = previous_options or {}
            return ({key: stored[key]
                     for key in (OPTION_CT_MIN_MIREDS, OPTION_CT_MAX_MIREDS)
                     if key in stored}, None)
        if not warm_raw and not cool_raw:
            return {}, None
        if not warm_raw or not cool_raw:
            return {}, ("Set both the warmest and coolest colour temperature, or leave both "
                        "empty to use the generic range.")
        try:
            warm_k, cool_k = int(warm_raw), int(cool_raw)
        except ValueError:
            return {}, "Warmest/coolest colour temperature must be whole numbers of Kelvin."
        if not (self._CT_SEED_KELVIN_MIN <= warm_k <= self._CT_SEED_KELVIN_MAX
                and self._CT_SEED_KELVIN_MIN <= cool_k <= self._CT_SEED_KELVIN_MAX):
            return {}, (f"Colour temperature must be between {self._CT_SEED_KELVIN_MIN}K and "
                        f"{self._CT_SEED_KELVIN_MAX}K.")
        if not warm_k < cool_k:
            return {}, "The warmest colour temperature must be lower, in Kelvin, than the coolest."
        ct_max = max(GENERIC_MIN_MIREDS, min(GENERIC_MAX_MIREDS, kelvin_to_mireds(warm_k)))
        ct_min = max(GENERIC_MIN_MIREDS, min(GENERIC_MAX_MIREDS, kelvin_to_mireds(cool_k)))
        if not (GENERIC_MIN_MIREDS <= ct_min < ct_max <= GENERIC_MAX_MIREDS):
            # Two distinct Kelvin values close to the fabric's own cool edge
            # can round to the SAME mired value (e.g. 6534K and 6535K both
            # round to 153) — a real but rare case the Kelvin-range check
            # above cannot catch on its own, because it is a property of the
            # rounding, not of either value individually.
            return {}, ("That Kelvin range is too narrow to express as a valid colour-"
                        "temperature bound — widen the gap between the two values.")
        return {OPTION_CT_MIN_MIREDS: ct_min, OPTION_CT_MAX_MIREDS: ct_max}, None

    def _fleet_state_key(self, dev) -> Optional[str]:
        """A state key already mapped for another device of the same TYPE.

        The measured motivation (issue #252): a Texecom panel produces 24
        identical `alarmZone` devices, and having answered "the boolean is
        `status`" once, a user should not be asked to hunt for it another 23
        times — only for the role, which genuinely differs per zone.

        Derived from the allow-list rather than stored anywhere. A recipe kept
        separately would be one more thing to persist, migrate and invalidate;
        read this way it cannot go stale, because it IS the set of choices the
        user has actually made and kept.
        """
        try:
            plugin_id = getattr(dev, "pluginId", None)
            type_id = getattr(dev, "deviceTypeId", None)
            if not plugin_id or not type_id or self.exports is None:
                return None
            for entry in self.exports.all():
                if entry.indigo_device_id == dev.id:
                    continue
                key = entry.options.get(OPTION_STATE_KEY)
                if not key:
                    continue
                other = self._indigo_device(entry.indigo_device_id)
                if other is None:
                    continue
                if getattr(other, "pluginId", None) == plugin_id \
                        and getattr(other, "deviceTypeId", None) == type_id:
                    return key
        except Exception as exc:  # pylint: disable=broad-except
            # A convenience, never a blocker — the user can still pick by hand.
            self.logger.debug("Matter export: could not derive a fleet state key — %s", exc)
        return None

    @degrades_to_list_error
    def getExportStateKeys(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """The boolean states the selected device offers as a mapping.

        Empty for any device that does not need a mapping, which is also what
        keeps the field hidden — the XML binds its visibility to
        ``exportNeedsMapping``, set by :meth:`exportDeviceChanged`.
        """
        kind, device_id = self._export_selection(valuesDict or {})
        if kind != "device":
            return []
        dev = self._indigo_device(device_id)
        if dev is None:
            return []
        return [(key, key) for key in export_catalog.binary_state_keys(dev)]

    def exportRoleChanged(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Role selection changed: re-derive the polarity default.

        Changing the role changes what the device's boolean MEANS — the same
        `onState` is "motion detected" under occupancy and "door open" under
        contact, and only the second disagrees with Matter's own reading. So
        the tick-box follows the role rather than persisting a value chosen for
        a different one. A user who wants the other polarity unticks it after,
        which is the last word; nothing re-derives it again unless the role
        moves again.

        Deliberately does NOT touch an export being edited: an existing entry's
        polarity is a decision already made and possibly hand-corrected, and
        silently re-deriving it on the way past would undo that.
        """
        values = valuesDict
        try:
            kind, device_id = self._export_selection(values)
            if kind != "device" or (self.exports is not None and device_id in self.exports):
                return values
            role = str(values.get("exportRole", "") or "")
            if role:
                values["exportStateInvert"] = export_catalog.default_invert_for(role)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
        return values

    def exportStateKeyChanged(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """State-key selection changed: the role menu can now be populated.

        Choosing a state is what turns a :class:`~export_catalog.MappableDevice`
        into an eligible one, so the roles that were unavailable a moment ago
        are available now — and the returned dict is what makes Indigo reload
        the menu that lists them. Seeds the default role too, so the common
        path is "pick the state, press Add".
        """
        values = valuesDict
        try:
            kind, device_id = self._export_selection(values)
            if kind != "device":
                return values
            dev = self._indigo_device(device_id)
            if dev is None:
                return values
            verdict = export_catalog.classify(dev, self._export_plugin_id(),
                                              self._mapping_from_values(values))
            if not isinstance(verdict, export_catalog.EligibleDevice):
                values["exportRole"] = ""
                return values
            if str(values.get("exportRole", "") or "") not in verdict.eligible_roles:
                values["exportRole"] = verdict.default_role
                values["exportStateInvert"] = export_catalog.default_invert_for(verdict.default_role)
            values["exportStatus"] = (
                f"{dev.name} will be read from its \"{values.get('exportStateKey')}\" state. "
                f"Now choose how it should appear.")
        except Exception as exc:  # pylint: disable=broad-except
            # A menu callback that raises leaves the dialog wedged; the user
            # can still press Add / update, which validates everything again.
            self.logger.exception(exc)
        return values

    def _exported_warning(self, device_id: int) -> str:
        """Suffix warning shown when an EXCLUDED device is nonetheless exported.

        This is the incoherent state worth naming out loud: the allow-list says
        export it, the catalog says it cannot be. Left alone it becomes an
        accessory that never appears, with no visible cause.
        """
        if self.exports is not None and device_id in self.exports:
            return " — but this device IS currently exported — remove it or it will fail to bridge"
        return ""

    def exportReloadPicker(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Apply-filter button: the return trip is what reloads the lists."""
        values = valuesDict
        text = str(values.get("exportFilter", "") or "").strip()
        values["exportStatus"] = (f'Filtered on "{text}". {self._export_summary()}' if text
                                  else self._export_summary())
        return values

    def exportDeviceChanged(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Picker selection changed: load that device's saved export, or defaults.

        Menu callbacks return a valuesDict, not an error dict (the SDK's menu
        contract), so an excluded pick is reported in the read-only status
        field here — and refused again by the Add/update button below. Both
        paths are covered by tests.
        """
        values = valuesDict
        kind, device_id = self._export_selection(values)
        if kind == "none":
            self._clear_export_detail(values)
            values["exportStatus"] = self._export_summary()
            return values
        dev = self._indigo_device(device_id)
        if kind == "excluded" or dev is None:
            reason = "that device no longer exists"
            if dev is not None:
                verdict = export_catalog.classify(dev, self._export_plugin_id(),
                                                  self._saved_options(device_id))
                reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                    else "that device is not exportable"
            self._clear_export_detail(values)
            values["exportStatus"] = (f"Not exportable: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        entry = self.exports.get(device_id) if self.exports is not None else None
        saved = dict(entry.options) if entry is not None else {}
        verdict = export_catalog.classify(dev, self._export_plugin_id(), saved)
        if isinstance(verdict, export_catalog.Excluded):
            self._clear_export_detail(values)
            values["exportStatus"] = (f"Not exportable: {verdict.reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        if isinstance(verdict, export_catalog.MappableDevice):
            # The device can be exported, but only once the user says which of
            # its states is the reading (ADR-0012). Everything below assumes a
            # role, and there is no role to offer yet, so this branch seeds the
            # mapping fields and stops. The suggestion order is deliberate:
            # a key already chosen for a SIBLING device of the same type beats
            # the catalog's own guess, because the fleet answer is the one the
            # user has already thought about.
            self._clear_export_detail(values)
            values["exportNeedsMapping"] = "true"
            state_key = self._fleet_state_key(dev) or verdict.suggested_state_key
            values["exportStateKey"] = state_key
            values["exportStateInvert"] = False
            # Seed the role too. `exportStateKeyChanged` does this — but only
            # when the user CHANGES the state menu, and the line above has
            # already chosen for them, so on the common path that callback
            # never fires and the role stayed blank. Every zone of a 24-zone
            # panel then had to have its role set by hand, which is exactly the
            # per-device work the name heuristic exists to remove.
            mapped = export_catalog.classify(dev, self._export_plugin_id(),
                                             {OPTION_STATE_KEY: state_key})
            suggested = getattr(mapped, "default_role", "")
            values["exportRole"] = suggested
            values["exportStateInvert"] = export_catalog.default_invert_for(suggested)
            values["exportStatus"] = (
                f'{dev.name} will be read from its "{state_key}" state'
                + (f", and looks like a {export_catalog.role_label(suggested).lower()} — "
                   "change that below if it is not." if suggested else
                   ". Now choose how it should appear.")
                + self._exported_warning(device_id))
            return values
        values["exportNeedsMapping"] = "true" if saved.get(OPTION_STATE_KEY) else "false"
        values["exportStateKey"] = str(saved.get(OPTION_STATE_KEY, "") or "")
        values["exportStateInvert"] = bool(saved.get(OPTION_STATE_INVERT, False))
        if entry is not None:
            values["exportRole"] = entry.role
            values["exportName"] = entry.name_override or ""
            values["exportInvert"] = bool(entry.options.get(OPTION_INVERT, False))
            self._apply_ct_bounds_fields(values, entry.options)
            values["exportStatus"] = f"{dev.name} is exported as {export_catalog.role_label(entry.role)}."
        else:
            values["exportRole"] = verdict.default_role
            values["exportStateInvert"] = export_catalog.default_invert_for(verdict.default_role)
            values["exportName"] = ""
            values["exportInvert"] = False
            self._apply_ct_bounds_fields(values, None)
            values["exportStatus"] = f"{dev.name} is not exported yet."
        return values

    #: One phrase per :func:`ct_bounds.effective_ct_sources` verdict, for the
    #: read-only line under the two Kelvin fields. A learned bound and a typed
    #: one are the same number on screen; only this says which is which.
    _CT_SOURCE_PHRASE = {
        SOURCE_LEARNED: "measured from the device",
        SOURCE_SEED: "as you set it",
        SOURCE_GENERIC: "generic limit",
    }

    def _apply_ct_bounds_fields(self, values: dict, options: Optional[dict]) -> None:
        """Fill the two Kelvin fields, their shadow copies, and the source line.

        The fields show the EFFECTIVE bounds (``ct_bounds.effective_ct_bounds``
        — learned, else seed, else generic per side), not just the user's seed:
        issue #293 shipped the learner writing its own keys, and a calibrated
        lamp with no seed therefore showed two empty boxes, which reads as "the
        calibration did nothing" when six devices on the live server had in
        fact learned their range.

        The shadow fields (``…KShown``) are what makes that safe. They record
        what was PUT on screen, so :meth:`_ct_seed_from_values` can tell a
        value the user typed from one the plugin displayed, and decline to
        re-save a measured bound as a declared seed. Without them, opening a
        calibrated device and pressing "Add / update export" — for any reason,
        such as renaming it — would silently convert the learner's evidence
        into a seed that outlives the evidence.

        Both boxes stay EMPTY when neither side has a bound, because empty is
        also how the dialog DELETES a seed (:meth:`_ct_seed_from_values`), and
        pre-filling the generic limits would make "no range set" indis-
        tinguishable from "the generic range, declared".
        """
        min_source, max_source = effective_ct_sources(options)
        if options is None or (min_source == SOURCE_GENERIC and max_source == SOURCE_GENERIC):
            values["exportCtWarmestK"] = ""
            values["exportCtCoolestK"] = ""
            values["exportCtWarmestKShown"] = ""
            values["exportCtCoolestKShown"] = ""
            values["exportCtBoundsSource"] = "" if options is None else (
                "No range set — the generic 2000-6535K limits are published. The plugin fills "
                "this in on its own once it sees what the device can actually reach.")
            return
        eff_min, eff_max = effective_ct_bounds(options)
        warm, cool = self._kelvin_shown(eff_max), self._kelvin_shown(eff_min)
        values["exportCtWarmestK"] = warm
        values["exportCtCoolestK"] = cool
        values["exportCtWarmestKShown"] = warm
        values["exportCtCoolestKShown"] = cool
        if SOURCE_LEARNED in (min_source, max_source):
            # Neither half of the seed/generic-only trailer holds once a
            # side is measured (issue #293 review): editing that side writes
            # a seed, which LOSES to the learned bound (`ct_bounds._side`),
            # so the field would silently revert on reopen; and clearing
            # both deletes only the seed key, so a learned side stays
            # published at its measured value, not the generic range.
            trailer = ("A measured value is what the plugin publishes; typing over it only "
                      "takes effect on a side that has not been measured yet.")
        else:
            trailer = "Change a value to override it; clear both for the generic range."
        values["exportCtBoundsSource"] = (
            f"Warmest {warm}K ({self._CT_SOURCE_PHRASE[max_source]}), "
            f"coolest {cool}K ({self._CT_SOURCE_PHRASE[min_source]}). {trailer}")

    @classmethod
    def _kelvin_shown(cls, mireds: int) -> str:
        """One effective mireds bound as Kelvin the two fields will ACCEPT back.

        Clamped to the fields' own range, because the mired domain is very
        slightly wider at the cool end: 153 mireds — the fabric's floor, and
        what an un-seeded cool side resolves to — is 6536K, one Kelvin above
        the 6535K ceiling :meth:`_ct_seed_from_values` enforces. Displayed
        raw, editing the OTHER field would then bounce the save with "must be
        between 2000K and 6535K" about a number the plugin itself put there.
        6535K converts straight back to 153 mireds, so the clamp costs
        nothing.
        """
        return str(min(max(mireds_to_kelvin(mireds), cls._CT_SEED_KELVIN_MIN),
                       cls._CT_SEED_KELVIN_MAX))

    @staticmethod
    def _clear_export_detail(values: dict) -> None:
        """Blank every detail field, including the mapping pair.

        Extracted when the mapping fields arrived (issue #252): the same four
        lines were already repeated three times, and adding two more to each
        copy is how one of them ends up forgotten — leaving a stale state key
        on screen under a different device.
        """
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportNeedsMapping"] = "false"
        values["exportStateKey"] = ""
        values["exportStateInvert"] = False
        values["exportCtWarmestK"] = ""
        values["exportCtCoolestK"] = ""
        values["exportCtWarmestKShown"] = ""
        values["exportCtCoolestKShown"] = ""
        values["exportCtBoundsSource"] = ""

    def _first_unclaimed_identity(self, identity: str, device_id: int) -> str:
        """Walk :func:`next_generation` from ``identity`` until no OTHER
        ``ExportStore`` entry effectively claims it (issue #246 review finding 1).

        "Migrate an exported accessory…" (``server_menu_mixin.ServerMenuMixin.
        menuMigrateExport``) moves a device's accessory identity onto ANOTHER
        device's entry. Re-exporting the original device afterwards through
        this ordinary dialog must not try to publish that same identity again
        — the node refuses a duplicate ``publishedAs`` for the WHOLE attach
        (``parseEndpointSpecs``, ``malformed_args``), which takes every export
        offline, not just the new one. The same collision can recur one
        generation later (a role change bumping past a generation someone else
        has since taken), so this keeps walking rather than trying
        :func:`next_generation` exactly once.

        Reuses :meth:`server_menu_mixin.ServerMenuMixin._readopt_identity_claimant`
        for the EFFECTIVE-identity comparison it already makes for the same
        reason (``published_as`` where an entry has one, otherwise the default
        derivation it publishes under) — ``device_id``'s OWN entry never counts
        as a claimant, so an unchanged existing entry stays free to keep
        publishing the identity it already holds.
        """
        while self._readopt_identity_claimant(identity, device_id) is not None:  # pylint: disable=no-member
            identity = next_generation(identity)
        return identity

    def exportAddOrUpdate(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Add or update one export. Validates the role against the catalog.

        A role the catalog does not offer for this device is refused here
        rather than by the bridge node, which would only reject it with
        ``unknown_role``/``role_change`` long after the user could connect the
        failure to what they did (BRIDGE_PROTOCOL §1.1).

        Returns **the values dict only**. A ``(valuesDict, errorsDict)`` tuple
        is the documented contract for *validation* methods, not for button
        ``CallbackMethod``s — the SDK's button reference says a button callback
        returns a dictionary of field changes, and the field carrying a button's
        outcome is read-only, so it cannot hold an error message anyway. Every
        refusal therefore lands in ``exportStatus``, which is what the dialog
        actually shows.
        """
        values = valuesDict
        if self.exports is None:
            values["exportStatus"] = "Plugin still starting — try again in a moment."
            return values
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportStatus"] = "Select a device to export."
            return values
        dev = self._indigo_device(device_id)
        if dev is None:
            values["exportStatus"] = "That device no longer exists — refresh the list."
            return values
        # The mapping on screen is part of the question (ADR-0012), so it is
        # composed BEFORE classifying: a custom device is eligible precisely
        # because the user has now answered which state is its reading.
        mapping = self._mapping_from_values(values)
        verdict = export_catalog.classify(dev, self._export_plugin_id(), mapping)
        if kind == "excluded" or isinstance(verdict, export_catalog.Excluded):
            reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                else "not exportable"
            values["exportStatus"] = (f"{dev.name} cannot be exported: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        if isinstance(verdict, export_catalog.MappableDevice):
            # Refused here rather than saved with an empty mapping: an entry
            # with no state key would fall back to reading `onState`, which
            # this device does not have, and publish nothing forever.
            values["exportStatus"] = (
                f"{dev.name} needs a state before it can be exported — pick which of its "
                f"states is the reading.")
            return values
        role = str(values.get("exportRole", "") or "")
        if role not in verdict.eligible_roles:
            values["exportStatus"] = ("Choose how this device should appear "
                                      f"({', '.join(verdict.eligible_roles)}).")
            return values
        name_override = str(values.get("exportName", "") or "").strip() or None
        # Only roles that read a boolean may carry the mapping. Saving it on,
        # say, a numeric sensor role would write an entry `ExportEntry`'s own
        # validation refuses — so the export would survive until the next
        # load and then be dropped, which is the worst possible time to find
        # out. Devices that need a mapping cannot reach a non-mappable role
        # anyway (their eligible set IS the binary family); this covers the
        # STANDARD sensor whose dialog still carries a key from a previous
        # selection.
        options = dict(mapping) if role in MAPPABLE_ROLES else {}
        if role == export_catalog.ROLE_WINDOW_COVERING and self._truthy(values.get("exportInvert")):
            options[OPTION_INVERT] = True
        previous = self.exports.get(device_id)
        if role in CT_BOUNDS_ROLES:
            ct_seed, ct_error = self._ct_seed_from_values(
                values, previous.options if previous is not None else None)
            if ct_error:
                values["exportStatus"] = ct_error
                return values
            options.update(ct_seed)
        existed = previous is not None
        role_changed = existed and previous.role != role
        # Issue #240 — a role change bumps the generation, so the node treats
        # it as a NEW accessory identity (remove-then-add) rather than an
        # in-place device-type mutation; anything else carries the identity
        # through unchanged — an ordinary update must never move it on its
        # own (PR5 design §1.3).
        if role_changed:
            # Issue #246 review finding 1 — the bump can ALSO land on a
            # generation another entry already claims (that entry was
            # re-exported while this one sat un-role-changed), so this walks
            # past every claimed generation rather than trying the immediate
            # next one and trusting it is free.
            bumped = next_generation(previous.published_as or published_id_for(device_id))
            published_as = self._first_unclaimed_identity(bumped, device_id)
        elif existed:
            published_as = previous.published_as
        else:
            # Issue #246 review finding 1 — a device whose DEFAULT identity
            # was migrated onto another device's entry must not silently
            # publish it again: `None` here means "publish the default
            # derivation", and that derivation may now belong to someone
            # else. Only claimed defaults pay the walk; the common case (never
            # migrated) is untouched and still stores `None`.
            default_identity = published_id_for(device_id)
            resolved = self._first_unclaimed_identity(default_identity, device_id)
            if resolved == default_identity:
                published_as = None
            else:
                published_as = resolved
                self.logger.info(
                    "Matter export: %s's original accessory identity (%s) was migrated to "
                    "another device, so this export becomes a NEW accessory under %s — it does "
                    "not steal back the migrated one.",
                    dev.name, default_identity, resolved)
        ct_coherence_note = ""
        if role in CT_BOUNDS_ROLES:
            # Issue #293 review — the learner's own record is carried
            # forward from a RE-READ taken here, immediately before the
            # write, not from `previous` above: `previous` may be a
            # snapshot from moments ago, and a learner adoption
            # (`ct_learner.CTBoundsLearner._adopt`, which runs on Indigo's
            # device thread, not this dialog callback's thread) landing on
            # THIS device in between must not be silently clobbered by a
            # carry-forward built from the stale copy. Mirrors `_adopt`'s
            # own re-read-immediately-before-write, for the identical
            # reason.
            fresh_previous = self.exports.get(device_id)
            for learned_key in (OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_LEARNED_MAX_MIREDS):
                options.pop(learned_key, None)
                if fresh_previous is not None and learned_key in fresh_previous.options:
                    options[learned_key] = fresh_previous.options[learned_key]
            # Coherence guard — a carried-forward learned bound can now
            # conflict with the FRESH seed just entered above (e.g. a stale
            # learned min of 250 mireds against a new seed max of 200): the
            # seed is the newer intent, so the conflicting learned key(s)
            # are dropped rather than saving an entry whose
            # `ct_bounds.effective_ct_bounds` would come out inverted or
            # collapsed — the learner simply re-earns the evidence from the
            # device again.
            eff_min, eff_max = effective_ct_bounds(options)
            if eff_min >= eff_max:
                dropped_any = False
                for learned_key in (OPTION_CT_LEARNED_MIN_MIREDS, OPTION_CT_LEARNED_MAX_MIREDS):
                    if options.pop(learned_key, None) is not None:
                        dropped_any = True
                if dropped_any:
                    ct_coherence_note = (
                        " A previously learned colour-temperature bound conflicted with this "
                        "seed and was dropped; the learner will re-confirm it from the device.")
        try:
            self.exports.upsert(ExportEntry(
                indigo_device_id=device_id, role=role,
                name_override=name_override, options=options,
                published_as=published_as,
            ))
        except Exception as exc:  # pylint: disable=broad-except
            # The store rolled back, so nothing was saved — say so rather than
            # reporting the success the old code reported unconditionally.
            self.logger.error("Matter bridge: saving the export list FAILED — %s", exc)
            self.logger.exception(exc)
            values["exportStatus"] = "FAILED to save the export list — see Event Log"
            return values
        verb = "Updated" if existed else "Added"
        self.logger.info("%s Matter export for %s (id %s) as %s%s",
                         verb, dev.name, device_id, role,
                         f' named "{name_override}"' if name_override else "")
        self._nudge_export(device_id, role_changed=role_changed)
        values["exportStatus"] = f"{verb} {dev.name} as {export_catalog.role_label(role)}. " \
                                 f"{self._role_change_warning(role_changed)}{ct_coherence_note}" \
                                 f"{self._export_summary()}"
        return values

    @staticmethod
    def _role_change_warning(role_changed: bool) -> str:
        """What a role change actually costs the user, said before they find out.

        BRIDGE_PROTOCOL §4.1 rejects changing an existing endpoint's role, so the
        plugin removes the old accessory identity and adds a new one (issue
        #240) rather than recreating it in place — recreating in place is what
        used to leave Apple Home stuck showing "could not change settings"
        until its home hub was restarted.
        """
        if not role_changed:
            return ""
        return ("Changing the role removes this accessory from your ecosystems and adds it "
                "back as a NEW one under a fresh accessory number, so you will need to put it "
                "back in its room. That is deliberate — changing it in place could leave Apple "
                "Home stuck on \"could not change settings\" until the home hub was restarted "
                "(issue #240). ")

    def _nudge_export(self, device_id: int, *, role_changed: bool = False) -> bool:
        """Tell the bridge about one changed export, without a full reconnect.

        A role change is the one case that cannot be an ``upsert``: §4.1 refuses
        it with ``role_change``, so it becomes remove-then-add.

        **Returns whether the bridge was actually told**, because this is only
        the second of two halves and they can land independently: the store
        write has already happened by the time anyone calls this, so a caller
        that reports an outcome to the user (the re-adopt confirmation,
        PR5 design §4.5) has to be able to report "saved, but not yet applied"
        rather than announce both. The existing export-dialog caller ignores
        it: its ``exportStatus`` line already says only what it saved.
        """
        self._exports_changed()  # pylint: disable=no-member  # lifecycle, stays in plugin.py
        bridge = self.export_bridge
        if bridge is None:
            return False
        try:
            if role_changed:
                bridge.replace(device_id)
            else:
                bridge.upsert(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            # Names the consequence, not just the failure: the allow-list is
            # already on disk, so this is "the ecosystems have not caught up
            # yet", not "the change was lost".
            self.logger.error(
                "Matter bridge: the export list WAS saved for device %s, but telling the bridge "
                "node about it FAILED — %s. Nothing was lost: the accessory catches up at the "
                "next reconnect/attach, which happens on its own.", device_id, exc)
            self.logger.exception(exc)
            return False
        return True

    def exportRemove(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Drop the picked device from the allow-list. Returns values only (see above)."""
        values = valuesDict
        if self.exports is None:
            values["exportStatus"] = "Plugin still starting — try again in a moment."
            return values
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportStatus"] = "Select a device to remove from the export list."
            return values
        try:
            removed = self.exports.remove(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error("Matter bridge: saving the export list FAILED — %s", exc)
            self.logger.exception(exc)
            values["exportStatus"] = "FAILED to save the export list — see Event Log"
            return values
        if not removed:
            values["exportStatus"] = "That device is not exported."
            return values
        dev = self._indigo_device(device_id)
        name = str(getattr(dev, "name", "") or "") if dev is not None else f"device {device_id}"
        self.logger.info("Removed Matter export for %s (id %s)", name, device_id)
        # XAC7: the accessory has to leave every paired ecosystem, not just the
        # allow-list. Order matters — remove the endpoint BEFORE the empty
        # allow-list stops the client out from under it.
        if self.export_bridge is not None:
            try:
                self.export_bridge.remove(device_id)
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception(exc)
        self._exports_changed()  # pylint: disable=no-member  # lifecycle, stays in plugin.py
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportStatus"] = f"Removed {name}. {self._export_summary()}"
        return values
