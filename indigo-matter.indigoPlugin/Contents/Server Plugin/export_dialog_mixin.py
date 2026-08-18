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
from export_store import ExportEntry, OPTION_INVERT
from plugin_constants import (
    EXCLUDED_OPTION_PREFIX, EXPORT_PICKER_LIMIT, LIST_ERROR_OPTION,
    MENU_MANAGE_EXPORTS, MENU_MIGRATE_EXPORT, MENU_READOPT_EXPORT, MENU_UNPAIR_ECOSYSTEM,
    NO_MATCH_OPTION, NO_SELECTION_ID, NO_SELECTION_LABEL, ROW_ERROR_LABEL, TRUNCATED_OPTION,
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
                verdict = export_catalog.classify(dev, plugin_id)
                if isinstance(verdict, export_catalog.Excluded):
                    self.logger.warning(
                        "Matter export allow-list: %s (id %s) is exported as %s but is no longer "
                        "exportable: %s. It will not be bridged.",
                        getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                        verdict.reason)
                elif entry.role not in verdict.eligible_roles:
                    self.logger.warning(
                        "Matter export allow-list: %s (id %s) is exported as %s, which this "
                        "device no longer offers (%s). Re-pick its role in "
                        "'Manage Matter Exports…'.",
                        getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                        ", ".join(verdict.eligible_roles))
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

    @staticmethod
    def _candidate_row(dev, name: str, plugin_id: str, exported) -> Optional[tuple[str, str]]:
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
        verdict = export_catalog.classify(dev, plugin_id)
        if isinstance(verdict, export_catalog.Excluded):
            if verdict.reason == export_catalog.REASON_LOOP_GUARD:
                return None
            return (f"{EXCLUDED_OPTION_PREFIX}{device_id}",
                    f"{mark}{name} — not exportable: {verdict.reason}")
        return (str(device_id), f"{mark}{name}")

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
        try:
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
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def getExportRoles(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Roles the picked device may legitimately be exported as (§5.2).

        Empty for no selection or an excluded pick — an empty role menu is the
        honest rendering of "there is nothing you may choose here". An outright
        failure is NOT empty: it says so, so the user does not read a broken
        callback as "this device offers no roles".
        """
        try:
            kind, device_id = self._export_selection(valuesDict)
            if kind != "device":
                return []
            dev = self._indigo_device(device_id)
            if dev is None:
                return []
            verdict = export_catalog.classify(dev, self._export_plugin_id())
            if isinstance(verdict, export_catalog.Excluded):
                return []
            options = []
            for role in verdict.eligible_roles:
                try:
                    options.append((role, export_catalog.role_label(role)))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not options)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def getCurrentExports(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Read-only summary of the allow-list (one row per export)."""
        try:
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
                    options.append((str(entry.indigo_device_id), label))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)
                    failures += 1
                    options.append((f"{EXCLUDED_OPTION_PREFIX}err{len(options)}",
                                    f"— {ROW_ERROR_LABEL}"))
            return options or [(NO_SELECTION_ID, "(nothing exported yet)")]
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

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
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = self._export_summary()
            return values
        dev = self._indigo_device(device_id)
        if kind == "excluded" or dev is None:
            reason = "that device no longer exists"
            if dev is not None:
                verdict = export_catalog.classify(dev, self._export_plugin_id())
                reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                    else "that device is not exportable"
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = (f"Not exportable: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if isinstance(verdict, export_catalog.Excluded):
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = (f"Not exportable: {verdict.reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        entry = self.exports.get(device_id) if self.exports is not None else None
        if entry is not None:
            values["exportRole"] = entry.role
            values["exportName"] = entry.name_override or ""
            values["exportInvert"] = bool(entry.options.get(OPTION_INVERT, False))
            values["exportStatus"] = f"{dev.name} is exported as {export_catalog.role_label(entry.role)}."
        else:
            values["exportRole"] = verdict.default_role
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = f"{dev.name} is not exported yet."
        return values

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
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if kind == "excluded" or isinstance(verdict, export_catalog.Excluded):
            reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                else "not exportable"
            values["exportStatus"] = (f"{dev.name} cannot be exported: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        role = str(values.get("exportRole", "") or "")
        if role not in verdict.eligible_roles:
            values["exportStatus"] = ("Choose how this device should appear "
                                      f"({', '.join(verdict.eligible_roles)}).")
            return values
        name_override = str(values.get("exportName", "") or "").strip() or None
        options = {}
        if role == export_catalog.ROLE_WINDOW_COVERING and self._truthy(values.get("exportInvert")):
            options[OPTION_INVERT] = True
        previous = self.exports.get(device_id)
        existed = previous is not None
        role_changed = existed and previous.role != role
        # Issue #240 — a role change bumps the generation, so the node treats
        # it as a NEW accessory identity (remove-then-add) rather than an
        # in-place device-type mutation; anything else carries the identity
        # through unchanged — an ordinary update must never move it on its
        # own (PR5 design §1.3).
        if role_changed:
            published_as = next_generation(previous.published_as or published_id_for(device_id))
        else:
            published_as = previous.published_as if existed else None
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
                                 f"{self._role_change_warning(role_changed)}{self._export_summary()}"
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
