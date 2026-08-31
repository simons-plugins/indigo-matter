"""Pairing (PRD §6, BRIDGE_PROTOCOL §3.7-§3.9) and fabric backup/restore — the
"Pair Matter Bridge…"/"Unpair an Ecosystem…" menus, the fabric picker, and the
export-bridge backup/restore menus (issue #26, #136). See issue #146.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import bridge_agent
import bridge_protocol
import bridge_health          # bridge_health.describe_fabric
import fabric_backup
from plugin_constants import (
    NO_SELECTION_ID, PAIRING_READ_TIMEOUT,
    UNPAIR_TIMEOUT, WINDOW_OPEN_TIMEOUT, degrades_to_list_error,
)
from server_process import ServerProcess


class PairingMenuMixin:
    """Pairing, ecosystem unpair, and fabric backup/restore menus.

    Composed into ``Plugin`` alongside the other three mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146.
    runtime: Any
    export_bridge: Any
    exports: Any
    bridge_process: Any  # written here too — _bridge_restore_control() assigns it
    server_process: Any
    pluginPrefs: Any
    logger: Any

    # ------------------------------------------------------------------
    # Pairing and fabric management (PRD §6, BRIDGE_PROTOCOL §3.7-§3.9)
    # ------------------------------------------------------------------
    def _pairing_client(self, errors, field: str):
        """The bridge client for a pairing action, or ``None`` with ``errors`` set.

        ``connected`` rather than ``attached``, for the same §1.1 reason the
        recovery menus use it: the node answers ``get_pairing`` while refusing to
        serve endpoints, and a user whose bridge is in that state still needs to
        be able to see and manage their pairings.

        The message names the real precondition, which is not obvious: the client
        exists only while something is exported (XG5), so "pair the bridge" is
        genuinely unreachable until the user has exported a device. That is XAC2's
        ordering, not an accident — a bridge with no accessories is nothing worth
        pairing, and Apple Home would show an empty one.
        """
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        if client is None or not client.connected:
            exported = len(self.exports) if self.exports is not None else 0
            why = ("Export at least one device in 'Manage Matter Exports…' first — the bridge only "
                   "runs while something is exported." if not exported else
                   "The bridge node is not answering; see the log for what its own error log says.")
            self.logger.warning("Matter bridge: cannot reach the bridge node for pairing. %s", why)
            errors[field] = "Not connected to the bridge node — see the log."
            return None
        return client

    def menuPairMatterBridge(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Open a pairing window and put the codes where the user can read them.

        **Why this is "open a window" and not "show the code" (PRD §6).** A Matter
        commissioning passcode is not durable: the moment the first ecosystem
        commissions, the basic window closes and the original code stops working.
        Every ecosystem after that needs an *enhanced* window with a freshly
        derived code (§3.8), so there is no such thing as "the" pairing code to
        display.

        **Why the event log.** Indigo dialogs have no dynamic labels and no image
        fields, so a runtime string cannot be shown in the dialog that produced
        it. The log is this plugin's established channel for exactly that, and it
        is also the one place the codes survive being scrolled past — a window
        lasts up to 15 minutes and users do not type 11 digits first time.
        """
        errors = indigo.Dict()
        duration = self._window_duration(valuesDict, errors)
        if duration is None:
            return (False, valuesDict, errors)
        client = self._pairing_client(errors, "duration")
        if client is None:
            return (False, valuesDict, errors)
        try:
            pairing = self.runtime.submit(client.get_pairing()).result(timeout=PAIRING_READ_TIMEOUT)
        except Exception as exc:
            self.logger.error("Matter bridge: could not read the bridge node's pairing state — %s. "
                              "No pairing window was opened.", exc)
            self.logger.exception(exc)
            errors["duration"] = "Could not reach the bridge node — see the log."
            return (False, valuesDict, errors)
        # Two states already have a usable code, and opening a window in either
        # would be actively harmful: §3.8's `assertClosed` refuses a second one,
        # and on a never-commissioned node the basic window is ALREADY open with
        # the persisted originals (§3.7) — deriving a fresh enhanced code there
        # would invalidate a code the user may already be typing.
        if pairing.window_open and pairing.manual_pairing_code:
            self._log_pairing_codes(pairing.manual_pairing_code, pairing.qr_pairing_code,
                                    pairing.window_expires_at,
                                    already_open=not pairing.commissioned)
            return (True, valuesDict)
        try:
            window = self.runtime.submit(
                client.open_commissioning_window(duration)).result(timeout=WINDOW_OPEN_TIMEOUT)
        except Exception as exc:
            self.logger.error(
                "Matter bridge: opening a pairing window FAILED — %s. Nothing was changed and "
                "existing pairings are untouched. If the bridge says a window is already open, "
                "wait for it to expire (up to 15 minutes) and try again.", exc)
            self.logger.exception(exc)
            errors["duration"] = "Could not open a pairing window — see the log."
            return (False, valuesDict, errors)
        if self.export_bridge is not None:
            self.export_bridge.note_window_opened(window.window_expires_at)
        self._log_pairing_codes(window.manual_pairing_code, window.qr_pairing_code,
                                window.window_expires_at, already_open=False)
        return (True, valuesDict)

    @staticmethod
    def _window_duration(values_dict, errors) -> Optional[int]:
        """Validate the duration field against §3.8's 180-900s band.

        Rejected in the dialog rather than clamped silently by the node: the
        number is how long the user has to walk to another room with a phone, and
        being given 900 when they asked for 60 is a difference they should be
        told about while the dialog is still open.
        """
        raw = str((values_dict or {}).get("duration", "") or "").strip()
        if not raw:
            return bridge_protocol.DEFAULT_WINDOW_SECONDS
        try:
            duration = int(raw)
        except (TypeError, ValueError):
            errors["duration"] = "Enter a whole number of seconds between 180 and 900."
            return None
        if not 180 <= duration <= 900:
            errors["duration"] = "Matter allows 180 to 900 seconds (3 to 15 minutes)."
            return None
        return duration

    def _log_pairing_codes(self, manual: Optional[str], qr: Optional[str],
                           expires_at: Optional[str], *, already_open: bool) -> None:
        """Write the codes, the expiry and the QR page URL to the event log."""
        when = f" It expires at {expires_at}." if expires_at else ""
        opening = ("The bridge has never been paired, so it is ALREADY advertising with its "
                   "original code — no new window was opened." if already_open else
                   "A pairing window is now open.")
        self.logger.info(
            "Matter export — %s%s\n"
            "    Manual pairing code: %s\n"
            "    QR payload: %s\n"
            "    QR code page: %s\n"
            "Add the bridge in your ecosystem's app as you would any Matter accessory, and type "
            "the manual code if it asks for one. Expect an 'uncertified accessory' warning — that "
            "is normal for a bridge like this one; choose Add Anyway.\n"
            "SECURITY: while this window is open, anyone who can reach that page (or read this "
            "code) can add your exported Indigo devices to THEIR Apple Home, Alexa or Google "
            "account. The page is served by the Indigo Web Server, which asks for a password only "
            "if you have switched authentication on — turn it on before using this over anything "
            "but a network you trust, and do not share the URL.",
            opening, when, manual or "(none)", qr or "(none)", self._pairing_page_url())

    def _pairing_page_url(self) -> str:
        """The IWS URL of the QR page (Actions.xml ``pairing``).

        ``getWebServerURL`` picks the reflector, then the Bonjour name, then
        localhost — so this is reachable from the phone the user is holding
        whenever a reflector or a ``.local`` name exists, which is the case the
        page is FOR. A failure falls back to the loopback default rather than
        omitting the line: a wrong-host URL a user can edit beats no URL.
        """
        base = "http://localhost:8176"
        try:
            base = str(indigo.server.getWebServerURL() or base)
        except Exception as exc:
            self.logger.debug("could not resolve the Indigo web server URL (%s)", exc)
        return f"{base}/message/{self._export_plugin_id()}/pairing/"  # pylint: disable=no-member  # ExportDialogMixin

    @degrades_to_list_error
    def getBridgeFabrics(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002
        # pylint: disable=redefined-builtin, unused-argument
        """Picker rows for the unpair menu: one per commissioned ecosystem.

        Built from the fabric set the bridge already reported (attach, then every
        §5 ``fabrics_changed``), never from a fresh WS round trip: a dynamic list
        callback runs on the Indigo UI's thread while the dialog is opening, and
        blocking it on a node that may be down would hang the dialog rather than
        render an empty one. That is a deliberate trade and the reason
        :meth:`menuUnpairEcosystem` re-reads the set *after* it acts, and the
        reason §3.9 now reports whether it removed anything: the list can be
        stale, so nothing downstream may assume it is not.

        **The first row is always "(select an ecosystem)".** Indigo pre-selects
        row one, so without it the dialog opened with a real ecosystem already
        chosen on a menu whose Execute button removes it — every other picker in
        this plugin (device, node, backup) leads with a no-selection row for
        exactly this reason, and the one destructive picker did not.
        """
        bridge = self.export_bridge
        fabrics = bridge.fabrics if bridge is not None else None
        if not fabrics:
            # None and [] are different facts, and both are un-pickable, but
            # only one of them should read as "you are not paired".
            return [(NO_SELECTION_ID,
                     "(no paired ecosystems)" if fabrics == []
                     else "(not connected to the bridge node)")]
        return [(NO_SELECTION_ID, "(select an ecosystem)")] + [
            (str(fabric.fabric_index), bridge_health.describe_fabric(fabric))
            for fabric in fabrics]

    def menuUnpairEcosystem(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """§3.9 — remove one ecosystem's fabric from the bridge.

        Two gates, like the reset menu, because the outcome is the same size for
        the ecosystem being removed: every accessory Indigo exports disappears
        from it, with the names, rooms and automations built on them.
        """
        errors = indigo.Dict()
        selected = str(valuesDict.get("fabric", "") or "")
        if not selected or selected == NO_SELECTION_ID:
            errors["fabric"] = "Select an ecosystem to unpair."
            return (False, valuesDict, errors)
        try:
            fabric_index = int(selected)
        except (TypeError, ValueError):
            errors["fabric"] = "Invalid selection."
            return (False, valuesDict, errors)
        # pylint: disable=no-member  # ExportDialogMixin._truthy via MRO (issue #146)
        if not self._truthy(valuesDict.get("confirm")) \
                or not self._truthy(valuesDict.get("confirmAgain")):
            field = "confirm" if not self._truthy(valuesDict.get("confirm")) else "confirmAgain"
            errors[field] = "Tick BOTH boxes — this removes every exported accessory from that "\
                            "ecosystem."
            return (False, valuesDict, errors)
        # pylint: enable=no-member
        client = self._pairing_client(errors, "confirmAgain")
        if client is None:
            return (False, valuesDict, errors)
        cached_last = self._is_last_fabric(fabric_index)
        try:
            removal = self.runtime.submit(
                client.remove_fabric(fabric_index)).result(timeout=UNPAIR_TIMEOUT)
        except Exception as exc:
            self.logger.error("Matter bridge: unpairing ecosystem %s FAILED — %s. Pairings are "
                              "unchanged.", fabric_index, exc)
            self.logger.exception(exc)
            errors["confirmAgain"] = "Unpair failed — see the log. Nothing was changed."
            return (False, valuesDict, errors)
        # The picker is built from a CACHED fabric list, so the ecosystem may
        # have unpaired itself since — which the node reports as a successful
        # no-op. Re-read before saying anything, so the picker cannot keep
        # offering a ghost and the sentence below is about the real outcome.
        self._refresh_fabric_cache(client)
        if not removal.removed:
            # ⊗ This used to be indistinguishable from a real removal: the node
            # answered `{}` either way and the menu logged "has been unpaired.
            # Every accessory has been removed" over a node-side no-op.
            self.logger.warning(
                "Matter bridge: ecosystem %s was ALREADY gone from the bridge node — nothing was "
                "removed by this action, because there was nothing there to remove. It had most "
                "likely unpaired itself since this dialog was opened. The ecosystem list has been "
                "refreshed%s.", fabric_index,
                f"; {removal.remaining} pairing(s) remain" if removal.remaining is not None else "")
            return (True, valuesDict)
        # `remaining` is the node's own post-removal count and beats the cache;
        # the cache is only the fallback for a node that could not read it.
        last = cached_last if removal.remaining is None else removal.remaining == 0
        if last:
            # §3.9: matter.js factory-resets itself when the fabric set empties,
            # and the node clears its commissioning witness to match. Say what
            # that actually means rather than reporting a routine removal, because
            # the user has just reset the whole bridge without using the reset menu.
            self.logger.warning(
                "Matter bridge: ecosystem %s was the LAST one paired, so the bridge node has "
                "reset itself and is advertising for commissioning again — exactly as 'Reset "
                "Matter Bridge Pairings…' would have done. Nothing in Indigo changed. Use "
                "'Pair Matter Bridge…' to pair it again.", fabric_index)
        else:
            self.logger.warning(
                "Matter bridge: ecosystem %s has been unpaired. Every accessory Indigo exports "
                "has been removed from it; remove any leftover 'Indigo' bridge entry in that "
                "ecosystem's app by hand.", fabric_index)
        return (True, valuesDict)

    def _refresh_fabric_cache(self, client) -> None:
        """Re-read the fabric set from the node after an unpair. Never raises.

        The §5 ``fabrics_changed`` that follows a removal is asynchronous, and
        the picker is built from the cache it updates — so without this a user
        who unpairs and immediately re-opens the dialog is offered the ecosystem
        they just removed. Blocking is fine HERE (a menu Execute already blocked
        on the removal itself); it is not fine in the picker callback, which runs
        on the UI thread while the dialog opens.
        """
        bridge = self.export_bridge
        if bridge is None:
            return
        try:
            pairing = self.runtime.submit(client.get_pairing()).result(timeout=PAIRING_READ_TIMEOUT)
            bridge.note_fabrics(pairing.fabrics)
        except Exception as exc:
            # The removal itself already succeeded or was already true; failing
            # to re-read the list afterwards is not worth reporting as a failure.
            self.logger.debug("Matter bridge: could not refresh the ecosystem list (%s)", exc)

    def _is_last_fabric(self, fabric_index: int) -> bool:
        """Whether removing ``fabric_index`` empties the fabric set.

        Read BEFORE the removal, from the set the bridge last reported: the §5
        ``fabrics_changed`` that follows arrives asynchronously, so asking
        afterwards races it. Unknown (nothing reported yet) reads as False —
        the message it selects is only the difference between two warnings.
        """
        bridge = self.export_bridge
        fabrics = bridge.fabrics if bridge is not None else None
        if not fabrics:
            return False
        return [f.fabric_index for f in fabrics] == [fabric_index]

    def _resolve_storage_path(self) -> str:
        """Storage dir path in BOTH managed and manual modes.

        In managed mode ``self.server_process`` already knows it. In manual mode
        we construct a throwaway ``ServerProcess`` purely to read ``storage_path``
        — its ``__init__`` writes no plist and runs no launchctl, so this is a
        side-effect-free path lookup.
        """
        if self.server_process is not None:
            return self.server_process.storage_path
        # See plugin.py's `_server_prefs` docstring: constructing a ServerProcess
        # from raw prefs skips the local-mode pinning — always go through it.
        # Tests wanting to intercept THIS construction must patch
        # pairing_menu_mixin.ServerProcess — patching plugin.ServerProcess misses it.
        return ServerProcess(self._server_prefs(), self.logger).storage_path  # pylint: disable=no-member

    def _bridge_storage_path(self) -> str:
        """The **export** bridge node's storage dir — sibling of the controller's.

        Derived rather than read from a pref, and derived by the module that also
        hands it to the agent as ``--storage-path`` (E7), so the directory this
        backs up and the directory the node actually writes cannot disagree. The
        path is the PRD §4.3 default (``…/com.simons-plugins.indigo-matter/
        bridge-node``), which is also ``bridge-node/src/config.ts``'s
        ``DEFAULT_STORAGE_PATH``.
        """
        return bridge_agent.bridge_storage_path(self._resolve_storage_path())

    def _bridge_restore_control(self) -> Optional["bridge_agent.BridgeProcess"]:
        """The bridge's ``stop()``/``start()`` seam for :func:`fabric_backup.restore_backup`.

        ``stop()``/``start()``, NOT ``uninstall()``: restore wants the node
        back in exactly its prior lifecycle state, plist included, so that a
        reboot afterwards behaves exactly as it would have before the
        restore. ``uninstall()`` is ``menuStopBridgeNode``'s primitive
        (:meth:`_stop_bridge_agent`) and answers "make sure a reboot cannot
        bring this back" — the wrong question here, and why THIS path leaves
        the XAC1 latch alone (see the call site in ``menuRestoreFabricBackup``).

        Built from CURRENT prefs when no agent object exists yet, exactly as
        ``menuStopBridgeNode`` does — this must work in a session that never
        exported anything. Construction writes nothing and runs no launchctl.
        """
        if self.bridge_process is not None:
            return self.bridge_process
        try:
            self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.warning(
                "Matter bridge: could not build a control for the bridge node (%s), so any "
                "bridge files in this backup will be reported and skipped. The controller "
                "fabric restores normally.", exc)
            return None
        return self.bridge_process

    @staticmethod
    def _human_size(num_bytes: int) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    def menuExportFabricBackup(self):  # noqa: N802
        # menuItem has no ConfigUI/valuesDict, so outcome can only surface via the
        # log — make both success and failure unmistakable there. create_backup
        # already prunes (no duplicate prune here) and validates its own output.
        storage_path = None
        try:
            storage_path = self._resolve_storage_path()
            archive = fabric_backup.create_backup(
                storage_path, now=datetime.now(timezone.utc), logger=self.logger,
                # PRD-indigo-matter-export §4.3: the bridge node's storage is
                # backed up alongside the controller's. Losing it costs every
                # ecosystem pairing AND every exported accessory's identity.
                bridge_storage_path=self._bridge_storage_path(),
            )
            size = self._human_size(os.path.getsize(archive))
            self.logger.info(
                "Fabric backup complete: %s (%s). This is a best-effort live snapshot — "
                "matter-server was NOT stopped. Backups live in %s.",
                archive, size, fabric_backup.backups_dir_for(storage_path),
            )
        except FileNotFoundError as exc:
            # storage dir missing or empty — there is no fabric to back up.
            self.logger.error(
                "Fabric backup FAILED — no fabric to back up, nothing was written: %s", exc,
            )
        except Exception as exc:
            self.logger.error("Fabric backup FAILED — nothing was written: %s", exc)
            self.logger.exception(exc)

    def getFabricBackups(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002
        """List-callback populating the restore picker (newest first)."""
        try:
            storage_path = self._resolve_storage_path()
        except Exception as exc:
            self.logger.exception(exc)
            return []
        options = []
        for entry in fabric_backup.list_backups(storage_path):
            when = datetime.fromtimestamp(entry["mtime"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            label = f"{entry['filename']} — {self._human_size(entry['size_bytes'])} — {when}"
            options.append((entry["path"], label))
        return options

    def menuRestoreFabricBackup(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        errors = indigo.Dict()
        # Restore must stop/start matter-server, which the plugin can only do in
        # managed mode. Externally-managed (run.sh / manual) servers must be
        # stopped by the user by hand.
        if self.server_process is None:
            msg = ("LaunchAgent management is off — the plugin cannot stop matter-server. "
                   "Stop matter-server yourself, unzip the chosen backup over the storage dir, "
                   "then restart it. Refusing to restore automatically.")
            self.logger.warning(msg)
            errors["backup"] = "Turn on 'Manage LaunchAgent', or restore by hand (see log)."
            return (False, valuesDict, errors)

        selected = valuesDict.get("backup", "")
        if not selected:
            errors["backup"] = "Select a backup to restore."
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm — restore replaces the current fabric."
            return (False, valuesDict, errors)

        try:
            storage_path = self._resolve_storage_path()
            # The bridge control/path are passed in, but the XAC1 latch (which
            # session started the bridge agent) is NEVER touched here in either
            # direction: alive+latched -> stop/start -> unchanged, correct;
            # alive+unlatched -> unchanged, because setting it would arm a
            # future bootout of an agent whose lifecycle this session does not
            # own; stopped-by-us + restart-failed -> the latch is left EXACTLY
            # as it was: if this session had started the agent it stays set
            # (so the next empty-export transition still uninstalls the
            # RunAtLoad plist); if a prior session's agent, it stays unset —
            # no worse than before the restore (XAC1/XG5).
            # restore_backup uses stop()/start(), never uninstall(), so the
            # plist survives and the latch's claim stays true throughout.
            result = fabric_backup.restore_backup(
                selected, storage_path, self.server_process,
                now=datetime.now(timezone.utc), logger=self.logger,
                bridge_storage_path=self._bridge_storage_path(),
                bridge_control=self._bridge_restore_control(),
            )
            # restore_backup only returns on success: the server was stopped, the
            # fabric was swapped, the restored dir is non-empty, and start()
            # returned True. Be honest — matter-server is RESTARTING, the node
            # count is not yet known; point the user at the real signal instead of
            # logging a likely-stale count and pretending it is confirmation.
            self.logger.info(
                "Fabric restored from %s; previous fabric preserved at %s. matter-server is "
                "restarting — watch the log for 'reconciled N node(s)' to confirm the devices "
                "came back.",
                result["restored_from"], result["moved_aside_to"],
            )
            if result["bridge_restored"]:
                if result["bridge_started"] is False:
                    self.logger.error(
                        "The controller fabric restored, but the Matter bridge node did not "
                        "come back up. %s", self._bridge_agent_diagnosis() or  # pylint: disable=no-member
                        "Check the bridge node's error log.")
                else:
                    # There may have been no pre-existing bridge dir to preserve —
                    # say nothing rather than "preserved at None".
                    preserved = (
                        f" (previous copy preserved at {result['bridge_moved_aside_to']})"
                        if result["bridge_moved_aside_to"] else "")
                    self.logger.info(
                        "The Matter bridge node's storage was restored too%s%s. It now holds "
                        "the accessory identities and endpoint numbers as of that backup — if "
                        "a paired ecosystem has changed since, the bridge REPORTS endpoint-map "
                        "drift in the log and renumbers nothing.", preserved,
                        " and the node has been restarted" if result["bridge_started"] else "")
            return (True, valuesDict)
        except Exception as exc:
            # restore_backup rolled back and preserved the original fabric (or
            # aborted before touching it). Surface the failure in the UI dialog —
            # never report success when the underlying op failed.
            self.logger.error("Fabric restore FAILED: %s", exc)
            self.logger.exception(exc)
            errors["backup"] = "Restore failed — see the log. Your existing fabric was preserved."
            return (False, valuesDict, errors)
