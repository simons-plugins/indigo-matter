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
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import bridge_agent
import bridge_client            # bridge_client.rebuild_timeout_for
import export_catalog
import protocol
from bridge_protocol import parse_published_id, published_id_for
from commission_jobs import fabric_counts, node_id_to_str
from export_store import ExportEntry
from http_handlers import MatterUnavailable
from plugin_constants import (
    EXCLUDED_OPTION_PREFIX,
    EXPORT_PICKER_LIMIT,
    FACTORY_RESET_TIMEOUT,
    LIST_ERROR_OPTION,
    NODE_TOMBSTONES_PREF,
    NO_SELECTION_ID,
    NO_SELECTION_LABEL,
    READOPT_ORPHANS_TIMEOUT,
    RECONCILE_TIMEOUT,
    SHARE_WINDOW_TIMEOUT,
    TRUNCATED_OPTION,
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
    """The context :meth:`ServerMenuMixin._share_node` hands the transport for
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


class ReadoptOrphansUnavailable(Exception):
    """The §3.12 orphan read was ATTEMPTED and did not answer (issue #219).

    Distinct from "there are no orphans" and from "there is no attached client
    to ask", because only this one means *we do not know*. Collapsing the three
    into one ``None`` is what let the re-adopt execute path tell a user that
    something had re-exported their accessory when in fact the bridge node had
    simply not replied — a wrong story about their house, told from a failure
    to read.
    """


@dataclass(frozen=True)
class _MigrateSourceIdentity:
    """Duck-types as the slice of ``bridge_protocol.OrphanRecord`` that
    :meth:`ServerMenuMixin._readopt_device_row` actually reads (``role``,
    ``unique_id``) — letting the migrate device picker
    (:meth:`ServerMenuMixin.getMigrateDevices`, #246 design §3.2) reuse that
    helper verbatim instead of forking its per-device rule for a second
    identity shape. Migrating and re-adopting pick a device by the exact
    same rule; only what is being inherited differs.
    """
    role: str
    unique_id: str


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
    # #187 review: reset here too (_stop_bridge_agent, menuStopBridgeNode) so an
    # uninstall with a standing conflict cannot leave plugin.py's next tick
    # reporting it "resolved" over a rival that still holds the port.
    _last_bridge_port_conflict: Any
    exports: Any
    export_bridge: Any
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

        The row-building loop is INSIDE the same try as the ``list_nodes()``
        call: a malformed entry (e.g. ``node_id_to_str`` choking on a bad id)
        used to raise past the try/except and take the whole dialog down with
        it — degrading to :data:`~plugin_constants.LIST_ERROR_OPTION` is the
        same contract every other picker in this file already gives a bad row.
        """
        if self.device_sync is None:
            # Distinct from "zero nodes": a picker rendered before startup
            # finished has no way to know whether anything is commissioned.
            return [(NO_SELECTION_ID, "(plugin still starting)")]
        try:
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
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to an error row
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

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
        except Exception as exc:  # noqa: BLE001 - _fetch_node only ever raises its own
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
        except Exception as exc:  # noqa: BLE001
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
    # Export-bridge recovery menus (BRIDGE_PROTOCOL §3.10/§3.11)
    # ------------------------------------------------------------------
    def _recovery_client(self, errors, field: str, *, require_attached: bool = False):
        """The bridge client, or ``None`` with ``errors`` filled in.

        The §3.10/§3.11 recovery commands need a live socket, and the state
        they exist to fix is exactly the one where the plugin holds the
        connection open UN-attached (§1.1 recovery). So `connected`, not
        `attached`, is the right gate for THEM — requiring an attach would
        make the rebuild unreachable in the only situation that needs it.

        Re-adopt (issue #219) is the opposite case (PR5 design E1): its
        picker and its validation both depend on a real, answered ``attach``
        — a node only serving the §1.1 recovery trio has no orphan list to
        offer and no way to verify one — so ``require_attached=True`` demands
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

    # ------------------------------------------------------------------
    # Migrate an exported accessory… (#246 design §3, issue #246)
    # ------------------------------------------------------------------
    def getMigrateSources(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Source picker for "Migrate an exported accessory…" (#246 design §3.2).

        Unlike the re-adopt orphan picker, this needs no bridge round trip at
        all: the allow-list (``self.exports``) IS the export list, so it is
        local truth rather than something only the node's §3.12 answer can
        confirm. A row's accessory number is decoration, read the same way
        :meth:`_readopt_device_row`'s neighbour :meth:`_previous_accessory_number`
        does — best-effort from the last attached client's status, omitted
        (not guessed) when there is none.

        A source entry whose Indigo device has been DELETED is still listed.
        That is not an oversight: it is ALSO a legitimate migrate source — the
        allow-list is never auto-pruned when a device disappears (see
        :meth:`export_dialog_mixin.ExportDialogMixin._reconcile_exports`), so
        an accessory can be moved onto a live replacement without detouring
        through re-adopt at all. The row names it "(device N no longer
        exists)" in place of the device's name, since there is nothing else
        to call it.
        """
        if self.exports is None:
            return [(NO_SELECTION_ID, "(plugin still starting — re-open this dialog in a moment)")]
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
        """No-op callback on the source menu (#246 design §3.2) — see
        :meth:`readoptOrphanChanged`; the same round-trip mechanism, re-running
        ``getMigrateDevices`` below it."""
        return valuesDict

    def getMigrateDevices(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        # pylint: disable=too-many-locals  # same two-list shape as getExportCandidates
        """Device picker for "Migrate an exported accessory…" (#246 design §3.2).

        Reuses :meth:`_readopt_device_row` verbatim for the per-device rule —
        selectable when role-eligible, ● when already exported, every other
        device SHOWN with its reason, loop-guard devices absent — via
        :class:`_MigrateSourceIdentity`, a two-field stand-in that duck-types
        as the slice of ``bridge_protocol.OrphanRecord`` that helper actually
        reads (``role``, ``unique_id``). Migrating and re-adopting pick a
        device by the exact same rule; only what is being inherited differs.

        The one rule that IS specific to migrate: the source device itself is
        absent from the list, not merely excluded-with-a-reason — it is the
        row directly above in the dialog, not a target for itself.
        """
        try:
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
                    row = self._readopt_device_row(dev, source_identity, plugin_id, exported)
                    if row is None:
                        continue
                    (explained if row[0].startswith(EXCLUDED_OPTION_PREFIX)
                     else eligible).append(row)
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)  # pylint: disable=no-member
                    failures += 1
            options = eligible + explained[:EXPORT_PICKER_LIMIT]
            if len(explained) > EXPORT_PICKER_LIMIT:
                options.append(TRUNCATED_OPTION)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def _migrate_refuse(self, errors, field: str, msg: str) -> None:
        """One #246 design §3.2 SUBSTANTIVE refusal — see :meth:`_readopt_refuse`'s
        docstring for the WARNING/dialog-only split this mirrors exactly."""
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
        verdict = export_catalog.classify(dev, self._export_plugin_id())  # pylint: disable=no-member
        if isinstance(verdict, export_catalog.Excluded):
            self._migrate_refuse(errors, "migrateDevice",
                                 f"{dev.name} cannot be exported: {verdict.reason}")
            return None
        if source_entry.role not in verdict.eligible_roles:
            self._migrate_refuse(
                errors, "migrateDevice",
                f'"{dev.name}" cannot appear as a {export_catalog.role_label(source_entry.role)}. '
                "Migrating only works when the replacement device can take the same role as "
                "the accessory it is inheriting; export it normally instead, and it becomes a "
                "new accessory.")
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
                                  target_previous_number) -> None:  # pylint: disable=too-many-arguments
        """#246 design §3.2 step 8 — the one WARNING a successful migrate leaves
        behind. Mirrors :meth:`_log_readopt_confirmation`'s shape, but the
        promise it makes is the one migrate actually earns: the identity never
        lapsed, so no ecosystem ever processed a removal, and room, name,
        scenes and automations survive BECAUSE of that — not despite it, the
        way re-adopt's corrected wording has to qualify its own promise.
        """
        role_label = export_catalog.role_label(source_entry.role)
        label = source_entry.label_for(self._migrate_source_display_name(source_id))
        role_and_number = (f"{role_label}, accessory number {source_number}"
                           if source_number is not None else role_label)
        superseded = ""
        if target_previous_number is not None:
            clause = f" (number {target_previous_number})"
            superseded = (f' "{dev.name}"\'s own previous accessory{clause} has been removed '
                          "from your ecosystems.")
        self.logger.warning(
            'Matter export: MIGRATED accessory "%s" (%s) from Indigo device %s to "%s" '
            '(id %s). No paired ecosystem processed any change — the accessory keeps its '
            'room, its name, and every scene and automation, because nothing was removed and '
            'nothing was re-added.%s',
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
        the nudge, because both make the bridge's live status describe the
        NEW arrangement — by the time the confirmation logs, there is nothing
        left to read the OLD numbers from.
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
            options=dict(source_entry.options),
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
            # and that has not been confirmed yet. Same discipline
            # `_readopt_commit` follows: never report success over an
            # operation that has not landed.
            errors["migrateDevice"] = ("Saved, but the bridge node was not told — "
                                       "see Event Log.")
            return (False, valuesDict, errors)
        self._log_migrate_confirmation(source_id, source_entry, dev, source_number,
                                       target_previous_number)
        return (True, valuesDict)

    def menuMigrateExport(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Execute "Migrate an exported accessory…" (#246 design §3.2, issue #246).

        Same discipline as :meth:`menuReadoptExport`: every step re-verified at
        Execute time rather than trusted from the picker, each step its own
        helper so later steps can assume earlier ones already hold.
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
    # Re-adopt a Matter accessory… (§4, issue #219)
    # ------------------------------------------------------------------
    def _live_readopt_orphans(self):
        """A fresh §3.12 orphan list; ``None`` when there is no attached
        bridge client to ask at all.

        Raises :class:`ReadoptOrphansUnavailable` when a client WAS there and
        the read itself failed (no answer, a timeout, an unparseable reply).
        Three outcomes, three answers: an empty list means the node says
        nothing is left behind, ``None`` means nobody was there to ask, and the
        exception means we asked and do not know. Only the first is safe to act
        on.

        Deliberately does its OWN live read every time it is called, rather
        than one picker populating a cache the other depends on: ConfigUI
        documents no ordering guarantee for which of a dialog's
        ``dynamicReload`` lists is (re-)evaluated first on a round trip, and
        nothing here should have to assume one. `list_orphans` is read-only
        and "local and quick" (§3.12), so the repeat cost is the same class
        as the diagnostics explorer re-deriving its own endpoint/cluster
        lists on every round trip (``DiagnosticsMenuMixin.exploreNodeChanged``).
        """
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        if client is None or not client.attached:
            return None
        try:
            return self.runtime.submit(
                client.list_orphans()).result(timeout=READOPT_ORPHANS_TIMEOUT)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error(
                "Matter bridge: reading the bridge node's left-behind accessory list "
                "(list_orphans, §3.12) FAILED — %s. Nothing was changed; the re-adopt dialog "
                "cannot be trusted until the node answers again.", exc)
            self.logger.exception(exc)
            raise ReadoptOrphansUnavailable(str(exc)) from exc

    @staticmethod
    def _format_orphan_date(orphaned_at) -> str:
        """"12 Aug 2026" from an ISO-8601 stamp, or "(date unknown)" for a
        pre-PR5 orphan that has none (§4.2)."""
        if not orphaned_at:
            return "(date unknown)"
        try:
            return datetime.fromisoformat(str(orphaned_at)).strftime("%-d %b %Y")
        except (TypeError, ValueError):
            return "(date unknown)"

    def _readopt_orphan_options(self, orphans) -> list:
        """§4.2 picker rows, newest orphan first.

        An orphan with no ``orphanedAt`` (a pre-PR5 record) sorts LAST: ``""``
        is lexicographically smaller than any ISO-8601 stamp, so the same
        descending sort that puts the newest date first puts an absent one
        last, with no second pass — ties (including several absent dates)
        keep their original map order, `sorted` being stable.
        """
        if not orphans:
            return [(NO_SELECTION_ID, "(no left-behind accessories — nothing to re-adopt)")]
        options = []
        for orphan in sorted(orphans, key=lambda o: o.orphaned_at or "", reverse=True):
            if orphan.role and orphan.label:
                when = self._format_orphan_date(orphan.orphaned_at)
                options.append((orphan.unique_id,
                                f"{orphan.label} — {export_catalog.role_label(orphan.role)} — "
                                f"un-exported {when} (accessory #{orphan.number})"))
            else:
                # PR5 design E4 — a pre-2026.16.2 bare orphan: shown so the user can see
                # its number is spoken for, but it matches nothing a device
                # could be checked against, so it is refused at Execute time
                # (menuReadoptExport step 4) rather than hidden here.
                options.append((orphan.unique_id,
                                f"(accessory #{orphan.number} — no role recorded, cannot be "
                                "re-adopted)"))
        return options

    def getReadoptOrphans(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Orphan picker for "Re-adopt a Matter accessory…" (§4.2).

        Unlike every other picker in this file, there is nothing cached to
        build this from — fabrics arrive on a ``fabrics_changed`` event
        (``PairingMenuMixin.getBridgeFabrics``) and orphans have no
        equivalent push — so this reads §3.12 live. Not attached and the read
        itself failing are two different facts (only one names a remedy), and
        :meth:`_live_readopt_orphans` keeps them apart for every caller —
        ``None`` for the gate, :class:`ReadoptOrphansUnavailable` for the read
        — so this simply renders each.
        """
        try:
            orphans = self._live_readopt_orphans()
        except ReadoptOrphansUnavailable:
            return [LIST_ERROR_OPTION]
        if orphans is None:
            return [(NO_SELECTION_ID, "(not connected to the Matter bridge node — start it, "
                                       "let the plugin attach, then re-open this dialog)")]
        return self._readopt_orphan_options(orphans)

    def readoptOrphanChanged(self, valuesDict, typeId="", devId=0):  # noqa: N802, ARG002
        """No-op callback on the orphan menu (§4.1).

        Its only job is making Indigo round-trip to the plugin, which is what
        re-runs ``getReadoptDevices`` below it — the same master-detail
        cascade ``DiagnosticsMenuMixin.exploreNodeChanged`` already uses:
        ConfigUI has no per-field change event, so a menu's own
        CallbackMethod is the whole mechanism.
        """
        return valuesDict

    def _readopt_device_row(self, dev, orphan, plugin_id: str, exported) -> Optional[tuple]:
        """One §4.3 device-picker row, or ``None`` to omit it. May raise —
        the caller contains it, matching ``ExportDialogMixin._candidate_row``.

        Selectable — a plain ``str(dev.id)`` id — when the orphan's role is in
        ``eligible_roles``; marked ● when already exported (that convention's
        own device, the common "deleted, recreated, re-exported before
        noticing" case, PR5 design E5/E2).

        **Every other device is SHOWN, unselectable, WITH its reason**
        (XAC9/PRD §5.2, the same rule ``_candidate_row`` follows). All three
        used to be a bare ``return None``, and each of the three is a device a
        user has a specific expectation about — "I recreated it, why is it not
        in this list?" — which an absence answers with nothing at all:

        * not exportable (the classifier's own reason);
        * exportable, but cannot take this accessory's role — the refusal
          PR5 design owner ruling 2/E3 makes at Execute time, said here first;
        * already publishing a DIFFERENT accessory's identity, i.e. re-adopted
          onto another orphan, naming which.

        The loop guard stays ABSENT rather than excluded, exactly as XAC6
        requires of the export picker: every such device shadows one the user
        already sees.

        "A DIFFERENT orphan" is decided by whose device id the identity
        embeds, not merely by it being non-default: a device publishing under
        its OWN ``indigo-<ownId>~<generation>`` (it has changed role at some
        point, issue #240) has claimed nobody else's accessory and stays
        selectable — PR5 design E2 keeps "``indigo-<newId>``, or a generation
        of it" as the explicitly-allowed already-exported case. The identity
        compared is the EFFECTIVE one — ``published_as`` where an entry has
        one, otherwise the default derivation it publishes under — so this
        reads the same way as :meth:`_readopt_identity_claimant`'s step-7
        check rather than treating ``published_as is None`` as "claims
        nothing".
        """
        mark = "● " if dev.id in exported else ""
        verdict = export_catalog.classify(dev, plugin_id)
        if isinstance(verdict, export_catalog.Excluded):
            if verdict.reason == export_catalog.REASON_LOOP_GUARD:
                return None                      # XAC6: absent, not excluded
            return (f"{EXCLUDED_OPTION_PREFIX}{dev.id}",
                    f"{mark}{dev.name} — not exportable: {verdict.reason}")
        if orphan.role not in verdict.eligible_roles:
            return (f"{EXCLUDED_OPTION_PREFIX}{dev.id}",
                    f"{mark}{dev.name} — cannot appear as a "
                    f"{export_catalog.role_label(orphan.role)}")
        entry = self.exports.get(dev.id)
        if entry is not None:
            publishing_as = entry.published_as or published_id_for(dev.id)
            if publishing_as != orphan.unique_id:
                claimed = parse_published_id(publishing_as)
                if claimed is None or claimed.device_id != dev.id:
                    return (f"{EXCLUDED_OPTION_PREFIX}{dev.id}",
                            f"{mark}{dev.name} — already re-adopted onto accessory "
                            f"{publishing_as}")
        return (str(dev.id), f"{mark}{dev.name}")

    def getReadoptDevices(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        # pylint: disable=too-many-locals  # same two-list shape as getExportCandidates
        """Device picker for "Re-adopt a Matter accessory…" (§4.3) — see
        :meth:`_readopt_device_row` for the per-device rule.

        Selectable rows first and never truncated, the same ordering rule
        ``getExportCandidates`` uses and for the same reason: this dialog is
        the only place a re-adopt can be started, so a device buried past
        :data:`EXPORT_PICKER_LIMIT` by a house full of unselectable rows would
        be effectively unreachable. The cap therefore applies only to the
        explained-but-unpickable tail, with the usual truncation row when it
        bites. There is no filter field to narrow on (the orphan's role
        already does most of the narrowing), which is exactly why the tail is
        the half that gets capped.
        """
        try:
            if self.exports is None:
                return [(NO_SELECTION_ID,
                         "(plugin still starting — re-open this dialog in a moment)")]
            selected = str((valuesDict or {}).get("readoptOrphan", "") or "")
            if not selected or selected == NO_SELECTION_ID:
                return []
            try:
                orphans = self._live_readopt_orphans() or []
            except ReadoptOrphansUnavailable:
                # Same row the orphan picker above it is showing, for the same
                # read: a list that could not be built must not look like a
                # list of no eligible devices.
                return [LIST_ERROR_OPTION]
            orphan = next((o for o in orphans if o.unique_id == selected), None)
            if orphan is None or not orphan.role:
                return []
            exported = self.exports.ids()
            plugin_id = self._export_plugin_id()  # pylint: disable=no-member  # ExportDialogMixin
            eligible: list = []
            explained: list = []
            failures = 0
            for dev in indigo.devices:
                try:
                    row = self._readopt_device_row(dev, orphan, plugin_id, exported)
                    if row is None:
                        continue
                    (explained if row[0].startswith(EXCLUDED_OPTION_PREFIX)
                     else eligible).append(row)
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)  # pylint: disable=no-member
                    failures += 1
            options = eligible + explained[:EXPORT_PICKER_LIMIT]
            if len(explained) > EXPORT_PICKER_LIMIT:
                options.append(TRUNCATED_OPTION)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    @staticmethod
    def _previous_accessory_number(client, device_id: int) -> Optional[int]:
        """The endpoint number of ``device_id``'s accessory BEFORE a
        re-adopt, read from the client's last :class:`StatusReport`
        (attach/get_status) — the only place a still-live endpoint's number
        is cached client-side. ``None`` when there is nothing to report (the
        device was not exported, or no status has landed yet), which the
        confirmation log then omits rather than guesses.
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

    def _readopt_identity_claimant(self, unique_id: str, device_id: int) -> Optional[int]:
        """Another ``ExportStore`` entry already publishing as ``unique_id``
        (PR5 design §4.4 step 7, edge case E12) — the plugin-side half of the
        duplicate-identity guard. The node's own duplicate-``publishedAs``
        refusal at attach time (``parseEndpointSpecs``) is the backstop that
        actually matters — a hand-edited ``.indiPref`` bypasses this one.

        **Compares EFFECTIVE identities, not the stored field.**
        ``published_as`` is ``None`` on every ordinary export, and that does
        not mean "claims nothing": it means "publishes as
        ``indigo-<own device id>``". Matching the raw field made an ordinary
        export's claim invisible here, so a re-adopt of that same identity was
        allowed to write a SECOND entry publishing it — and the node refuses a
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

    def _log_readopt_confirmation(self, client, orphan, dev, device_id: int, previous) -> None:
        """PR5 design §4.5 — the one WARNING a successful re-adopt leaves behind.

        The last sentence is emitted only when ``device_id`` was already
        exported under a DIFFERENT identity than the orphan it is now
        inheriting — PR5 design E2/E5's "recreated and re-exported before noticing"
        case, whose own accessory the ``_nudge_export`` call just before this
        one has already asked the bridge to remove.

        The closing sentence states the MEASURED truth (#246 design §3.4,
        2026-08-18 jarvis validation), not the promise the old text made:
        identity, accessory number and no-duplicate continuity always hold —
        that part re-adopt genuinely restores — but room/name/scenes/
        automations do not, because Apple Home (and, presumably, every other
        ecosystem) purges them within moments of processing the removal that
        orphaned this identity in the first place, long before this dialog
        can run. "Migrate an exported accessory…" is the tool that keeps
        those, and only while the old device still exists — which by
        definition it no longer does here.
        """
        role_label = export_catalog.role_label(orphan.role)
        if orphan.device_id is not None:
            # "stopped exporting it", not "was deleted": the node records an
            # orphan whenever an identity goes away, and a deliberate
            # un-export is at least as common as a deleted device. Naming the
            # cause we did not observe is how a user ends up hunting for a
            # deletion that never happened.
            left_behind = (f"That accessory was left behind when device {orphan.device_id} "
                          "stopped exporting it.")
        else:
            left_behind = "That accessory was left behind."
        prior_identity = (previous.published_as or published_id_for(device_id)) \
            if previous is not None else None
        superseded = ""
        if prior_identity is not None and prior_identity != orphan.unique_id:
            number = self._previous_accessory_number(client, device_id)
            clause = f" (number {number})" if number is not None else ""
            superseded = (f" Device {device_id}'s own previous accessory{clause} has been "
                          "removed from your ecosystems.")
        self.logger.warning(
            'Matter export: RE-ADOPTED accessory "%s" (%s, accessory number %d) onto Indigo '
            'device "%s" (id %s). %s The accessory returns under its original identity and '
            'number, and no duplicate is created — but an ecosystem that has already processed '
            'the removal (Apple Home does within moments) will show it in the Default Room '
            'under a default name, and any scene or automation that referenced it must be '
            'rebuilt.%s',
            orphan.label, role_label, orphan.number, dev.name, device_id, left_behind, superseded)

    def _readopt_refuse(self, errors, field: str, msg: str) -> None:
        """One PR5 design §4.4 SUBSTANTIVE refusal: dialog error plus a matching
        WARNING — ``menuRebuildEndpointMap``'s convention of never reporting
        success over an operation that did not land, extended to a refusal
        that never started. Never used for a plain "tick the box"/"make a
        selection" nag — that stays dialog-only, the same split
        ``menuRebuildEndpointMap`` already draws between its silent
        confirm-tick gate and its logged M11 "nothing to rebuild" gate.
        """
        self.logger.warning("Matter bridge: re-adopt REFUSED — %s", msg)
        errors[field] = msg

    def _readopt_pick_orphan(self, valuesDict, errors):
        """The picked orphan (or "select one"), re-verified per PR5 design §4.4 steps
        3-4: a FRESHLY-fetched ``list_orphans`` (PR5 design E6 — never the
        picker's own read, which may be stale by Execute time), with both a
        role and a label (PR5 design E4 — a pre-2026.16.2 bare orphan, which
        deleted them on un-export).

        Returns the orphan, or ``None`` with ``errors`` filled in.
        """
        orphan_id = str(valuesDict.get("readoptOrphan", "") or "")
        if not orphan_id or orphan_id == NO_SELECTION_ID:
            errors["readoptOrphan"] = "Select a left-behind accessory to re-adopt."
            return None
        try:
            orphans = self._live_readopt_orphans()
        except ReadoptOrphansUnavailable:
            orphans = None
        if orphans is None:
            # NOT the PR5 design E6 story below: nothing re-exported anything, we simply
            # could not re-read the list, and step 3 exists precisely because
            # the picker's own read is not trusted at Execute time.
            self._readopt_refuse(errors, "readoptOrphan",
                                 "Could not re-check the left-behind accessory list — the bridge "
                                 "node did not answer. Nothing was changed; try again once the "
                                 "plugin has reconnected.")
            return None
        orphan = next((o for o in orphans if o.unique_id == orphan_id), None)
        if orphan is None:
            self._readopt_refuse(errors, "readoptOrphan",
                                 "That accessory is in use again — something re-exported it "
                                 "while this dialog was open. Nothing was changed.")
            return None
        if not orphan.role or not orphan.label:
            self._readopt_refuse(
                errors, "readoptOrphan",
                "That accessory has no role or name recorded — it was un-exported by a plugin "
                "version older than 2026.16.2, which deleted them. It cannot be re-adopted; "
                "export the device normally instead, and its accessory will be a new one.")
            return None
        return orphan

    def _readopt_pick_device(self, orphan, valuesDict, errors):
        # pylint: disable=too-many-return-statements  # one return per PR5 §4.4 refusal
        """The picked device (or "select one"), re-verified per PR5 design §4.4 steps
        5-6: still existing, still classifying as exportable, and still able
        to take the orphan's role (PR5 design E3, owner ruling 2 — REFUSE,
        never allow-with-warning: cross-role re-adopt is exactly the
        in-place device-type mutation issue #240 exists to eliminate, since
        the endpoint number is preserved by construction).

        Returns the device, or ``None`` with ``errors`` filled in.
        """
        selection = str(valuesDict.get("readoptDevice", "") or "")
        if not selection or selection == NO_SELECTION_ID:
            errors["readoptDevice"] = "Select the Indigo device to hand it to."
            return None
        if selection.startswith(EXCLUDED_OPTION_PREFIX):
            # An unselectable XAC9 row. Its label already carries the specific
            # reason, and re-deriving it here would either repeat the checks
            # below verbatim or — for the "already re-adopted onto another
            # orphan" row — miss it entirely, since nothing after this point
            # looks at the TARGET device's own published identity.
            self._readopt_refuse(errors, "readoptDevice",
                                 "That row is listed with the reason it cannot take this "
                                 "accessory — pick a device offered without one.")
            return None
        try:
            device_id = int(selection)
        except (TypeError, ValueError):
            # Not a user mistake — every row this picker emits is either an
            # integer device id or an `x-`-prefixed one handled above, so a
            # `valuesDict` that reaches here means the dialog and the list
            # callback disagree. Nothing in the message tells the user that,
            # so the log has to.
            self.logger.error(
                "Matter bridge: re-adopt got a device selection that is neither a device id nor "
                "an unselectable row — %r. Nothing was changed; this is a plugin fault, not a "
                "bad choice.", selection)
            errors["readoptDevice"] = "Invalid selection."
            return None
        dev = self._indigo_device(device_id)  # pylint: disable=no-member  # ExportDialogMixin
        if dev is None:
            self._readopt_refuse(errors, "readoptDevice",
                                 "That device no longer exists — refresh the list.")
            return None
        verdict = export_catalog.classify(dev, self._export_plugin_id())  # pylint: disable=no-member
        if isinstance(verdict, export_catalog.Excluded):
            self._readopt_refuse(errors, "readoptDevice",
                                 f"{dev.name} cannot be exported: {verdict.reason}")
            return None
        if orphan.role not in verdict.eligible_roles:
            self._readopt_refuse(
                errors, "readoptDevice",
                f'"{dev.name}" cannot appear as a {export_catalog.role_label(orphan.role)}. '
                "Re-adopting only works when the replacement device can take the same role as "
                "the accessory it is inheriting; export it normally instead, and it becomes a "
                "new accessory.")
            return None
        return dev

    def _readopt_commit(self, client, orphan, dev, errors, valuesDict):  # pylint: disable=too-many-arguments
        """The PR5 design §4.4 closing paragraph: one ``ExportStore.upsert`` preserving
        any existing ``name_override``/``options``, then the remove-then-add
        nudge and the PR5 design §4.5 confirmation log.
        """
        device_id = dev.id
        previous = self.exports.get(device_id)
        try:
            self.exports.upsert(ExportEntry(
                indigo_device_id=device_id,
                role=orphan.role,
                name_override=previous.name_override if previous is not None else None,
                options=dict(previous.options) if previous is not None else {},
                published_as=orphan.unique_id,
            ))
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error("Matter bridge: re-adopt FAILED to save the export list — %s", exc)
            self.logger.exception(exc)
            errors["readoptDevice"] = "FAILED to save the export list — see Event Log."
            return (False, valuesDict, errors)
        # The remove-then-add path — the device's PUBLISHED IDENTITY changed,
        # the same shape as a role change (PR5 design §4.4) even though the
        # role itself did not move here.
        told = self._nudge_export(device_id, role_changed=True)  # pylint: disable=no-member  # ExportDialogMixin
        if not told:
            # The store write stands (re-attaching WILL apply it), but the
            # PR5 design §4.5 WARNING claims every ecosystem has already kept its room —
            # and that has not happened yet. Reporting it here would be the
            # same "success over an operation that did not land" this whole
            # menu is written to avoid.
            errors["readoptDevice"] = ("Saved, but the bridge node was not told — "
                                       "see Event Log.")
            return (False, valuesDict, errors)
        self._log_readopt_confirmation(client, orphan, dev, device_id, previous)
        return (True, valuesDict)

    def menuReadoptExport(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Execute "Re-adopt a Matter accessory…" (PR5 design §4.4, issue #219).

        Validated in the exact order PR5 design §4.4 specifies, each step delegated to
        one helper so later steps can assume earlier ones already hold:
        :meth:`_readopt_pick_orphan` (steps 3-4), :meth:`_readopt_pick_device`
        (steps 5-6), the step-7 identity claim here, then
        :meth:`_readopt_commit`.
        """
        errors = indigo.Dict()
        # 1. readoptConfirm ticked.
        if not self._truthy(valuesDict.get("readoptConfirm")):  # pylint: disable=no-member  # ExportDialogMixin
            errors["readoptConfirm"] = (
                "Tick the box — re-adopting hands this accessory to a different Indigo device.")
            return (False, valuesDict, errors)
        # 2. a live, ATTACHED bridge client (PR5 design E1) — stricter than the
        #    rebuild/reset gate; see _recovery_client's own docstring.
        client = self._recovery_client(errors, "readoptConfirm", require_attached=True)
        if client is None:
            return (False, valuesDict, errors)
        # 3-4 (inside _readopt_pick_orphan) and 5-6 (inside _readopt_pick_device).
        orphan = self._readopt_pick_orphan(valuesDict, errors)
        if orphan is None:
            return (False, valuesDict, errors)
        dev = self._readopt_pick_device(orphan, valuesDict, errors)
        if dev is None:
            return (False, valuesDict, errors)
        # 7. the orphan's identity is not already claimed by another
        #    ExportStore entry (PR5 design E12).
        claimant = self._readopt_identity_claimant(orphan.unique_id, dev.id)
        if claimant is not None:
            self._readopt_refuse(
                errors, "readoptOrphan",
                f"Accessory {orphan.unique_id!r} is already claimed by Indigo device "
                f"{claimant} — nothing was changed.")
            return (False, valuesDict, errors)
        return self._readopt_commit(client, orphan, dev, errors, valuesDict)

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
        except Exception as exc:  # noqa: BLE001
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

        **#187 review: a rebuild can silently drop a still-pending post-bootstrap
        verification.** This runs on every allow-list transition, so a second
        export inside the previous one's grace window rebuilds a fresh instance
        whose OWN ``ensure_installed()`` may take the digest-match "leave alone"
        path (nothing actually changed) — no fresh bootstrap on THIS instance, and
        the replaced instance's armed deadline would otherwise simply vanish with
        it. :meth:`~launch_agent.LaunchAgent.adopt_pending_bootstrap_verification`
        carries it over; it is a no-op when this call's own ``ensure_installed()``
        DID bootstrap fresh (that arming must win).
        """
        previous = self.bridge_process
        self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        agent = self.bridge_process
        applied = agent.ensure_installed()
        if applied is None:
            # Preflight failed; the plist has been torn down and the reason
            # logged. Nothing to start, and starting would only crash-loop.
            return
        if applied is False:
            agent.adopt_pending_bootstrap_verification(previous)
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

        **#187 review: also resets ``_last_bridge_port_conflict``.** Also called
        from :meth:`menuStopBridgeNode`, which routes through here rather than
        uninstalling separately. With a conflict standing, the very next tick
        would otherwise see ``port_conflict_report()`` go quiet — because the
        managed job now has no pid, not because the rival let go of the port —
        and (pre-#187-review) announce it "resolved". Commit 1's pid-gate on
        that log line is the belt; resetting the memory of the conflict here,
        so there is nothing left to compare against, is the braces.
        """
        agent = self.bridge_process
        if agent is None:
            return
        self._last_bridge_port_conflict = None
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
