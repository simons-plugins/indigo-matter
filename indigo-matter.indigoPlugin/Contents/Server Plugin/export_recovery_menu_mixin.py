"""Export-bridge recovery menus (BRIDGE_PROTOCOL §3.10/§3.11): rebuild the
endpoint map, migrate an exported accessory onto a different Indigo device
(#246), and reset the bridge's ecosystem pairings.

**Why a separate mixin from the matter-server and bridge-agent bands it used
to share a file with (issue #146 split, one file → three).** See
:mod:`matter_server_menu_mixin`'s docstring for the full argument — issue #146
requires every callback to resolve as a flat attribute on ``Plugin``, not that
few files do it. This file takes the **outbound** export bridge's recovery
surface; the bridge node's LaunchAgent lifecycle (also outbound, but a
different concern — starting/stopping a process rather than reconciling what
it serves) is ``bridge_agent_menu_mixin.py``, and the **inbound** controller
and node-device recovery are ``matter_server_menu_mixin.py``.

Migrate re-verifies every step at Execute time rather than trusting a
picker's possibly-stale read — see its own docstrings for the step-by-step
design references (#246 design §3).

**Re-adopt (issue #219) was removed in issue #274b.** A deleted or
un-exported Indigo device now destroys its Matter accessory outright
(#274) rather than leaving it as an orphan a user could hand to a
replacement device — see workspace ADR-0015. "Migrate an exported
accessory…" remains the way to move a LIVE accessory's identity onto a
different device.

See issue #146.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import bridge_client            # bridge_client.rebuild_timeout_for
import export_catalog
from bridge_protocol import parse_published_id, published_id_for
from export_store import ExportEntry, OPTION_STATE_INVERT, OPTION_STATE_KEY
from plugin_constants import (
    EXCLUDED_OPTION_PREFIX,
    EXPORT_PICKER_LIMIT,
    FACTORY_RESET_TIMEOUT,
    NO_SELECTION_ID,
    TRUNCATED_OPTION,
    degrades_to_list_error,
)


def _migrated_options(source_options: dict, target_options: Optional[dict]) -> dict:
    """Options for a migrated entry: the accessory's, but the TARGET's mapping.

    Everything about an export follows the accessory — role, name override,
    identity — with one exception, and it is the exception because of what a
    state mapping actually describes. `stateKey`/`stateInvert` (ADR-0012) say
    *which boolean on this piece of hardware is the reading*. That is a fact
    about the device, not about the accessory riding on it, and the two
    devices in a migration are different hardware.

    Carrying the source's mapping across was the bug: migrating an accessory
    from a Masquerade proxy (no mapping — it has a real `onState`) onto a
    Texecom zone produced an entry with no mapping for a device that needs
    one. It classified as merely mappable, the endpoint provider skipped it,
    and the accessory went dark instead of moving. The reverse is just as
    wrong: a mapping naming a state the new device does not publish.

    """
    carried = {key: value for key, value in (source_options or {}).items()
               if key not in (OPTION_STATE_KEY, OPTION_STATE_INVERT)}
    for key in (OPTION_STATE_KEY, OPTION_STATE_INVERT):
        if target_options and key in target_options:
            carried[key] = target_options[key]
    return carried


@dataclass(frozen=True)
class _MigrateSourceIdentity:
    """The slice of the migrate source entry :meth:`ExportRecoveryMenuMixin._migrate_device_row`
    actually reads (``role``, ``unique_id``) — a two-field stand-in so the
    migrate device picker (:meth:`ExportRecoveryMenuMixin.getMigrateDevices`,
    #246 design §3.2) can build one from an :class:`~export_store.ExportEntry`
    without handing the whole entry around.
    """
    role: str
    unique_id: str


class ExportRecoveryMenuMixin:
    """Export-bridge recovery menus: rebuild the endpoint map, migrate an
    exported accessory, and reset the bridge's ecosystem pairings.

    Composed into ``Plugin`` alongside the other six mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146.
    exports: Any
    export_bridge: Any
    runtime: Any
    logger: Any

    # ------------------------------------------------------------------
    # Export-bridge recovery menus (BRIDGE_PROTOCOL §3.10/§3.11)
    # ------------------------------------------------------------------
    def _recovery_client(self, errors, field: str, *, require_attached: bool = False):
        """The bridge client, or ``None`` with ``errors`` filled in.

        The §3.10/§3.11 recovery commands need a live socket, and the state
        they exist to fix is exactly the one where the plugin holds the
        connection open UN-attached (§1.1 recovery). So `connected`, not
        `attached`, is the right gate for THEM — requiring an attach would
        make the rebuild unreachable in the only situation that needs it.

        Migrate (#246 design §3.2 step 2) is the opposite case: its commit
        ends with a mid-session reattach nudge, which needs a real, answered
        ``attach`` to land on — a node only serving the §1.1 recovery trio
        cannot take one — so ``require_attached=True`` demands
        :attr:`~bridge_client.BridgeClient.attached` rather than merely
        :attr:`~bridge_client.BridgeClient.connected`.
        """
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        ready = client is not None and (client.attached if require_attached else client.connected)
        if not ready:
            if require_attached:
                msg = ("Not connected to the Matter bridge node. Start it, let the plugin "
                       "attach, then re-open this dialog.")
            else:
                msg = ("Not connected to the Matter bridge node. Start it (it is launched by "
                       "hand in this build), export at least one device so the plugin connects, then "
                       "try again.")
            self.logger.warning(msg)
            errors[field] = "Not connected to the bridge node — see the log."
            return None
        return client

    def menuRebuildEndpointMap(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """§3.11 — the way out of the endpoint_map_invalid refuse-to-start state.

        Two outcomes are reported separately, because they can differ and the
        expensive one is the first: the rebuild is irreversible (it discards
        the persisted baseline and adopts the live numbers) while the re-attach
        that follows it is an ordinary connection step that retries on its own.
        It renumbers nothing, so it cannot itself duplicate accessories — any
        duplication belongs to the storage loss that caused the refusal (#132).
        Reporting the pair as one used to tell users their node was "unchanged
        and still refusing" over a map that had already been rewritten — and
        invite them to do it again.
        """
        errors = indigo.Dict()
        if not self._truthy(valuesDict.get("confirm")):  # pylint: disable=no-member  # ExportDialogMixin
            errors["confirm"] = ("Tick the box — a rebuild replaces the endpoint-number record "
                                 "and cannot be undone.")
            return (False, valuesDict, errors)
        client = self._recovery_client(errors, "confirm")
        if client is None:
            return (False, valuesDict, errors)
        if not client.recovery:
            # M11: `connected` is the right gate for REACHING the node, but it
            # is not a reason to rebuild. Run against a healthy node this
            # silently discards the retained endpoint-number allocations of
            # every export that is not currently live — §3.3 keeps them exactly
            # so that re-adding a device restores its accessory — and there is
            # nothing to recover from in the first place.
            msg = ("Matter bridge: the bridge node is NOT refusing to serve endpoints, so there "
                   "is nothing to rebuild. Rebuilding anyway would discard the retained endpoint "
                   "numbers of every device that is not currently exported, and those are what "
                   "make re-adding one restore the same accessory.")
            self.logger.warning(msg)
            errors["confirm"] = ("The bridge node is not refusing to export — there is nothing "
                                 "to rebuild. See the log.")
            return (False, valuesDict, errors)
        # Derived from the same two deadlines the call itself is built from: a
        # flat number here is the one that expires first on a large export list,
        # turning a rebuild that worked into a reported failure.
        deadline = bridge_client.rebuild_timeout_for(
            len(self.exports) if self.exports is not None else 0)
        try:
            status = self.runtime.submit(client.rebuild_endpoint_map()).result(timeout=deadline)
        except Exception as exc:
            # Never report success over a rebuild that did not persist: the node
            # answers with an error rather than a StatusReport when the new map
            # could not be written, and the refusal is still in force.
            self.logger.error("Matter bridge: rebuilding the endpoint map FAILED — %s. The bridge "
                              "node is unchanged and still refusing to export.", exc)
            self.logger.exception(exc)
            errors["confirm"] = "Rebuild failed — see the log. Nothing was changed."
            return (False, valuesDict, errors)
        self.logger.warning(
            "Matter bridge: endpoint map REBUILT — the bridge node has stopped refusing and is "
            "serving %d endpoint(s). Nothing was renumbered. If only the map file was damaged, "
            "no paired ecosystem will see any change; if the bridge's Matter storage was lost, "
            "your ecosystems already hold dead accessories under the old numbers — delete those "
            "by hand. Do NOT run this again for the same fault.",
            status.endpoint_count)
        for warning in status.warnings:
            self.logger.warning("Matter bridge: the bridge node reports — %s", warning)
        if not client.attached:
            # The rebuild stands; only the connection step after it did not. The
            # client's own triage has already named the reason at error level.
            self.logger.warning(
                "Matter bridge: the rebuild succeeded but re-attaching to the bridge node did "
                "not. The rebuild does NOT need repeating — exports resume when the connection "
                "does, and the reason is logged above.")
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # Migrate an exported accessory… (#246 design §3, issue #246)
    # ------------------------------------------------------------------
    @degrades_to_list_error  # #246 finding 4 — sibling pickers' discipline
    def getMigrateSources(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Source picker for "Migrate an exported accessory…" (#246 design §3.2).

        This needs no bridge round trip at all: the allow-list
        (``self.exports``) IS the export list, so it is local truth rather
        than something only a live node could confirm. A row's accessory
        number is decoration, read via :meth:`_previous_accessory_number` —
        best-effort from the last attached client's status, omitted (not
        guessed) when there is none.

        A source entry whose Indigo device has been DELETED is still listed.
        That is not an oversight: it is ALSO a legitimate migrate source — the
        allow-list is never auto-pruned when a device disappears (see
        :meth:`export_dialog_mixin.ExportDialogMixin._reconcile_exports`), so
        an accessory can be moved onto a live replacement device directly.
        The row names it "(device N no longer exists)" in place of the
        device's name, since there is nothing else to call it.
        """
        if self.exports is None:
            return [(NO_SELECTION_ID,
                     "(plugin still starting — re-open this dialog in a moment)")]
        entries = self.exports.all()
        if not entries:
            return [(NO_SELECTION_ID, "(nothing is currently exported — nothing to migrate)")]
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        options = []
        for entry in entries:
            identity = entry.published_as or published_id_for(entry.indigo_device_id)
            dev = self._indigo_device(entry.indigo_device_id)  # pylint: disable=no-member  # ExportDialogMixin
            name = dev.name if dev is not None \
                else f"(device {entry.indigo_device_id} no longer exists)"
            number = self._previous_accessory_number(client, entry.indigo_device_id)
            suffix = f" (accessory #{number})" if number is not None else ""
            options.append((str(entry.indigo_device_id),
                            f"{name} — {export_catalog.role_label(entry.role)} — "
                            f"accessory {identity}{suffix}"))
        return options

    def migrateSourceChanged(self, valuesDict, typeId="", devId=0):  # noqa: N802, ARG002
        """No-op callback on the source menu (#246 design §3.2). Its only job
        is making Indigo round-trip to the plugin, which is what re-runs
        ``getMigrateDevices`` below it: ConfigUI has no per-field change
        event, so a menu's own CallbackMethod is the whole mechanism."""
        return valuesDict

    @degrades_to_list_error
    def getMigrateDevices(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        # pylint: disable=too-many-locals  # same two-list shape as getExportCandidates
        """Device picker for "Migrate an exported accessory…" (#246 design §3.2).

        Uses :meth:`_migrate_device_row` for the per-device rule — selectable
        when role-eligible, ● when already exported, every other device SHOWN
        with its reason, loop-guard devices absent — via
        :class:`_MigrateSourceIdentity`, a two-field stand-in built from the
        source :class:`~export_store.ExportEntry` so the row-builder does not
        need the whole entry.

        The one rule that IS specific to migrate: the source device itself is
        absent from the list, not merely excluded-with-a-reason — it is the
        row directly above in the dialog, not a target for itself.
        """
        if self.exports is None:
            return [(NO_SELECTION_ID,
                     "(plugin still starting — re-open this dialog in a moment)")]
        selected = str((valuesDict or {}).get("migrateSource", "") or "")
        if not selected or selected == NO_SELECTION_ID:
            return []
        try:
            source_id = int(selected)
        except (TypeError, ValueError):
            return []
        source_entry = self.exports.get(source_id)
        if source_entry is None:
            return []
        source_identity = _MigrateSourceIdentity(
            role=source_entry.role,
            unique_id=source_entry.published_as or published_id_for(source_id))
        exported = self.exports.ids()
        plugin_id = self._export_plugin_id()  # pylint: disable=no-member  # ExportDialogMixin
        eligible: list = []
        explained: list = []
        failures = 0
        for dev in indigo.devices:
            if dev.id == source_id:
                continue                     # the row above, not a target for itself
            try:
                row = self._migrate_device_row(dev, source_identity, plugin_id, exported)
                if row is None:
                    continue
                (explained if row[0].startswith(EXCLUDED_OPTION_PREFIX)
                 else eligible).append((str(getattr(dev, "name", "") or "").lower(), row))
            except Exception as exc:  # pylint: disable=broad-except
                self._log_row_failure(exc, first=not failures)  # pylint: disable=no-member
                failures += 1
        options = (self._sorted_device_rows(eligible)
                   + self._sorted_device_rows(explained)[:EXPORT_PICKER_LIMIT])
        if len(explained) > EXPORT_PICKER_LIMIT:
            options.append(TRUNCATED_OPTION)
        return options

    def _migrate_refuse(self, errors, field: str, msg: str) -> None:
        """One #246 design §3.2 SUBSTANTIVE refusal: dialog error plus a
        matching WARNING — never used for a plain "tick the box"/"make a
        selection" nag, which stays dialog-only."""
        self.logger.warning("Matter bridge: migrate REFUSED — %s", msg)
        errors[field] = msg

    def _migrate_pick_source(self, valuesDict, errors):
        """The picked export entry (or "select one"), RE-READ from the store at
        Execute time (#246 design §3.2 step 3) — never trusted from the picker's
        own read, which may be stale by the time Execute runs (something else
        un-exported it while the dialog was open).

        Returns ``(source_id, entry)``, or ``(0, None)`` with ``errors`` filled in.
        """
        selection = str(valuesDict.get("migrateSource", "") or "")
        if not selection or selection == NO_SELECTION_ID:
            errors["migrateSource"] = "Select the exported accessory to migrate."
            return (0, None)
        try:
            source_id = int(selection)
        except (TypeError, ValueError):
            self.logger.error(
                "Matter bridge: migrate got a source selection that is not a device id — %r. "
                "Nothing was changed; this is a plugin fault, not a bad choice.", selection)
            errors["migrateSource"] = "Invalid selection."
            return (0, None)
        entry = self.exports.get(source_id)
        if entry is None:
            self._migrate_refuse(errors, "migrateSource",
                                 "That accessory is no longer exported — something removed it "
                                 "while this dialog was open. Nothing was changed.")
            return (0, None)
        return (source_id, entry)

    def _migrate_pick_device(self, source_id, source_entry, valuesDict, errors):
        # pylint: disable=too-many-return-statements  # one return per #246 design §3.2 refusal
        """The picked target device (or "select one"), re-verified per #246 design
        §3.2 steps 4-5: still exists, still classifies as exportable, can take
        the source's role (cross-role REFUSED — owner ruling 2/E3, #219/#240
        reasoning), and is not the source device itself.

        Returns the device, or ``None`` with ``errors`` filled in.
        """
        selection = str(valuesDict.get("migrateDevice", "") or "")
        if not selection or selection == NO_SELECTION_ID:
            errors["migrateDevice"] = "Select the Indigo device to migrate it to."
            return None
        if selection.startswith(EXCLUDED_OPTION_PREFIX):
            self._migrate_refuse(errors, "migrateDevice",
                                 "That row is listed with the reason it cannot take this "
                                 "accessory — pick a device offered without one.")
            return None
        try:
            device_id = int(selection)
        except (TypeError, ValueError):
            self.logger.error(
                "Matter bridge: migrate got a target selection that is neither a device id nor "
                "an unselectable row — %r. Nothing was changed; this is a plugin fault, not a "
                "bad choice.", selection)
            errors["migrateDevice"] = "Invalid selection."
            return None
        dev = self._indigo_device(device_id)  # pylint: disable=no-member  # ExportDialogMixin
        if dev is None:
            self._migrate_refuse(errors, "migrateDevice",
                                 "That device no longer exists — refresh the list.")
            return None
        own = self.exports.get(dev.id)
        # ADR-0012 — see _migrate_device_row. eligible_target (export_catalog.py)
        # is the classify-then-check-role dance both need.
        result = export_catalog.eligible_target(  # pylint: disable=no-member
            dev, self._export_plugin_id(), own.options if own else None, source_entry.role)
        if isinstance(result, export_catalog.TargetRefusal):
            if result.role_mismatch:
                self._migrate_refuse(
                    errors, "migrateDevice",
                    f'"{dev.name}" cannot appear as a {export_catalog.role_label(source_entry.role)}. '
                    "Migrating only works when the replacement device can take the same role as "
                    "the accessory it is inheriting; export it normally instead, and it becomes a "
                    "new accessory.")
                return None
            self._migrate_refuse(
                errors, "migrateDevice",
                f"{dev.name} needs a state mapping before anything can be migrated onto it — "
                "export it directly first, which is where that is asked."
                if isinstance(result.verdict, export_catalog.MappableDevice)
                else f"{dev.name} cannot be exported: {getattr(result.verdict, 'reason', 'not exportable')}")
            return None
        # 5. target != source — defensive against a stale/hand-crafted
        # selection; the picker itself never offers this row (it is absent,
        # not excluded-with-a-reason — see getMigrateDevices).
        if device_id == source_id:
            self._migrate_refuse(errors, "migrateDevice",
                                 "That is the device already exporting this accessory — pick a "
                                 "different one to migrate it to.")
            return None
        return dev

    def _migrate_source_display_name(self, source_id: int) -> str:
        """The source device's current Indigo name, or a placeholder naming
        the id when it no longer exists (:meth:`getMigrateSources`'s "still a
        legitimate migrate source" case)."""
        dev = self._indigo_device(source_id)  # pylint: disable=no-member  # ExportDialogMixin
        return dev.name if dev is not None else f"device {source_id}"

    def _log_migrate_confirmation(self, source_id: int, source_entry, dev, source_number,
                                  target_previous, target_previous_number) -> None:
        # pylint: disable=too-many-arguments
        """#246 design §3.2 step 8 — the one WARNING a successful migrate leaves
        behind. The promise it makes is the one migrate actually earns: the
        MIGRATED accessory's identity never lapsed, so no ecosystem ever
        processed a removal of IT, and its room, name, scenes and automations
        survive BECAUSE of that.

        **The headline is scoped to the migrated accessory itself (issue #246
        review finding 3).** It used to read "nothing was removed and nothing
        was re-added" unconditionally — false in the ● target-already-exported
        case, where this very attach DID remove the target's own accessory.
        The corrective sentence below is what covers that; the headline no
        longer claims what it cannot promise for the whole operation.

        **The removal sentence is gated on whether a removal actually
        happened (``target_previous is not None``), not on whether its NUMBER
        could be read (``target_previous_number``).** Those used to be the
        same test, but they answer different questions — a target's own
        accessory can be discarded by the atomic commit even when the client
        has no cached :class:`~bridge_protocol.StatusReport` naming its
        number, and gating on the number silently dropped the sentence for
        that case even though the removal genuinely happened. ``(number N)``
        is decoration, included only when it happens to be known.
        """
        role_label = export_catalog.role_label(source_entry.role)
        label = source_entry.label_for(self._migrate_source_display_name(source_id))
        role_and_number = (f"{role_label}, accessory number {source_number}"
                           if source_number is not None else role_label)
        superseded = ""
        if target_previous is not None:
            clause = f" (number {target_previous_number})" if target_previous_number is not None else ""
            superseded = (f' "{dev.name}"\'s own previous accessory{clause} has been removed '
                          "from your ecosystems.")
        self.logger.warning(
            'Matter export: MIGRATED accessory "%s" (%s) from Indigo device %s to "%s" '
            '(id %s). The migrated accessory keeps its room, its name, and every scene and '
            'automation — its identity never lapsed and nothing about it was removed or '
            're-added.%s',
            label, role_and_number, source_id, dev.name, dev.id, superseded)

    def _migrate_commit(self, client, source_id, source_entry, dev, errors, valuesDict):
        # pylint: disable=too-many-arguments,too-many-locals
        """#246 design §3.2 steps 6-8: one ATOMIC ``ExportStore.replace_all()`` —
        never two sequential ``upsert``s, which would transiently leave two
        entries claiming one published identity, the exact duplicate state
        that refuses the WHOLE next attach — then the mid-session
        :meth:`export_bridge.ExportBridge.reattach` nudge and the confirmation
        WARNING (ADR-0011).

        ``name_override``/``options`` are carried FROM THE SOURCE entry: the
        export configuration follows the accessory, not the hardware. A
        pre-existing entry for the TARGET device is discarded along with its
        own accessory — the ● warning in the dialog already said so.

        The pre-migrate accessory numbers are read BEFORE the store write and
        the nudge. Only the nudge touches ``client.status`` — its reattach is
        what makes the bridge's live status describe the NEW arrangement (the
        store write itself does not, though a spontaneous reconnect after it
        could) — but by the time the confirmation logs, either one may have
        already happened, so there is nothing left to read the OLD numbers
        from.
        """
        target_id = dev.id
        identity = source_entry.published_as or published_id_for(source_id)
        source_number = self._previous_accessory_number(client, source_id)
        target_previous = self.exports.get(target_id)
        target_previous_number = (self._previous_accessory_number(client, target_id)
                                  if target_previous is not None else None)
        new_entry = ExportEntry(
            indigo_device_id=target_id,
            role=source_entry.role,
            name_override=source_entry.name_override,
            options=_migrated_options(source_entry.options,
                                      target_previous.options if target_previous else None),
            published_as=identity,
        )
        new_entries = [entry for entry in self.exports.all()
                      if entry.indigo_device_id not in (source_id, target_id)]
        new_entries.append(new_entry)
        try:
            self.exports.replace_all(new_entries)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error("Matter bridge: migrate FAILED to save the export list — %s", exc)
            self.logger.exception(exc)
            errors["migrateDevice"] = "FAILED to save the export list — see Event Log."
            return (False, valuesDict, errors)
        self._exports_changed()  # pylint: disable=no-member  # lifecycle, stays in plugin.py
        told = self.export_bridge.reattach()
        if not told:
            # The store write stands (the next reconnect/attach applies it),
            # but the confirmation below claims no ecosystem saw any change —
            # and that has not been confirmed yet. Never report success over
            # an operation that has not landed.
            errors["migrateDevice"] = ("Saved, but the bridge node was not told — "
                                       "see Event Log.")
            return (False, valuesDict, errors)
        self._log_migrate_confirmation(source_id, source_entry, dev, source_number,
                                       target_previous, target_previous_number)
        return (True, valuesDict)

    def menuMigrateExport(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Execute "Migrate an exported accessory…" (#246 design §3.2, issue #246).

        Every step re-verified at Execute time rather than trusted from the
        picker, each step its own helper so later steps can assume earlier
        ones already hold.
        """
        errors = indigo.Dict()
        # 1. migrateConfirm ticked.
        if not self._truthy(valuesDict.get("migrateConfirm")):  # pylint: disable=no-member  # ExportDialogMixin
            errors["migrateConfirm"] = (
                "Tick the box — migrating moves this accessory to a different Indigo device.")
            return (False, valuesDict, errors)
        # 2. a live, ATTACHED bridge client (#246 design §3.2 step 2, E1 precedent) —
        #    the mid-session reattach the commit ends with needs one to nudge.
        client = self._recovery_client(errors, "migrateConfirm", require_attached=True)
        if client is None:
            return (False, valuesDict, errors)
        # 3.
        source_id, source_entry = self._migrate_pick_source(valuesDict, errors)
        if source_entry is None:
            return (False, valuesDict, errors)
        # 4-5.
        dev = self._migrate_pick_device(source_id, source_entry, valuesDict, errors)
        if dev is None:
            return (False, valuesDict, errors)
        # 6-8.
        return self._migrate_commit(client, source_id, source_entry, dev, errors, valuesDict)

    # ------------------------------------------------------------------
    # Migrate helpers shared with the ordinary export dialog
    # ------------------------------------------------------------------
    def _migrate_device_row(self, dev, source, plugin_id: str, exported) -> Optional[tuple]:
        """One #246 design §3.2 device-picker row, or ``None`` to omit it. May
        raise — the caller contains it, matching
        ``ExportDialogMixin._candidate_row``.

        Selectable — a plain ``str(dev.id)`` id — when ``source.role`` is in
        ``eligible_roles``; marked ● when already exported (moving the source
        onto it will discard that device's own accessory too — the dialog
        says so).

        **Every other device is SHOWN, unselectable, WITH its reason**
        (XAC9/PRD §5.2, the same rule ``_candidate_row`` follows) rather than
        a bare omission — each is a device a user has a specific expectation
        about:

        * not exportable (the classifier's own reason);
        * exportable, but cannot take this accessory's role — the refusal
          :meth:`_migrate_pick_device` makes at Execute time, said here first;
        * already publishing a DIFFERENT accessory's identity than the one
          being migrated, naming which.

        The loop guard stays ABSENT rather than excluded, exactly as XAC6
        requires of the export picker: every such device shadows one the user
        already sees.

        "A DIFFERENT accessory" is decided by whose device id the identity
        embeds, not merely by it being non-default: a device publishing under
        its OWN ``indigo-<ownId>~<generation>`` (it has changed role at some
        point, issue #240) has claimed nobody else's accessory and stays
        selectable.
        """
        mark = "● " if dev.id in exported else ""
        # Classified WITH the device's OWN saved options (ADR-0012), not
        # bare. A custom device that has already been exported carries its
        # state mapping in that entry, and asking without it reads a
        # perfectly-configured device back as one still needing a mapping —
        # which is how a zone the user had just exported stayed unselectable
        # as a migrate target.
        own = self.exports.get(dev.id) if self.exports is not None else None
        verdict = export_catalog.classify(dev, plugin_id, own.options if own else None)
        # Positive test, not `not isinstance(..., Excluded)`. ADR-0012 gave
        # `classify` a third outcome, and a device that could be exported once
        # its state is mapped answers False to `Excluded` while carrying no
        # `eligible_roles` at all — so a negative test fell through to the line
        # below and raised, which the caller turns into a DROPPED ROW. Every
        # such device vanished from both device pickers with no reason given:
        # the one outcome this method's whole docstring exists to prevent.
        if isinstance(verdict, export_catalog.Excluded):
            # export_catalog.excluded_row is the loop-guard-omit (XAC6) /
            # not-exportable-reason row `_candidate_row` builds too — shared
            # because the two pickers word this one outcome identically.
            return export_catalog.excluded_row(verdict, dev.id, dev.name, mark, EXCLUDED_OPTION_PREFIX)
        if isinstance(verdict, export_catalog.MappableDevice):
            # Shown, not selectable. Inheriting an accessory does not ask
            # which state is the reading, so an entry retargeted onto an
            # unmapped device would publish nothing — the accessory would
            # go dark rather than move. Export it directly first (which
            # does ask), then it is an ordinary target.
            return (f"{EXCLUDED_OPTION_PREFIX}{dev.id}",
                    f"{mark}{dev.name} — needs a state mapping first; "
                    f"export it directly, then retry")
        if source.role not in verdict.eligible_roles:
            return (f"{EXCLUDED_OPTION_PREFIX}{dev.id}",
                    f"{mark}{dev.name} — cannot appear as a "
                    f"{export_catalog.role_label(source.role)}")
        entry = self.exports.get(dev.id)
        if entry is not None:
            publishing_as = entry.published_as or published_id_for(dev.id)
            if publishing_as != source.unique_id:
                claimed = parse_published_id(publishing_as)
                if claimed is None or claimed.device_id != dev.id:
                    return (f"{EXCLUDED_OPTION_PREFIX}{dev.id}",
                            f"{mark}{dev.name} — already publishing accessory "
                            f"{publishing_as}")
        return (str(dev.id), f"{mark}{dev.name}")

    @staticmethod
    def _sorted_device_rows(rows: list) -> list:
        """``(sort_key, row)`` pairs → rows, by that key.

        The key is the lower-cased device name — uniform across the one call
        site that feeds this, which is all ``sorted`` requires.

        ``indigo.devices`` order is neither alphabetical nor stable-looking to
        a user: Indigo collates with a Finder-style rule that treats
        punctuation differently, so "Apple TV Power Socket" sorts BEFORE
        "AP_78:8a:20:b3:cc:4f". Concatenating two such runs — selectable, then
        explained — reads as an alphabet that restarts halfway down a list of
        several hundred devices.

        Sorting is applied WITHIN each block rather than across both, so the
        eligible-first rule (and its never-truncated guarantee) is untouched:
        the pickable devices stay at the top, and each block is now scannable.
        The device NAME is the key, not the row label, because the label
        carries the ``●`` exported marker and the trailing reason — sorting on
        it would file every exported device under "●".
        """
        return [row for _key, row in sorted(rows, key=lambda pair: pair[0])]

    @staticmethod
    def _previous_accessory_number(client, device_id: int) -> Optional[int]:
        """The endpoint number of ``device_id``'s accessory, read from the
        client's last :class:`StatusReport` (attach/get_status) — the only
        place a still-live endpoint's number is cached client-side. ``None``
        when there is nothing to report (the device was not exported, or no
        status has landed yet), which the confirmation log then omits rather
        than guesses.
        """
        status = getattr(client, "status", None)
        if status is None:
            return None
        try:
            for summary in status.endpoints:
                if summary.indigo_device_id == device_id:
                    return summary.endpoint_number
        except Exception:  # pylint: disable=broad-except
            # A malformed/unexpected `status.endpoints` degrades to "nothing
            # to report" — the confirmation log must never raise over a
            # cosmetic omission, and by the time it runs the store write and
            # the bridge nudge have both already landed, so an exception here
            # would unwind the REPORT of a change that is not itself undone.
            # Deliberately every exception, not just the `TypeError` a
            # non-iterable produces: the docstring's promise is unconditional
            # and a summary object with a raising property would break a
            # narrower one.
            return None
        return None

    def _identity_claimant(self, unique_id: str, device_id: int) -> Optional[int]:
        """Another ``ExportStore`` entry already publishing as ``unique_id``
        — the plugin-side half of the duplicate-identity guard. The node's
        own duplicate-``publishedAs`` refusal at attach time
        (``parseEndpointSpecs``) is the backstop that actually matters — a
        hand-edited ``.indiPref`` bypasses this one.

        Used by :meth:`export_dialog_mixin.ExportDialogMixin._first_unclaimed_identity`
        to walk past a generation "Migrate an exported accessory…" has since
        claimed for another device, so an ordinary re-export never collides
        with it.

        **Compares EFFECTIVE identities, not the stored field.**
        ``published_as`` is ``None`` on every ordinary export, and that does
        not mean "claims nothing": it means "publishes as
        ``indigo-<own device id>``". Matching the raw field would make an
        ordinary export's claim invisible here — and the node refuses a
        duplicate ``publishedAs`` for the whole attach (``malformed_args``),
        which takes every export offline rather than just the new one.
        """
        for entry in self.exports.all():
            if entry.indigo_device_id == device_id:
                continue
            claimed = entry.published_as or published_id_for(entry.indigo_device_id)
            if claimed == unique_id:
                return entry.indigo_device_id
        return None

    # ------------------------------------------------------------------
    # Reset the bridge's ecosystem pairings (BRIDGE_PROTOCOL §3.10)
    # ------------------------------------------------------------------
    def menuResetBridgePairings(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """§3.10 — wipe the Matter bridge's commissioning and re-advertise."""
        errors = indigo.Dict()
        # Two boxes, deliberately. This is the only plugin action that destroys
        # every ecosystem pairing at once, and it is irreversible without
        # re-pairing each ecosystem by hand.
        # pylint: disable=no-member  # ExportDialogMixin._truthy via MRO (issue #146)
        if not self._truthy(valuesDict.get("confirm")) \
                or not self._truthy(valuesDict.get("confirmAgain")):
            field = "confirm" if not self._truthy(valuesDict.get("confirm")) else "confirmAgain"
            errors[field] = "Tick BOTH boxes — this removes every ecosystem pairing."
            return (False, valuesDict, errors)
        # pylint: enable=no-member
        client = self._recovery_client(errors, "confirmAgain")
        if client is None:
            return (False, valuesDict, errors)
        try:
            # preserve_endpoint_numbers=True: a user resetting to re-pair the
            # same ecosystems should not also lose accessory identity. The
            # "the map itself is corrupt" path is the rebuild above.
            self.runtime.submit(client.factory_reset(True)).result(timeout=FACTORY_RESET_TIMEOUT)
        except Exception as exc:
            self.logger.error("Matter bridge: resetting the bridge pairings FAILED — %s. "
                              "Pairings are unchanged.", exc)
            self.logger.exception(exc)
            errors["confirmAgain"] = "Reset failed — see the log. Pairings were not changed."
            return (False, valuesDict, errors)
        self.logger.warning(
            "Matter bridge: the bridge node's pairings have been RESET. It is advertising for "
            "commissioning again — pair it from each ecosystem, and remove the now-dead Indigo "
            "bridge from any ecosystem that still lists it.")
        return (True, valuesDict)
