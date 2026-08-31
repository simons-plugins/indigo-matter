"""The Matter bridge node's LaunchAgent (E7 — PRD §4.2, XG5, XAC1): install,
start, stop, and diagnose the bridge process itself.

**Why a separate mixin from the matter-server and export-recovery bands it
used to share a file with (issue #146 split, one file → three).** See
:mod:`matter_server_menu_mixin`'s docstring for the full argument. This file
owns the bridge node's *process lifecycle* — the LaunchAgent plist, npm
install/reinstall, start/stop, and the ``agent_diagnose`` seam that reads its
error log — which is a different concern from ``export_recovery_menu_mixin.
py``'s reconciliation of *what the running bridge serves* (endpoint map,
export list, pairings). Both are "outbound"; this one is about the process,
that one is about its content.

See issue #146.
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

import bridge_agent

class BridgeAgentMenuMixin:
    """Install/reinstall/start/stop the Matter bridge node's LaunchAgent, and
    diagnose why it is not answering.

    Composed into ``Plugin`` alongside the other six mixins; never
    instantiated on its own and never subclasses ``indigo.PluginBase``.
    """

    # Self-attribute contract: these are created by ``Plugin.__init__``/
    # ``startup`` and stay in plugin.py. Declared here as class-level
    # annotations only (no assignment) so mixin methods resolve `self.<attr>`
    # without shadowing instance state, and so pylint's `no-member` check has
    # something to verify against. See issue #146.
    bridge_process: Any    # read and written here — also written by PairingMenuMixin._bridge_restore_control
    pluginPrefs: Any
    logger: Any
    # #187 review: reset here too (_stop_bridge_agent, menuStopBridgeNode) so an
    # uninstall with a standing conflict cannot leave plugin.py's next tick
    # reporting it "resolved" over a rival that still holds the port.
    _last_bridge_port_conflict: Any
    _install_thread: Any
    _stopping: Any
    export_bridge: Any
    exports: Any

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
        except Exception as exc:
            self.logger.exception(exc)
            self.logger.error(
                "Install of the Matter bridge did not complete after the npm step — the "
                "package may be installed but the agent was not restarted. See the trace above, "
                "then retry Plugins ▸ Matter ▸ Install/update the Matter bridge.")
