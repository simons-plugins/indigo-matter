"""matter-server install/restart/logs, manual commission/decommission (with
their pickers), sharing an already-commissioned node with a second ecosystem
(issue #210), and node-device recovery for device_sync's synthetic
``matterNode`` devices (issue #204).

**Why a separate mixin from the export-recovery and bridge-agent bands it used
to share a file with (issue #146 split, one file → three).** Issue #146 only
requires that every XML-named callback live somewhere Indigo can resolve as a
flat attribute on the ``Plugin`` class — it does not require the somewheres to
be few. The pre-split ``server_menu_mixin.py`` gathered six unrelated bands
under one shared reason (below); this file keeps the three that are actually
about the **inbound** controller — the matter-server process, and the
Indigo-fabric nodes it manages — while ``export_recovery_menu_mixin.py`` and
``bridge_agent_menu_mixin.py`` take the **outbound** export bridge's two
bands. Node-device recovery (:meth:`MatterServerMenuMixin.menuRecreateNodeDevices`)
sits here rather than with its physical neighbours in the pre-split file for
the same inbound/outbound reason: it is about ``device_sync``'s synthetic
node devices, not about anything exported.

**The construction-rule/install-thread argument for one file no longer holds,
and here is why, precisely.** ``docs/ARCHITECTURE.md``'s original row justified
keeping matter-server install/restart, export recovery, and the bridge
LaunchAgent together because they "share one construction rule (``ServerProcess``
only ever from ``Plugin._server_prefs()``) and one install thread". Both are
still true — ``self._install_thread`` and ``self._server_prefs()`` are
:class:`Plugin` instance state, reached by ``self.<attr>``/``self.<method>()``
identically from a mixin no matter which file defines it. A shared install
thread constrains what two *concurrent* installs may do to
``~/indigo-matter``'s npm root; it says nothing about which *file* the code
that starts one lives in. Splitting the file changes zero of that — it is not
the reason to keep the three bands in one module, only a fact about
``Plugin`` that would be true of a fourth split just as much as a first.

See issue #146.
"""
from __future__ import annotations

import threading
from concurrent.futures import CancelledError as FuturesCancelledError
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import indigo  # provided by the Indigo runtime

import protocol
from commission_jobs import fabric_counts, node_id_to_str
from http_handlers import MatterUnavailable
from plugin_constants import (
    NODE_TOMBSTONES_PREF,
    NO_SELECTION_ID,
    NO_SELECTION_LABEL,
    RECONCILE_TIMEOUT,
    SHARE_WINDOW_TIMEOUT,
    degrades_to_list_error,
    server_location,
)
from protocol import Protocol
from server_process import ServerProcess

#: Shared "device unreachable" refusal (issue #210), used both by
#: ``_share_node``'s fast pre-flight (matter-server's cached ``available``
#: flag) and its ``protocol.is_node_unreachable`` branch (a live refusal from
#: the open_commissioning_window call itself) — same cause, same wording.
#: "currently" and the parenthetical acknowledge what the picker's own
#: docstring says: this flag can lag a device that just woke up, so the fast
#: refusal is a good bet, not a certainty.
_UNREACHABLE_MESSAGE = (
    "matter-server currently reports this device as unreachable — wake it or power it "
    "on and try again in a moment (the report can lag a device that just woke). "
    "Nothing was changed."
)


@dataclass(frozen=True)
class ShareWindowRequest:
    """The context :meth:`MatterServerMenuMixin._share_node` hands the transport for
    its ``open_commissioning_window`` RPC (issue #210) — both the correlation
    key a LATE response (``ws_json_client.LateResponse.context``) is matched
    back to this share attempt with, and the human-readable note an
    un-awaited request's log line shows. Mirrors ``commission_jobs.
    CommissionRequest``'s #23 role for the commission RPC.
    """
    node_id: int
    duration: int

    def __str__(self) -> str:
        return f"share window for node {node_id_to_str(self.node_id)}"


class MatterServerMenuMixin:
    """matter-server install/restart/logs, manual commission/decommission-device
    (with their pickers), sharing an already-commissioned node with a second
    ecosystem (issue #210), and recovering device_sync's synthetic
    ``matterNode`` devices (issue #204).

    Composed into ``Plugin`` alongside the other six mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146.
    server_process: Any    # read and written here (_install_matter_server, menuRestartMatterServer)
    _install_thread: Any   # read and written here
    _stopping: Any
    # ⚠ Written here (menuRestartMatterServer, _install_matter_server) and read
    # by plugin.py's `_on_server_unreachable` — the tightest lifecycle coupling
    # in the split. Still one plain attribute on one object; nobody should
    # "tidy" it into a local (issue #146).
    _restart_expected_until: Any
    jobs: Any
    device_sync: Any
    runtime: Any
    matter: Any     # read here too — menuShareMatterNode/_share_node (issue #210)
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
            # #340 review: armed BEFORE ensure_installed(), not before restart()
            # below. `ensure_installed` bootouts and re-bootstraps launchd, so
            # the plist rewrite is itself what drops the socket — the same
            # ordering the bridge's install path was corrected to, and the same
            # one `menuRestartMatterServer` already uses. Armed only here, the
            # first connect failure is immediate and the second (1s later) fires
            # the full "appears to be crashing" report with a stderr tail, for a
            # restart this menu ordered.
            self._expect_restart()  # pylint: disable=no-member
            self.server_process.ensure_installed()
            # #187 review: no adopt_pending_bootstrap_verification() needed here —
            # unlike _start_bridge_agent's automatic, high-frequency rebuilds, the
            # UNCONDITIONAL restart() a few lines below always re-arms this fresh
            # instance's own #187 deadline regardless of what ensure_installed()
            # above did, so there is never an old verification left to lose.
            # Restart matter-server onto the just-installed version — otherwise the
            # newly-installed package sits on disk while the OLD process keeps running
            # (a running LaunchAgent doesn't pick up new files). This is what makes the
            # menu action a one-click, no-CLI update.
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
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: anything after npm returns — the prefs save, the
            # ServerProcess rebuild, the restart. Safe because this runs on a
            # BACKGROUND INSTALL THREAD, where an escape dies with no log at
            # all; converting it into a trace plus the half-success message is
            # strictly more than the user would otherwise get.
            #
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
            # #187 review: no adopt_pending_bootstrap_verification() needed here —
            # every branch below either bootstraps this fresh instance itself
            # (ensure_installed() returning True, or the False branch's explicit
            # restart()) or leaves nothing running at all (None), so there is
            # never an old verification on the replaced instance left to lose.
            # None = preflight failed (plist torn down, nothing to restart);
            # True = it already reloaded launchd, so a restart() here would stop and
            # start the server a SECOND time for nothing — two outages, every device's
            # session dropped twice; False = job left running, so we do the restart.
            reloaded = self.server_process.ensure_installed()
            restarted = True if reloaded else (
                False if reloaded is None else self.server_process.restart()
            )
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any ensure_installed/restart failure in a menu
            # callback. Safe because the handler disarms the window before
            # reporting — and an escape would ALSO show Indigo's raw traceback
            # dialog. Pinned by test_menu_restart_clears_window_when_ensure_installed_raises.
            #
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
        if status == 409:
            # #226: the raw 409 body is jargon on its own; create_job already
            # logs the INFO narrative for a fresh 202, so this is the 409
            # counterpart, not a duplicate of it.
            self.logger.info(
                "A commission for this setup code is already running — its "
                "result will be logged here.")
        # The wire-level acknowledgment stays available for debugging, but at
        # DEBUG — create_job's narrative line (202) and the breadcrumb above
        # (409) are what a user should see (#226).
        self.logger.debug("manual commission → %s %s", status, body)
        if status not in (202, 409):
            # #227: create_job rejected the request outright (400 invalid_setup_code
            # — the single most likely menu failure — or 503 matter_server_unreachable).
            # Demoting the wire line to DEBUG above removed the only log output for
            # this, and a bare (False, valuesDict) leaves the dialog open with no
            # on-screen explanation either — the exact invisible-failure shape #227
            # exists to eliminate. Mirrors the `self.jobs is None` branch above.
            message = body.get("message") or body
            self.logger.error("manual commission failed: %s", message)
            errors = indigo.Dict()
            errors["setupCode"] = str(message)
            return (False, valuesDict, errors)
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
        except Exception as exc:  # pylint: disable=broad-except
            # never break the dialog; degrade to no-folder only
            #
            # Absorbing: any failure enumerating indigo.devices.folders while
            # the dialog is being drawn. Safe because a raising LIST CALLBACK
            # takes the whole dialog down — degrading to the "(no folder)" row
            # keeps commissioning usable.
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
        except Exception as exc:  # pylint: disable=broad-except
            # degrade to no folder, never fail the commission
            #
            # Absorbing: a stale or unresolvable picker selection (the folder
            # deleted between render and submit is the routine case). Safe
            # because the cost is "the device lands at the device-list root",
            # never a refused commission; the WARNING below names it.
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
        except Exception as exc:  # pylint: disable=broad-except
            # never break the dialog; degrade to an empty picker
            #
            # Absorbing: any list or format failure on the UI thread. Safe
            # because the exception log keeps the evidence while the dialog
            # still opens; an escape is a raw traceback dialog.
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
        except (Exception, FuturesCancelledError) as exc:  # pylint: disable=broad-except
            # CancelledError is BaseException on 3.10+ — hence the tuple, not
            # a bare Exception, which would miss it.
            #
            # Absorbing: whatever _decommission_sync's documented "other →
            # caller handles" contract hands back. Safe because the caller IS
            # this handler: it converts the failure into a logged dialog error
            # rather than a raw traceback on Indigo's UI thread.
            self.logger.error("decommission %s failed: %s", node_id_to_str(node_id), exc)
            self.logger.exception(exc)
            errors["node"] = "Decommission failed — see the Indigo event log."
            return (False, valuesDict, errors)
        if result is None:
            errors["node"] = "Unknown node — nothing was removed."
            return (False, valuesDict, errors)
        if result["fabricRemoved"]:
            # "removed or already absent": a node matter-server does not have is
            # reported as removed on purpose (HttpApiMixin._decommission), so this
            # message must not claim we sent a RemoveFabric that never happened.
            self.logger.info(
                "Decommissioned Matter node %s: fabric entry removed or already absent, "
                "Indigo device(s) deleted: %s",
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
    # Share a Matter device with another ecosystem (issue #210)
    # ------------------------------------------------------------------
    @degrades_to_list_error
    def getShareableNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Node picker for "Share a Matter device with another ecosystem…".

        Every commissioned node is eligible — the Indigo fabric holds
        Administer on all of them, unlike an export role picker there is no
        capability gate to apply here. An offline device is NOT filtered out:
        matter-server's ``available`` flag can be stale by the time the
        dialog opens, and Execute is where that gets caught for real (fast,
        before the expensive call).

        Leads with a real, unpickable "select a device" row
        (:data:`~plugin_constants.NO_SELECTION_LABEL`) when there is
        something to pick — Indigo pre-selects row one, and Execute here
        opens a live commissioning window on whatever ends up selected, the
        same reasoning ``PairingMenuMixin.getBridgeFabrics`` gives its own
        leading row (pairing_menu_mixin.py:203-208). NEVER a WS call in a
        picker — this runs on the Indigo UI thread while the dialog is being
        drawn (pairing_menu_mixin.py:196-202); ``device_sync.list_nodes()``
        is a local reconciliation-state read, not a round trip.

        A malformed entry (e.g. ``node_id_to_str`` choking on a bad id) used
        to raise past this method's own try/except and take the whole dialog
        down with it — :func:`~plugin_constants.degrades_to_list_error` is
        the same contract every other picker in this file already gives a
        bad row.
        """
        if self.device_sync is None:
            # Distinct from "zero nodes": a picker rendered before startup
            # finished has no way to know whether anything is commissioned.
            return [(NO_SELECTION_ID, "(plugin still starting)")]
        nodes = list(self.device_sync.list_nodes())
        if not nodes:
            return [(NO_SELECTION_ID, "(no Matter devices — none commissioned yet)")]
        options = [(NO_SELECTION_ID, NO_SELECTION_LABEL)]
        options.extend(
            (str(node_id), f"{', '.join(names) if names else '(no Indigo devices)'} "
                          f"— node {node_id_to_str(node_id)}")
            for node_id, names in nodes
        )
        return options

    def menuShareMatterNode(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Open a commissioning window on an ALREADY-commissioned node so a
        second ecosystem (Apple Home, Alexa, Google, Home Assistant, …) can
        add it too (issue #210) — the reverse of the share model
        ``matter_client.commission_with_code``'s docstring describes: there,
        another ecosystem holds admin 1 and Indigo joins as admin 2 over IP;
        here, Indigo already holds admin 1 (it commissioned this node itself)
        and this menu opens the window that lets a second controller join
        without displacing it. Same multi-admin machinery Matter itself
        provides either way.

        **Why the event log, again.** Indigo dialogs have no dynamic labels,
        so the code this call derives cannot be shown in the dialog that
        produced it — the same constraint ``menuPairMatterBridge`` documents
        for the export bridge's own pairing codes.

        **Why there is no "is a window already open" pre-check.**
        matter-server 1.2.2 has no command to ask, and no way to re-read a
        code once issued — a lost code is unrecoverable, which is why every
        refusal path below says so rather than implying a retry is free.
        """
        errors = indigo.Dict()
        if self.runtime is None or self.matter is None:
            self.logger.warning("share requested before the plugin finished starting")
            errors["node"] = "Plugin still starting — try again in a moment."
            return (False, valuesDict, errors)
        selected = str(valuesDict.get("node", "") or "")
        if not selected or selected == NO_SELECTION_ID:
            errors["node"] = "Select a device to share."
            return (False, valuesDict, errors)
        try:
            node_id = int(selected)
        except (TypeError, ValueError):
            errors["node"] = "Invalid selection."
            return (False, valuesDict, errors)
        # pylint: disable=no-member  # PairingMenuMixin._window_duration via MRO (issue #146)
        duration = self._window_duration(valuesDict, errors)
        # pylint: enable=no-member
        if duration is None:
            return (False, valuesDict, errors)
        ok, message = self._share_node(node_id, duration)
        if not ok:
            errors["node"] = message
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def _share_node(self, node_id: int, duration: int) -> tuple[bool, str]:  # pylint: disable=too-many-return-statements
        """Do the actual share: fetch, refuse fast if unreachable, warn (never
        block) on tight fabric capacity, open the window, log the codes.

        Each early return below is a distinct, named refusal from the §5
        error-path table (issue #210 plan) — collapsing them into fewer exits
        would blur exactly the distinctions that table exists to keep separate.

        Shared core for :meth:`menuShareMatterNode` and ``Plugin.
        actionShareMatterNode`` (the device-action twin, issue #210) — the
        seam a future Domio "share this device" endpoint would also call.

        Returns ``(True, "")`` on success — the codes are already in the
        event log by the time this returns — or ``(False, message)`` where
        ``message`` is short enough for a dialog error field; the log carries
        the rest.
        """
        try:
            node = self._fetch_node(node_id)  # pylint: disable=no-member  # DiagnosticsMenuMixin (issue #146)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: broad ON PURPOSE as a name-avoidance device, not as a
            # net — the body immediately re-splits by type name, so connectivity
            # stays at debug and a genuine code bug still gets a traceback. The
            # distinction a narrow catch would give is preserved inside.
            #
            # _fetch_node only ever raises its own
            # MatterUnavailable (diagnostics_menu_mixin.py), caught by base class here
            # rather than importing that mixin's exception type — a mixin importing a
            # sibling mixin's name is exactly the back-import issue #146 removed
            # (see diagnostics_menu_mixin.MatterUnavailable's own docstring).
            if type(exc).__name__ == "MatterUnavailable":
                self.logger.debug("share: node %s fetch unavailable — %s",
                                  node_id_to_str(node_id), exc)
            else:
                # Anything else escaping _fetch_node is a code bug there, not a
                # connectivity problem — a traceback is what a bug report needs.
                self.logger.error("share: node %s fetch raised an unexpected %s — %s",
                                  node_id_to_str(node_id), type(exc).__name__, exc)
                self.logger.exception(exc)
            return False, f"matter-server did not answer — {exc}. Nothing was changed."
        if node is None:
            return False, ("matter-server does not know this node — it may have been "
                          "decommissioned. Reopen this dialog.")
        if not node.available:
            # Refuse FAST, before the expensive open_commissioning_window call:
            # matter-server's own ``available`` flag already answered the
            # question a timeout would otherwise spend up to a minute finding out.
            # Acknowledged rather than treated as certain — the picker's own
            # docstring argues this same flag can be stale, and Execute must
            # not contradict that while still taking the fast, cheap exit.
            return False, _UNREACHABLE_MESSAGE

        fabric_line = self._fabric_warning_line(node, node_id)

        try:
            window = self.runtime.submit(
                self.matter.open_commissioning_window(
                    node_id, duration=duration, context=ShareWindowRequest(node_id, duration))
            ).result(timeout=SHARE_WINDOW_TIMEOUT)
        except FuturesTimeoutError:
            self.logger.warning(
                "share: node %s — matter-server did not answer opening a commissioning "
                "window within %.0fs", node_id_to_str(node_id), SHARE_WINDOW_TIMEOUT)
            return False, ("matter-server did not answer in time. A window may still have "
                          "opened; if it did, its code is lost — wait for it to expire "
                          "before retrying.")
        except (ConnectionError, RuntimeError) as exc:
            self.logger.warning("share: node %s — not connected to matter-server (%s)",
                               node_id_to_str(node_id), exc)
            return False, "Not connected to matter-server — see the log."
        except protocol.ProtocolError as exc:
            self.logger.warning(
                "share: node %s — matter-server refused opening a commissioning window "
                "(code %s: %s)", node_id_to_str(node_id), exc.code, exc.details)
            if protocol.is_node_not_exists(exc):
                return False, ("matter-server does not know this node — it may have been "
                              "decommissioned. Reopen this dialog.")
            if protocol.is_node_unreachable(exc):
                return False, _UNREACHABLE_MESSAGE
            # Codes 8/9/100 (InvalidArguments/InvalidCommand/IcdMultiAdmin) and every
            # other unnamed one land here too — the same code protocol.py's own error
            # table says is not reliably distinguishable from "a window is already
            # open" (0 UnknownError, 7 SDKStackError), so the message hedges rather
            # than asserting a single cause for what could be up to ~10 different ones.
            return False, (
                f"matter-server refused this (error {exc.code}) — a window may already be "
                "open on this device; wait up to 15 minutes and try again. The previous "
                "window's code cannot be recovered. See the log.")
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: anything the named rungs above (timeout, connection,
            # protocol) did not claim — the last rung of issue #210's error
            # ladder. Safe because a menu callback must return a dialog error
            # rather than a raw traceback, and the half-opened-window warning
            # below still has to reach the user.
            self.logger.error("share: opening a commissioning window on node %s FAILED — %s",
                              node_id_to_str(node_id), exc)
            self.logger.exception(exc)
            return False, f"Opening a sharing window failed — {exc}. See the log."

        try:
            codes = protocol.parse_commissioning_window(window)
        except ValueError as exc:
            self.logger.error(
                "share: node %s's open_commissioning_window result had an unexpected shape "
                "(%s) — raw payload: %r", node_id_to_str(node_id), exc, window)
            return False, ("matter-server's answer had an unexpected shape — see the log. A "
                          "window may have opened anyway, but its code is lost.")

        self._log_node_share_codes(node_id, codes, duration, fabric_line)
        return True, ""

    def _fabric_warning_line(self, node, node_id) -> str:
        """§3.2-style fabric-capacity line for the share log (issue #210).

        Never blocking — only a warning when free slots would drop below 2,
        because an Apple Home pairing alone uses two (Apple Home + Apple
        Keychain, docs/MATTER.md). Reconstructed from the already-fetched
        :class:`~matter_model.NodeInfo` rather than a second ``get_node`` round
        trip — ``commission_jobs.fabric_counts`` reads the raw wire attribute
        shape, so its keys are rebuilt from ``node.attributes`` (already
        parsed) via :meth:`Protocol.attr_key`.
        """
        raw_attrs = {Protocol.attr_key(ep, cl, attr): value
                    for (ep, cl, attr), value in node.attributes.items()}
        supported, commissioned = fabric_counts({"attributes": raw_attrs})
        if supported is None or commissioned is None:
            return "fabric slot count unavailable (older firmware, or a partial read)"
        free = supported - commissioned
        line = f"{free} of {supported} fabric slot(s) free before this share"
        if free < 2:
            self.logger.warning(
                "share: node %s has only %d of %d fabric slot(s) free — an Apple Home "
                "pairing alone uses TWO (Apple Home + Apple Keychain, docs/MATTER.md), so "
                "this may be the last ecosystem that fits. Proceeding anyway.",
                node_id_to_str(node_id), free, supported)
        return line

    def _log_node_share_codes(self, node_id, window, duration: int, fabric_line: str,
                              *, prefix: str = "") -> None:
        """Write the share codes to the event log — the node-flavoured sibling
        of :meth:`PairingMenuMixin._log_pairing_codes` (pairing_menu_mixin.py:
        153-173), same reasoning (no dynamic dialog labels), adapted rather
        than copied: this is one node the Indigo fabric already administers,
        not the whole exported bridge, and matter-server reports no expiry
        timestamp the way the export bridge's own node does — the expiry
        below is computed from ``duration``, not read, hence "approximately".

        ``prefix`` (issue #210's late-answer path,
        :meth:`_note_late_share_response`) escalates this to a WARNING and
        prepends the given text — a late answer means the caller has ALREADY
        told the user the code was lost, which deserves more visibility than
        the ordinary info-level confirmation.
        """
        expires = (datetime.now(timezone.utc) + timedelta(seconds=duration)).strftime("%Y-%m-%d %H:%M UTC")
        log = self.logger.warning if prefix else self.logger.info
        log(
            "%sMatter share — a %ds commissioning window is now open on node %s.\n"
            "    Manual pairing code: %s\n"
            "    QR payload: %s\n"
            "    Expires approximately %s (matter-server does not report an exact expiry).\n"
            "    %s.\n"
            "Add it in your ecosystem's app as you would any Matter accessory. A certified "
            "retail device should be accepted without warnings; if the ecosystem shows an "
            "'uncertified accessory' prompt, the device's own attestation is non-production "
            "(development hardware) — that prompt is about the device, not about Indigo or "
            "this share.\n"
            "SECURITY: while this window is open, this passcode grants administrator access "
            "to a device you own — anyone who can read this code can add it to THEIR Apple "
            "Home, Alexa or Google account. Do not share it beyond the ecosystem you intend "
            "to add.",
            prefix, duration, node_id_to_str(node_id), window.manual_code, window.qr_code or "(none)",
            expires, fabric_line,
        )

    def _note_late_share_response(self, late) -> None:
        """Fold a late matter-server answer to an ``open_commissioning_window``
        RPC this plugin already gave up waiting on (issue #210) — the share
        counterpart of ``commission_jobs.CommissionJobs.note_late_response``'s
        #23 handling of the commission RPC. ``late`` is a
        ``ws_json_client.LateResponse``, delivered to every ``on_late_response``
        consumer (``Plugin._on_late_matter_response``); ``late.context`` names
        this as ours only when it is our own :class:`ShareWindowRequest`.

        Without this, matter-server answering after
        ``matter_client.SHARE_WINDOW_RPC_TIMEOUT`` (a sleepy Thread device)
        meant the code carrying the ONLY chance to share that device was
        silently dropped into ``WsJsonClient._log_unmatched``'s payload-free
        DEBUG line — the dialog had already told the user the code was lost,
        so there was nowhere else for it to land.

        A late ERROR is logged at INFO, not WARNING: it confirms no window
        opened, which is a relief, not a fresh problem — the WARNING already
        fired (or is about to, on the RPC-timeout path) is enough.
        """
        if not isinstance(late.context, ShareWindowRequest):
            return
        node_id = late.context.node_id
        if late.error is not None:
            code, details = late.error
            self.logger.info(
                "share: matter-server's late answer for node %s is a refusal — no window "
                "opened; code %r (%s)", node_id_to_str(node_id), code, details)
            return
        try:
            codes = protocol.parse_commissioning_window(late.result)
        except ValueError as exc:
            self.logger.error(
                "share: node %s's late open_commissioning_window answer had an unexpected "
                "shape (%s) — raw payload: %r", node_id_to_str(node_id), exc, late.result)
            return
        self._log_node_share_codes(
            node_id, codes, late.context.duration,
            "(fabric slot count not re-checked for a late answer)",
            prefix="matter-server answered late — the window DID open after all; codes "
                  "follow. ")

    # ------------------------------------------------------------------
    # Node-device recovery (issue #204) — device_sync's synthetic matterNode
    # devices left behind by a hand-deleted device, not the export side,
    # despite living among export-recovery code in the pre-split file
    # ------------------------------------------------------------------
    def menuRecreateNodeDevices(self):  # noqa: N802
        """The only way back after a matterNode device was deleted by hand
        (issue #204, ADR-0008): device_sync deliberately does NOT recreate a
        node device it has been told to leave deleted, so undoing that has to
        be a deliberate action too. No ConfigUI/valuesDict — same "outcome
        via the log only" shape as menuExportFabricBackup — because there is
        nothing to pick: every tombstoned node is cleared in one go.

        Clearing first and reconciling second means a reconcile failure still
        leaves the tombstones cleared — the next successful reconnect (the
        normal, unattended reconcile path) picks up where this left off,
        rather than silently discarding the user's request.

        ``_resync`` (``Plugin._resync``) swallows every exception itself and
        logs "resync incomplete" rather than raising (issue #204 review, fix
        B), so the ``except`` below only ever catches ``runtime.submit``/
        ``.result`` failing outright (timeout, submission failure) — never a
        real reconcile error. That means this handler must not claim success
        unconditionally past that point: it can honestly say a reconcile was
        *requested*, not that devices *were* recreated.
        """
        tombstones = getattr(self.device_sync, "node_tombstones", None)
        if tombstones is None:
            self.logger.info("Matter: no node-device tombstones to clear.")
            return
        count = tombstones.clear_all()
        if count == 0:
            self.logger.info(
                "Matter: no node-device tombstones to clear — nothing to recreate.")
            return
        try:
            # pylint: disable=no-member  # Plugin._resync (issue #146)
            self.runtime.submit(self._resync()).result(timeout=RECONCILE_TIMEOUT)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: submit/result plumbing failures ONLY. _resync swallows
            # every real reconcile error itself and logs "resync incomplete"
            # (issue #204 review, fix B), so nothing reaching here is a
            # reconcile failure. Safe because the tombstones were already
            # cleared, which is what makes "recreated on the next reconnect"
            # honest rather than a claim this handler cannot verify.
            self.logger.error(
                "Matter: cleared %d node-device tombstone(s), but the reconcile that "
                "recreates them FAILED — %s. They will be recreated on the next successful "
                "reconnect instead.", count, exc)
            self.logger.exception(exc)
            return
        self.logger.info(
            "Matter: cleared %d node-device tombstone(s) and requested a reconcile. If the "
            "device(s) do not reappear, look for a 'resync incomplete' warning above — "
            "they will be recreated on the next successful reconnect either way.", count)

    def _save_node_tombstones(self, blob: str) -> None:
        """Persist the node-device tombstone set (issue #204). Same discipline
        as DiagnosticsMenuMixin._save_survey_log: written through
        self.pluginPrefs at call time (Indigo rebinds it on PluginConfig save)
        and committed via savePluginPrefs, without which it survives only
        until the plugin stops."""
        self.pluginPrefs[NODE_TOMBSTONES_PREF] = blob
        indigo.server.savePluginPrefs()
