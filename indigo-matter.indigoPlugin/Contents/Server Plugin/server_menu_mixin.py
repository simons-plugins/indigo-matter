"""matter-server menus (install/restart + manual commission/decommission with
their pickers), export-bridge recovery, and the bridge node's
LaunchAgent (PRD §4.2/§4.4, BRIDGE_PROTOCOL §3.10/§3.11, E7 — PRD-indigo-matter-export
§4.2/XG5/XAC1). Three originally separate sections in one mixin because they share
one construction rule (ServerProcess only ever from ``self._server_prefs()``) and
one install thread. See issue #146.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import CancelledError as FuturesCancelledError
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import bridge_agent
import bridge_client            # bridge_client.rebuild_timeout_for
from commission_jobs import node_id_to_str
from http_handlers import MatterUnavailable
from plugin_constants import FACTORY_RESET_TIMEOUT, server_location
from server_process import ServerProcess


class ServerMenuMixin:
    """matter-server install/restart, manual commission/decommission-device,
    export-bridge recovery, and bridge-node LaunchAgent menus.

    Composed into ``Plugin`` alongside the other three mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146.
    server_process: Any    # read and written here (_install_matter_server, menuRestartMatterServer)
    bridge_process: Any    # read and written here — also written by PairingMenuMixin._bridge_restore_control
    _install_thread: Any   # read and written here
    _stopping: Any
    # ⚠ Written here (menuRestartMatterServer, _install_matter_server) and read
    # by plugin.py's `_on_server_unreachable` — the tightest lifecycle coupling
    # in the split. Still one plain attribute on one object; nobody should
    # "tidy" it into a local (issue #146).
    _restart_expected_until: Any
    exports: Any
    export_bridge: Any
    jobs: Any
    device_sync: Any
    runtime: Any
    pluginPrefs: Any
    logger: Any

    # ------------------------------------------------------------------
    # Menu items
    # ------------------------------------------------------------------
    def menuInstallMatterServer(self):  # noqa: N802
        """Install/update the matter-server npm package, then pin the node used.

        Only meaningful in local (managed) mode — self-managers keep their own
        server untouched. Runs off the Indigo main thread so the UI never blocks on
        npm; progress and outcome go to the log.
        """
        if server_location(self.pluginPrefs) != "local":
            self.logger.error(
                "Install is only for local mode. Set 'is matter-server on this Mac?' "
                "to local (managed) first, or install/manage the server yourself."
            )
            return
        if self._install_thread is not None and self._install_thread.is_alive():
            self.logger.warning("matter-server install already in progress.")
            return
        self.logger.info("Starting matter-server install in the background — watch the "
                         "log for progress; this can take a minute.")
        self._install_thread = threading.Thread(
            target=self._install_matter_server, name="matter-install", daemon=True)
        self._install_thread.start()

    def _install_matter_server(self, clean: bool = False) -> None:
        try:
            # See plugin.py's `_server_prefs` docstring: constructing a ServerProcess
            # from raw prefs skips the local-mode pinning — always go through it.
            sp = self.server_process or ServerProcess(self._server_prefs(), self.logger)  # pylint: disable=no-member
            if clean:
                # "Start fresh": delete node_modules and reinstall. Stops/reaps the server
                # first and leaves the storage (fabric/pairings) intact. Used to recover a
                # matter-server that won't start after an upgrade.
                self.logger.info("Removing the installed matter-server for a clean "
                                 "reinstall (your devices/pairings are kept)…")
                if not sp.remove_package():
                    # remove_package has already said what is still there. Do NOT
                    # install over it: a clean reinstall that quietly became a
                    # plain reinstall leaves the wedge the user came here for.
                    self.logger.error(
                        "Clean reinstall ABANDONED — the old package could not be removed, so "
                        "nothing was reinstalled over it. Nothing was changed.")
                    return
            if not sp.install():
                self.logger.error(
                    "Install/update of the Matter controller (matter-server) did not "
                    "complete — see the error above. The server was not (re)installed; "
                    "retry when resolved."
                )
                return
            if self._stopping:  # plugin is tearing down — don't mutate its state
                return
            # Pin the exact node used so the LaunchAgent runs the same one forever —
            # this is what keeps install-node == run-node and avoids ABI crash-loops.
            self.pluginPrefs["nodeBinDir"] = sp.resolved_bin_dir
            indigo.server.savePluginPrefs()
            self.server_process = ServerProcess(self._server_prefs(), self.logger)  # pylint: disable=no-member
            self.server_process.ensure_installed()
            # Restart matter-server onto the just-installed version — otherwise the
            # newly-installed package sits on disk while the OLD process keeps running
            # (a running LaunchAgent doesn't pick up new files). This is what makes the
            # menu action a one-click, no-CLI update.
            self._expect_restart()  # pylint: disable=no-member
            if not self.server_process.restart():
                # Don't claim success: the new version may not be running.
                self._restart_expected_until = 0.0  # let the crash diagnostic work
                self.logger.error(
                    "matter-server was installed and pinned to node at %s, but the "
                    "restart onto the new version FAILED — the old version may still be "
                    "running. Use Plugins ▸ Matter ▸ Restart the Matter controller, or "
                    "reload the plugin.", sp.resolved_bin_dir,
                )
                return
            self.logger.info(
                "matter-server installed, pinned to node at %s, and restarting onto the "
                "new version — it reconnects automatically.", sp.resolved_bin_dir,
            )
        except Exception as exc:  # noqa: BLE001
            # npm may have succeeded and only the pin/activate step failed — say so, so
            # the user doesn't reinstall in circles chasing a downstream problem.
            self.logger.exception(exc)
            self.logger.error(
                "matter-server install did not complete after the npm step — the "
                "package may be installed but the node was not pinned and the server "
                "was not (re)started. See the trace above, then retry Plugins ▸ Matter "
                "▸ Install/update the Matter controller (matter-server)."
            )

    def menuReinstallMatterServerClean(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Menu callback: delete matter-server and reinstall it fresh (keeps devices).

        The "blow it all away and start over" recovery when matter-server won't start
        after an upgrade (e.g. a wedged install or a stray process). Removes
        ~/indigo-matter/node_modules and reinstalls; the fabric/storage is left intact so
        commissioned devices survive. Runs in the background like the plain install.
        """
        errors = indigo.Dict()
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm the reinstall."
            return (False, valuesDict, errors)
        if server_location(self.pluginPrefs) != "local":
            errors["confirm"] = ("Reinstall is only for local (managed) mode. Set 'is "
                                 "matter-server on this Mac?' to local first.")
            return (False, valuesDict, errors)
        if self._install_thread is not None and self._install_thread.is_alive():
            errors["confirm"] = "An install is already in progress — wait for it to finish."
            return (False, valuesDict, errors)
        self.logger.info("Starting a clean matter-server reinstall in the background — "
                         "watch the log for progress; this can take a minute.")
        self._install_thread = threading.Thread(
            target=self._install_matter_server, kwargs={"clean": True},
            name="matter-reinstall", daemon=True)
        self._install_thread.start()
        return (True, valuesDict)

    def menuRestartMatterServer(self):  # noqa: N802
        if self.server_process is None:
            self.logger.warning("LaunchAgent management is off; start matter-server manually")
            return
        # Rebuild from CURRENT prefs first. ServerProcess snapshots prefs at construction
        # and restart() bootstraps the plist *as it is on disk*, which only
        # ensure_installed() regenerates — so without this, a setting changed since
        # startup (notably the attestation flag) is silently NOT applied and this menu
        # still logs success.
        self.server_process = ServerProcess(self._server_prefs(), self.logger)  # pylint: disable=no-member
        self._expect_restart()  # expected outage, not a crash  # pylint: disable=no-member
        try:
            # None = preflight failed (plist torn down, nothing to restart);
            # True = it already reloaded launchd, so a restart() here would stop and
            # start the server a SECOND time for nothing — two outages, every device's
            # session dropped twice; False = job left running, so we do the restart.
            reloaded = self.server_process.ensure_installed()
            restarted = True if reloaded else (
                False if reloaded is None else self.server_process.restart()
            )
        except Exception as exc:  # noqa: BLE001
            # Unguarded, this would escape with the expected-restart window still armed,
            # suppressing the crash diagnostic for 30s while the server is down.
            self._restart_expected_until = 0.0
            self.logger.exception(exc)
            return
        if restarted:
            self.logger.info("matter-server restart requested")
        elif reloaded is None:
            self._restart_expected_until = 0.0
            self.logger.error(
                "matter-server cannot be restarted — see the error above. Its LaunchAgent "
                "was removed to stop a crash-loop; fix the cause, then reload the plugin."
            )
        else:
            self._restart_expected_until = 0.0  # restart failed — don't suppress the diagnostic
            self.logger.error(
                "matter-server restart failed; check ~/Library/Logs/indigo-matter/matter-server.err.log"
            )

    def menuShowMatterServerLogs(self):  # noqa: N802
        self.logger.info("matter-server log: ~/Library/Logs/indigo-matter/matter-server.log")

    def menuCommissionDeviceManually(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        if self.jobs is None:
            # Surface WHY OK did nothing — a bare (False, valuesDict) leaves the
            # dialog open with no explanation at all.
            self.logger.warning("manual commission requested before the plugin finished starting")
            errors = indigo.Dict()
            errors["setupCode"] = "Plugin still starting — try again in a moment."
            return (False, valuesDict, errors)
        status, body = self.jobs.create_job({
            "setupCode": valuesDict.get("setupCode", ""),
            "suggestedName": valuesDict.get("suggestedName", "Matter Device"),
            # The picker's value is a folder id; map it back to the folder NAME and
            # pass it as suggestedRoom, which device_sync resolves to that folder
            # (the same path Domio's room uses). "0"/unknown → no folder (root).
            "suggestedRoom": self._folder_name_for(valuesDict.get("folder")),
        })
        self.logger.info("manual commission → %s %s", status, body)
        return (status in (202, 409), valuesDict)

    def getDeviceFolders(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the folder picker on the manual-commission menu.

        Options are (folderId, folderName) with a leading "0" → no folder (the
        device-list root). The id MUST be a non-empty string — Indigo rejects an
        empty list id with "UI dynamic list function returned illegal ID string",
        which silently drops the option — so the no-folder sentinel is "0" (folder
        id 0 == no folder), never "". menuCommissionDeviceManually maps the chosen
        id back to the folder NAME for suggestedRoom."""
        options = [("0", "(no folder)")]
        try:
            for folder in indigo.devices.folders:
                options.append((str(folder.id), folder.name))
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to no-folder only
            self.logger.exception(exc)
        return options

    def _folder_name_for(self, folder_id):
        """Resolve the folder picker's selected id (string) to the folder NAME.

        "0", empty, or an unknown/stale id → None (commission to the device-list
        root). Never raises — an unresolvable folder must not fail the commission."""
        if not folder_id or folder_id == "0":
            return None
        try:
            fid = int(folder_id)
            for folder in indigo.devices.folders:
                if folder.id == fid:
                    return folder.name
            # Parses fine but matches nothing — e.g. folder deleted between the
            # picker rendering and submit. Benign (device lands at root), but leave
            # a trail rather than silently dropping the selection.
            self.logger.debug("folder id %r not found, commissioning at root", folder_id)
        except Exception as exc:  # noqa: BLE001 - degrade to no folder, never fail the commission
            self.logger.warning("folder id %r not resolvable, commissioning without a folder: %s", folder_id, exc)
        return None

    def getMatterNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the decommission picker (one entry per node)."""
        if self.device_sync is None:
            return []
        try:
            options = []
            for node_id, names in self.device_sync.list_nodes():
                label = ", ".join(names) if names else "(no Indigo devices)"
                options.append((str(node_id), f"{label} — node {node_id_to_str(node_id)}"))
            return options
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to an empty picker
            self.logger.exception(exc)
            return []

    def menuDecommissionDevice(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Menu callback: decommission the selected node.

        Returns ``(False, valuesDict, errors)`` to keep the dialog open with a
        field error, ``(True, valuesDict)`` only when the node was fully
        removed (fabric AND Indigo devices) — partial outcomes are dialog
        errors so they can't masquerade as success.
        """
        errors = indigo.Dict()
        selected = valuesDict.get("node", "")
        if not selected:
            errors["node"] = "Select a device to decommission."
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm removal from Indigo."
            return (False, valuesDict, errors)
        try:
            node_id = int(selected)
        except (TypeError, ValueError):
            errors["node"] = "Invalid selection."
            return (False, valuesDict, errors)
        try:
            result = self._decommission_sync(node_id)  # pylint: disable=no-member  # HttpApiMixin
        except MatterUnavailable as exc:
            self.logger.error("decommission %s failed — matter-server unavailable: %s",
                              node_id_to_str(node_id), exc)
            # A timeout does NOT cancel the in-flight coroutine — the removal may
            # still complete in the background, so don't claim nothing happened.
            errors["node"] = ("matter-server did not respond — see the log. The removal may "
                              "still complete in the background; check the device before retrying.")
            return (False, valuesDict, errors)
        except (Exception, FuturesCancelledError) as exc:  # CancelledError is BaseException on 3.10+
            self.logger.error("decommission %s failed: %s", node_id_to_str(node_id), exc)
            self.logger.exception(exc)
            errors["node"] = "Decommission failed — see the Indigo event log."
            return (False, valuesDict, errors)
        if result is None:
            errors["node"] = "Unknown node — nothing was removed."
            return (False, valuesDict, errors)
        if result["fabricRemoved"]:
            self.logger.info(
                "Decommissioned Matter node %s: fabric removed, Indigo device(s) deleted: %s",
                result["nodeId"], result["removedIndigoDeviceIds"] or "none",
            )
            return (True, valuesDict)
        # remove_node failed (usually: device offline) — matter-server most likely
        # still has the node (any remove_node failure is treated as not-removed),
        # so the next reconcile (plugin restart or matter-server reconnect) will
        # recreate the Indigo devices we just deleted. Surface that in the dialog —
        # never report success when the underlying op only half-happened.
        self.logger.warning(
            "Decommission of node %s incomplete: Indigo device(s) %s deleted but the "
            "fabric removal failed (device offline?). The node is still commissioned in "
            "matter-server and its devices will reappear at the next reconcile — retry "
            "once the device is reachable.",
            result["nodeId"], result["removedIndigoDeviceIds"] or "none",
        )
        errors["node"] = ("Device unreachable — removed from Indigo, but it is still commissioned "
                          "in matter-server and will reappear at the next reconcile (plugin restart "
                          "or reconnect). Retry once the device is powered and reachable.")
        return (False, valuesDict, errors)

    # ------------------------------------------------------------------
    # Export-bridge recovery menus (BRIDGE_PROTOCOL §3.10/§3.11)
    # ------------------------------------------------------------------
    def _recovery_client(self, errors, field: str):
        """The bridge client, or ``None`` with ``errors`` filled in.

        Both recovery commands need a live socket, and the state they exist to
        fix is exactly the one where the plugin holds the connection open
        UN-attached (§1.1 recovery). So `connected`, not `attached`, is the
        right gate — requiring an attach would make the rebuild unreachable in
        the only situation that needs it.
        """
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        if client is None or not client.connected:
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
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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

    # ------------------------------------------------------------------
    # The Matter bridge node's LaunchAgent (E7 — PRD §4.2, XG5, XAC1)
    # ------------------------------------------------------------------
    def _start_bridge_agent(self) -> None:
        """Install (if needed) and start the bridge node's LaunchAgent.

        The ``agent_start`` seam. Called by :class:`export_bridge.ExportBridge`
        on the empty→non-empty allow-list transition, on whichever Indigo thread
        made that change — never on the loop. Blocking, but only by a couple of
        ``launchctl`` calls; ``install`` is deliberately NOT attempted here (npm
        takes a minute and this can run from ``deviceDeleted``), so a missing
        package surfaces as ``ensure_installed``'s actionable preflight error
        naming the install menu.

        Rebuilt from current prefs on every call rather than cached: the ports
        and the mDNS interface are prefs, ``ensure_installed`` only reloads
        launchd when the resulting plist actually differs, and a stale
        ``BridgeProcess`` would write yesterday's ports while reporting success.
        """
        self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        agent = self.bridge_process
        if agent.ensure_installed() is None:
            # Preflight failed; the plist has been torn down and the reason
            # logged. Nothing to start, and starting would only crash-loop.
            return
        # ``ensure_installed() is not None`` is NOT the process being up. It is
        # False for "the current definition was already loaded and healthy" AND
        # for "bootout succeeded but neither bootstrap nor load did" AND for "the
        # job is loaded with a pid line we could not parse" — and the middle one
        # was reported here as "bridge node LaunchAgent is running". Ask launchd.
        state = agent.run_state()
        if state == agent.RUNNING:
            self.logger.info("Matter bridge: bridge node LaunchAgent is running (protocol port %s, "
                             "Matter port %s)", agent.ws_port, agent.matter_port)
        elif state == agent.UNKNOWN:
            # A pid line we could not parse. It may well be serving; what we must
            # not do is assert either way.
            self.logger.info(
                "Matter bridge: the bridge node's LaunchAgent is loaded (protocol port %s, Matter "
                "port %s); launchd did not report a readable pid, so whether the process is up "
                "will show as the plugin connects — or fails to.", agent.ws_port, agent.matter_port)
        else:
            self.logger.error(
                "Matter bridge: the bridge node's LaunchAgent %s. Exported accessories will be "
                "unavailable until it does. %s",
                "did not start" if state == agent.LOADED_NOT_RUNNING
                else "could not be loaded by launchd",
                self._bridge_agent_diagnosis())

    def _stop_bridge_agent(self) -> None:
        """Stop the bridge agent and REMOVE its plist. The storage is untouched.

        The ``agent_stop`` seam, called only once there is genuinely nothing to
        serve.

        **``uninstall()`` rather than ``stop()``, and that is a correction.**
        ``stop()`` boots the job out and keeps the plist, which reads as a
        thrifty choice until you notice the plist carries ``RunAtLoad: True``:
        at the next login launchd started an *unpaired* bridge node with an
        EMPTY allow-list, advertising on the Matter port, that this plugin never
        started and — because ``_agent_started`` is false in a session that
        never brought it up (XAC1) — would never stop. XG5's guarantee is that a
        fresh or emptied install runs no bridge process, and a guarantee that
        does not survive a reboot is not one. Re-deriving the plist costs one
        ``ensure_installed`` on the next export.

        The storage dir — every ecosystem pairing plus the endpoint-number
        witness — is never touched by either (PRD §5.4).
        """
        agent = self.bridge_process
        if agent is None:
            return
        was_loaded = agent.is_running()      # "is there a job on the books"
        agent.uninstall()                    # bootout + remove the plist
        if agent.is_running() or os.path.exists(agent.plist_path):
            # ⊗ The silent branch. stop() returning False used to say nothing at
            # all, and `_agent_started` had already been cleared, so nothing
            # retried: the node kept serving every paired ecosystem with the log
            # asserting the opposite by omission.
            self.logger.warning(
                "Matter bridge: nothing is exported, but the bridge node's LaunchAgent could not "
                "be %s (%s). It keeps running and serving every paired ecosystem, and NOTHING "
                "retries this on its own — reload the plugin, or run 'launchctl bootout "
                "gui/$(id -u)/%s' and delete that file by hand.",
                "stopped" if agent.is_running() else "removed", agent.plist_path,
                bridge_agent.LABEL)
            return
        if was_loaded:
            self.logger.info(
                "Matter bridge: nothing is exported — the bridge node has been stopped and its "
                "LaunchAgent removed, so a restart of this Mac cannot bring it back. Its pairings "
                "are kept.")
        else:
            # The two Falses `stop()` conflated: this one is "there was no job",
            # which is not a failure and must not be reported as one.
            self.logger.debug(
                "Matter bridge: nothing is exported and no bridge node LaunchAgent was loaded; "
                "any plist has been removed. Pairings are kept.")

    def _bridge_agent_diagnosis(self) -> Optional[str]:
        """Why is the bridge node not answering? The ``agent_diagnose`` seam.

        Reads the agent's own error log, which is the only place the real cause
        appears: a Matter port already bound by another stack (PRD §7), a package
        that was never installed, an ABI mismatch. The socket sees "connection
        refused" for all of them.

        **Read-only.** It deliberately does not restart anything. launchd already
        owns respawn via ``KeepAlive``, the loaded-but-dead revival lives in
        ``ensure_installed``, and a diagnostic that quietly bounced the agent on
        every failure streak would turn a crash-loop into a crash-loop nobody can
        read the log of.
        """
        agent = self.bridge_process
        if agent is None:
            return ("The bridge node's LaunchAgent has not been started by this plugin session — "
                    "export at least one device, or reload the plugin.")
        # ⊗ Asked FIRST, and it was not asked at all. preflight() holds the
        # actual fact — is the node interpreter there, is the package installed —
        # while the old code guessed at it from an empty error log and then said
        # "checked {project_dir}", which it had not looked at. A missing package
        # is also the case where the error log is empty *for the right reason*:
        # launchd never got far enough to write one.
        problem = agent.preflight()
        if problem:
            return f"The bridge node cannot start: {problem}"
        tail = agent.tail_error_log()
        if tail:
            # NOT "recent". The file is appended to and never truncated, so the
            # last 20 lines can be from a crash-loop days ago that has since been
            # fixed — naming the file is what lets the user check the timestamps.
            return (f"The last lines of {os.path.join(agent.log_dir, bridge_agent.BRIDGE_ERR_LOG)} "
                    f"(appended to since the bridge was first started, so these may be old):\n"
                    f"{tail}")
        return (f"The {bridge_agent.BRIDGE_PACKAGE} package is installed and its error log "
                f"({os.path.join(agent.log_dir, bridge_agent.BRIDGE_ERR_LOG)}) is empty, so the "
                f"node is failing without saying why — check that nothing else on this Mac holds "
                f"Matter port {agent.matter_port} or protocol port {agent.ws_port}.")

    def menuInstallBridgeNode(self):  # noqa: N802
        """Install/update the ``indigo-matter-bridge`` npm package.

        The export-side twin of ``menuInstallMatterServer``, and a sibling rather
        than an extension of it: the two agents are separately versioned, and a
        user recovering a wedged bridge must not also be made to reinstall a
        controller that is working (or the reverse). They share the install
        thread because ``~/indigo-matter`` is one npm root and two concurrent
        ``npm install``s into it corrupt each other.
        """
        self._run_bridge_install(clean=False)

    def menuReinstallBridgeNodeClean(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Remove the bridge package and install it fresh (the controller's twin).

        The controller has had this exit since 2026.7; the bridge shipped without
        it, so a user whose bridge install was wedged had a menu that reinstalled
        *over* the wedge and no way to clear it. ``remove_package`` is per-package
        since E7, which is what makes this safe to offer at all — it used to
        rmtree the shared ``node_modules`` and take the controller with it.

        Pairings are untouched: they live in the storage dir, which nothing in
        the install path goes near.
        """
        errors = indigo.Dict()
        if not self._truthy(valuesDict.get("confirm")):  # pylint: disable=no-member  # ExportDialogMixin
            errors["confirm"] = "Tick the box to confirm."
            return (False, valuesDict, errors)
        if not self._run_bridge_install(clean=True):
            errors["confirm"] = "An npm install is already running — wait for it to finish."
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def _run_bridge_install(self, *, clean: bool) -> bool:
        """Start the background bridge install. False if one is already running."""
        if self._install_thread is not None and self._install_thread.is_alive():
            self.logger.warning("An npm install is already in progress — wait for it to finish.")
            return False
        self.logger.info(
            "%s the Matter bridge node in the background — watch the log for progress; "
            "this can take a minute.",
            "Removing and reinstalling" if clean else "Installing")
        self._install_thread = threading.Thread(
            target=self._install_bridge_node, args=(clean,),
            name="matter-bridge-install", daemon=True)
        self._install_thread.start()
        return True

    def menuStopBridgeNode(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Stop the bridge node and remove its LaunchAgent, by hand.

        The controller has "Restart the Matter controller"; the bridge had nothing at
        all, because it is started and stopped by the allow-list. That leaves one
        state with no UI: a user who disables the plugin (or whose plugin dies
        mid-session) has a node still running and still serving every paired
        ecosystem, and the only lever is ``launchctl``. Exporting nothing is not
        that lever — it needs the plugin to be running to notice.

        Exports are NOT changed. The next export starts the node again, which is
        exactly XG5 and is why this is safe to hand a user: the worst outcome is
        a bridge that comes back.
        """
        errors = indigo.Dict()
        if not self._truthy(valuesDict.get("confirm")):  # pylint: disable=no-member  # ExportDialogMixin
            errors["confirm"] = "Tick the box to confirm."
            return (False, valuesDict, errors)
        # Built from CURRENT prefs rather than reused: this must work in a
        # session that never started the agent, which is the whole point of it.
        self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        self._stop_bridge_agent()
        if self.export_bridge is not None:
            # The plugin no longer has an agent it started, so the XAC1 latch
            # must not go on claiming it does.
            self.export_bridge.note_agent_stopped()
        return (True, valuesDict)

    def _install_bridge_node(self, clean: bool = False) -> None:
        """npm-install the bridge package, then restart it if anything is exported.

        The restart is conditional on there being something to export, which is
        the difference from the controller's install: bringing the agent up
        because a package was updated would violate XG5 on an install with an
        empty allow-list, and leave a bridge process running for nothing.

        **``ensure_installed()`` before ``restart()``, and its absence was the
        first-run dead end.** The bridge's plist is written by exactly one place
        — ``_start_bridge_agent`` — and on a machine where the package has never
        been installed that place cannot get past its own preflight, so it writes
        no plist and (correctly) tears any stale one down. The user's route out
        of that is this menu; it then went install() → restart(), restart found
        no plist, and printed "nothing to restart. Fix the problem reported
        above" (there was no problem above — the install had just SUCCEEDED)
        followed by "the restart FAILED — the old version may still be running"
        (nothing was running). Two wrong messages, no bridge, and the only real
        remedy — write the plist now that the package exists — never attempted.
        """
        try:
            agent = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
            if clean and not agent.remove_package():
                # remove_package has said what is still there. Installing over a
                # wedged install is what the clean variant exists to avoid.
                self.logger.error(
                    "Clean reinstall of the Matter bridge ABANDONED — the old package "
                    "could not be removed, so nothing was reinstalled over it.")
                return
            if not agent.install():
                self.logger.error(
                    "Install/update of the Matter bridge did not complete — see the error "
                    "above. Nothing was changed; retry when resolved.")
                return
            if self._stopping:  # plugin is tearing down — don't mutate its state
                return
            self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
            if self.exports is None or not len(self.exports):
                self.logger.info(
                    "Matter bridge installed. It is NOT being started: nothing is exported "
                    "yet, and the bridge only runs while the export list is non-empty. Add a "
                    "device in 'Manage Matter Exports…' and it will start itself.")
                return
            # Write (or refresh) the plist first. On the first-run path there is
            # none — this is where it comes from — and `ensure_installed` returning
            # True means launchd has already bootstrapped the NEW files, so there
            # is nothing left for restart() to do and bouncing again would be a
            # second gratuitous outage.
            applied = self.bridge_process.ensure_installed()
            if applied is None:
                self.logger.error(
                    "The Matter bridge was installed, but its LaunchAgent could not be "
                    "written — see the reason above. The package is on disk; fix that and reload "
                    "the plugin.")
                return
            # A running LaunchAgent does not pick up new files on disk, so a job
            # left alone by ensure_installed is still executing the OLD version.
            if applied is False and not self.bridge_process.restart():
                self.logger.error(
                    "The Matter bridge was installed but the restart onto the new version "
                    "FAILED — the old version may still be running. Check %s.",
                    os.path.join(self.bridge_process.log_dir, bridge_agent.BRIDGE_ERR_LOG))
                return
            if self._stopping:
                # Second check, deliberately: ensure_installed()/restart() are
                # subprocess work that can outlast shutdown()'s 5s thread join,
                # and revive_after_install is the one caller of start() that
                # can genuinely race teardown — it would bootstrap launchctl
                # and then log a scary "could not schedule bridge client run
                # loop" over a plugin that is simply exiting.
                return
            # #154: a client HALTED on version skew is not the retry_now() case
            # below — it declines the poke by design (a halt is fail-closed) and
            # nothing revives it on its own, so the reinstall that was SUPPOSED
            # to fix it left the user with no route back except a plugin reload.
            # Tried first, and only ever replaces a client actually halted for
            # that reason — see `revive_after_install`'s own reason gate.
            revived = self.export_bridge is not None and self.export_bridge.revive_after_install()
            if revived:
                self.logger.info("Matter bridge installed and restarted onto the new "
                                 "version — the halted connection has been replaced; "
                                 "reconnecting now.")
            else:
                # Cuts the reconnect backoff short (issue #135): it grew to its
                # 30s ceiling while the package was missing, and without this
                # the user watches out the rest of that delay right after a
                # success message. `poked` is truthful, not assumed: no bridge,
                # no client, or a declining client (halted/closing/#154) all
                # mean the poke never reached a run loop, so the log must not
                # claim it is reconnecting.
                poked = self.export_bridge is not None and self.export_bridge.retry_now()
                if poked:
                    self.logger.info("Matter bridge installed and restarted onto the new "
                                     "version — reconnecting now.")
                else:
                    self.logger.info("Matter bridge installed and restarted onto the new "
                                     "version — reload the plugin to reconnect.")
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
            self.logger.error(
                "Install of the Matter bridge did not complete after the npm step — the "
                "package may be installed but the agent was not restarted. See the trace above, "
                "then retry Plugins ▸ Matter ▸ Install/update the Matter bridge.")
