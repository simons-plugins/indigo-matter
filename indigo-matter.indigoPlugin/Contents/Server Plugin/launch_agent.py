"""Generic launchd LaunchAgent management for the plugin's node processes.

This is the machinery that used to live inside ``ServerProcess`` (which managed
exactly one agent, the matter-server controller). The plugin now needs a SECOND
agent — the Matter *bridge node* (PRD §4.2, XOQ3) — and duplicating this file
would duplicate the hard-won recovery behaviour (applied-plist digest,
loaded-but-dead revival, orphan/EADDRINUSE reaping) along with it. So the
identity of an agent — launchd label, npm package, pinned install spec, entry
point, storage dir, log filenames, port, and how its argv is built — moves into
a frozen :class:`AgentSpec`, and everything else lives here, parameterised by it.

Everything that is *policy* (which package, which flags, which port) is in the
spec; everything that is *mechanism* (npm/npx/node resolution, plist authoring,
launchctl, reaping) is in :class:`LaunchAgent`. ``server_process.ServerProcess``
is the controller's specialisation of it.

Argv construction is deliberately NOT generalised into a flags mechanism: it is
a per-agent callable on the spec. matter-server's ``--enable-test-net-dcl`` has
a must-be-last hazard (see ``server_process``) that a generic builder would
silently lose.

Node/npm toolchain resolution (npx/node path lookup, nvm, ABI-pin checks, the
shared ``.indigo-node`` install stamp) lives in :mod:`node_resolver` instead of
here — it was the one band of the six with zero outbound calls into the rest of
this class, so it is the one that could be split out without adding the
indirection the other five would (see :class:`node_resolver.NodeResolver`).

Paths and the subprocess runner are injectable so the whole module is
unit-testable without touching the real launchd or filesystem.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

# INSTALL_NODE_STAMP and expand_home are re-exported here: bridge_agent.py and
# server_process.py import both from this module (pre-dating the node-resolver split),
# and tests/test_launch_agent.py imports INSTALL_NODE_STAMP the same way.
from node_resolver import (  # pylint: disable=unused-import
    INSTALL_NODE_STAMP, MIN_NODE_VERSION, NodeResolver, _parse_node_version, expand_home,
)

DEFAULT_PROJECT_DIRNAME = "indigo-matter"   # ~/indigo-matter (npm install location)
# On macOS lsof ships ONLY in /usr/sbin — there is no /usr/bin/lsof. Invoking it by
# bare name makes the port probe depend on whatever PATH the Indigo plugin host
# inherited, and a PATH without /usr/sbin turns the probe into a silent no-op. That is
# how issue #182's second matter-server went unnoticed for four days. Absolute paths
# first, bare name last so an unusual install still works.
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof", "lsof")
# The substring matter-server logs when it cannot bind its WebSocket port. Used only as
# an ADVISORY signal (see LaunchAgent.port_conflict_report): the err log is append-only
# across restarts, so a hit may be ancient history.
EADDRINUSE_MARKER = "EADDRINUSE"
# Filenames matter.js's own storage lock leaves behind
# (node_modules/@matter/nodejs/src/fs/lock-utils.ts). matter.pid holds "<pid> <token>";
# matter.lock is the lock file itself. Both live together in a storage directory — see
# LaunchAgent.clear_stale_storage_locks() for why we look in more than one such directory.
MATTER_LOCK_FILENAME = "matter.lock"
MATTER_PID_FILENAME = "matter.pid"
# Slack on the "process started after matter.pid was written" signal, in seconds.
# The age probe is `ps -o etime=`, which prints WHOLE seconds truncated DOWN, and we
# subtract that age from a time.time() read taken after the ps subprocess has returned
# — both errors push the computed start time LATER, by up to a second each. A genuinely
# live server writes matter.pid well inside that margin (on jarvis matter.js acquires
# the lock ~30ms after the process's first log line), so a bare `started_at > mtime`
# comparison can accuse a healthy owner and delete the lock out from under a running
# fabric. That is a far worse outage than the crash-loop this check exists to end, so
# the signal only fires when the process is LATER BY A MARGIN no probe error explains.
START_AFTER_LOCK_SLACK_SECONDS = 5
# How long a freshly started server is allowed to have no listener before "nothing is
# listening on our port" counts as a fault. matter-server was observed taking ~9s to
# reach its bind on jarvis; 120s is generous enough that a slow or loaded Mac never
# trips it, and short enough to still catch a genuinely headless server on the next
# plugin reload.
STARTUP_GRACE_SECONDS = 120

# LaunchAgent.post_bootstrap_verdict() outcomes (issue #187 review). A due
# verification whose port_conflict_report() answered None must be rendered into
# exactly one of these three — see that method's docstring for why the three
# cannot be conflated.
VERDICT_DEAD = "dead"
VERDICT_CONFLICT_FREE = "conflict_free"
VERDICT_PENDING = "pending"

# LaunchAgent._stale_reason_for_live_pid() outcomes (stale-lock review). Collapsing
# LIVE_PID_OURS and LIVE_PID_UNDECIDED into the same (False, "") is what let the
# `age = age or 0` mutation survive the old test suite: both looked identical to the
# caller, so an unusable probe silently read as "nothing to report" instead of "I
# could not tell". A live pid must be judged into exactly one of these three.
LIVE_PID_STALE = "stale"
LIVE_PID_OURS = "ours"
LIVE_PID_UNDECIDED = "undecided"


@dataclass(frozen=True)
class AgentSpec:
    """The identity of one launchd-managed node process.

    Everything :class:`LaunchAgent` needs to know about *which* agent it is
    managing. Frozen because an agent's identity must not drift underneath a
    loaded launchd job — a changed label or storage path mid-life would orphan
    the running process.

    :param label: launchd job label; also the plist filename stem.
    :param package: npm package name — matched in ``ps`` output when reaping
        orphans, and the directory under ``node_modules`` holding the entry.
    :param install_spec: exact-pinned ``name@version`` handed to ``npm install``.
    :param default_entry: fallback ``main`` when the package's ``package.json``
        is missing/unreadable.
    :param storage_path: the agent's resolved (absolute) storage dir. SACRED —
        created but never deleted.
    :param out_log: ``StandardOutPath`` filename inside the shared log dir.
    :param err_log: ``StandardErrorPath`` filename inside the shared log dir.
    :param argv: builds the agent's ``ProgramArguments``, given the
        :class:`LaunchAgent`. Per-agent by design (see module docstring).
    :param port: TCP port the agent listens on, for the EADDRINUSE/orphan logic.
        ``None`` means "does not listen" (or an unparseable pref) and disables
        the port-based orphan signal — storage-path matching still applies.
    :param applied_marker: filename of the applied-plist digest stamp. Defaults
        (via :attr:`applied_marker_name`) to a per-label name so two agents
        sharing one project_dir cannot clobber each other's digest and trigger
        spurious bootout/bootstrap cycles.
    :param install_menu: the EXACT wording of this agent's Install/update menu
        item. Every message that tells a user to run it interpolated
        :attr:`package` instead, producing "Plugins ▸ Matter ▸ Install/update
        indigo-matter-bridge" — a menu that does not exist (the real one is
        "Install/update the Matter bridge"). It fires on the first-run
        path, where the user is already stuck, so a menu name they cannot find
        is the difference between a fixable state and a support thread. Blank
        falls back to the old wording via :attr:`install_menu_name`, which is
        right for an agent that has no menu at all.
    :param env: extra ``EnvironmentVariables`` for the plist, as ``(name, value)``
        pairs (a tuple, so the frozen spec stays hashable). ``PATH`` is always set
        by :meth:`LaunchAgent.build_plist` and cannot be overridden here. Changing
        a spec's ``env`` changes its plist digest, so the next plugin start
        bootouts and re-bootstraps THAT agent only — see :meth:`_apply_plist`.
    """

    label: str
    package: str
    install_spec: str
    default_entry: str
    storage_path: str
    out_log: str
    err_log: str
    # Safe as a plain field ONLY because it is required: a Callable given a dataclass
    # DEFAULT lands on the class, the descriptor protocol binds it, and it would be
    # called with the spec instead of the agent. Never give this field a default.
    argv: Callable[[Any], list[str]]
    port: Optional[int] = None
    applied_marker: Optional[str] = None
    install_menu: str = ""
    env: tuple[tuple[str, str], ...] = ()

    @property
    def applied_marker_name(self) -> str:
        """Filename of this agent's applied-plist digest stamp."""
        return self.applied_marker or f".launchagent-{self.label}.sha256"

    @property
    def install_menu_name(self) -> str:
        """What to call this agent's Install/update menu item in a message."""
        return self.install_menu or f"Install/update {self.package}"


class LaunchAgent:
    """Install / control one launchd LaunchAgent described by an :class:`AgentSpec`."""

    def __init__(
        self,
        spec: AgentSpec,
        prefs: dict,
        logger: Any,
        *,
        home: Optional[str] = None,
        npx_path: Optional[str] = None,
        runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
        exists: Callable[[str], bool] = os.path.exists,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.spec = spec
        self.logger = logger
        self._run = runner
        # Injectable existence check keeps preflight() unit-testable without the FS.
        self._exists = exists
        # Injectable so reap_orphan_servers()'s TERM→KILL grace is instant in tests.
        self._sleep = sleep
        # "Currently degraded" latches for the two external probes (issue #182). Each
        # is cleared by the next successful probe so a later failure is reported again.
        self._port_probe_warned = False
        self._process_age_warned = False
        # Same discipline for the stale-lock sweep's own probes (stale-lock review):
        # each latch is cleared the next time the sweep succeeds at the thing it warns
        # about, so a later, DIFFERENT episode of the same failure is reported again
        # rather than being silenced by a warning issued weeks (or one directory) ago.
        self._pid_read_warned = False
        self._listdir_warned = False
        self._ps_unusable_for_lock_warned = False
        self._live_pid_undecided_warned = False
        # Set by _bootstrap_and_record() on a successful fresh bootstrap; cleared once
        # due_for_bootstrap_verification() has been acted on (issue #187). See that
        # method's docstring for why this exists alongside port_conflict_report().
        self._bootstrap_verify_after: Optional[float] = None
        self.home = home or os.path.expanduser("~")
        self.project_dir = os.path.join(self.home, DEFAULT_PROJECT_DIRNAME)
        # Node/npm toolchain resolution (npx/node path, nvm, ABI-pin checks) is a
        # self-contained band extracted into its own collaborator — see node_resolver.py.
        self._node = NodeResolver(
            spec, prefs, home=self.home, project_dir=self.project_dir, logger=logger,
            npx_path=npx_path, runner=runner,
        )
        # Mirrored onto the agent: build_plist()/resolved_bin_dir() (npx_path) and
        # preflight()/install() (node_path) need them too — see node_resolver.py.
        self.npx_path = self._node.npx_path
        self.node_path = self._node.node_path
        # Mirrored from the spec so callers (the pairing/backup menus, fabric backup)
        # keep reading it off the agent itself.
        self.storage_path = spec.storage_path

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def plist_path(self) -> str:
        return os.path.join(self.home, "Library", "LaunchAgents", f"{self.spec.label}.plist")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.home, "Library", "Logs", "indigo-matter")

    @property
    def resolved_bin_dir(self) -> str:
        """The node/npx bin directory this instance resolved to.

        The caller pins this into the ``nodeBinDir`` pref after install() so the node
        that RAN the install is the node that RUNS the server — the match that avoids
        native-binding ABI crash-loops.
        """
        return os.path.dirname(self.npx_path)

    def _package_dir(self) -> str:
        """Absolute path of the agent's installed npm package."""
        return os.path.join(self.project_dir, "node_modules", self.spec.package)

    def _server_entry(self) -> str:
        """Absolute path to the package main (the JS to run with node).

        Reads ``main`` from ``{project_dir}/node_modules/{package}/package.json``
        so the launch adapts automatically if the package bumps its entry path.
        Falls back to the spec's ``default_entry`` if the manifest is missing,
        unreadable, or malformed.
        """
        pkg_dir = self._package_dir()
        main = self.spec.default_entry
        manifest = os.path.join(pkg_dir, "package.json")
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            entry = data.get("main")
            if isinstance(entry, str) and entry.strip():
                main = entry
        except (OSError, ValueError):
            pass
        return os.path.join(pkg_dir, main)

    # ------------------------------------------------------------------
    # Plist
    # ------------------------------------------------------------------
    def program_arguments(self) -> list[str]:
        """The agent's ``ProgramArguments``, built by its spec's argv hook."""
        return self.spec.argv(self)

    def build_plist(self) -> bytes:
        out_log = os.path.join(self.log_dir, self.spec.out_log)
        err_log = os.path.join(self.log_dir, self.spec.err_log)
        # dirname(npx) is the resolved node bin dir (Homebrew/nvm ship node + npx
        # together). We invoke node directly because the matter-server package
        # exposes no bin; prepending this dir to launchd's restricted PATH lets the
        # spawned node find its own co-located libexec/helpers. /usr/bin:/bin stays
        # appended.
        npx_dir = os.path.dirname(self.npx_path)
        spec = {
            "Label": self.spec.label,
            "ProgramArguments": self.program_arguments(),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
            "StandardOutPath": out_log,
            "StandardErrorPath": err_log,
            "EnvironmentVariables": {**dict(self.spec.env), "PATH": f"{npx_dir}:/usr/bin:/bin"},
        }
        return plistlib.dumps(spec)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def preflight(self) -> Optional[str]:
        """Return a FATAL reason the agent can't launch, else None.

        Guards the two failures that otherwise surface only as a launchd crash-loop
        (and a bare "connection refused" at the WS client): a missing node
        interpreter, or the agent's npm package not being installed. Both are
        common for nvm users whose ``nodeBinDir`` didn't resolve to a real bin dir.
        The Node-ABI check is deliberately NOT here — see :meth:`abi_warning` (it is
        advisory, not fatal, because a stale stamp must never block a working server).
        """
        if not self._exists(self.node_path):
            return (
                f"node was not found at {self.node_path}. Set the 'Node bin "
                f"directory' plugin pref to a folder containing node/npx, or install "
                f"Node (e.g. 'brew install node'), then restart the plugin."
            )
        entry = self._server_entry()
        if not self._exists(entry):
            return (
                f"the {self.spec.package} package is not installed ({entry} is missing). "
                f"Use the plugin menu: Plugins ▸ Matter ▸ {self.spec.install_menu_name} "
                f"(or run 'npm install {self.spec.install_spec}' in {self.project_dir} "
                f"with the same node, {self.node_path}), then restart the plugin."
            )
        return None

    def abi_warning(self) -> Optional[str]:
        """Return an ADVISORY warning if node's major differs from the install stamp.

        Delegates to :class:`node_resolver.NodeResolver` — see there for why this
        is advisory, never a fatal preflight block.
        """
        return self._node.abi_warning()

    def _install_stamp_path(self) -> str:
        """Path of the ``.indigo-node`` install stamp. See :class:`node_resolver.NodeResolver`."""
        return self._node._install_stamp_path()  # pylint: disable=protected-access

    def _read_install_node_major(self) -> Optional[int]:
        """The node major the package was installed with. See :class:`node_resolver.NodeResolver`."""
        return self._node._read_install_node_major()  # pylint: disable=protected-access

    def tail_error_log(self, max_lines: int = 20) -> Optional[str]:
        """Return the last ``max_lines`` of the agent's error log, else None.

        Surfaces WHY the launchd-managed process keeps dying (module-not-found,
        native-binding ABI mismatch, a bad ``--flag``, …) where the WS client only
        sees "connection refused". Returns None if the log is absent or empty.
        """
        path = os.path.join(self.log_dir, self.spec.err_log)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except FileNotFoundError:
            return None
        except OSError as exc:
            # An existing-but-unreadable log (e.g. permissions) is distinct from "no
            # log"; log it so the caller's "may not be installed" hint isn't mistaken
            # for the whole story.
            self.logger.debug("could not read %s: %s", path, exc)
            return None
        tail = "".join(lines[-max_lines:]).strip()
        return tail or None

    def install(self, install_spec: Optional[str] = None) -> bool:
        """npm-install the agent's package with the resolved node. Idempotent.

        Installs into ``~/indigo-matter`` using the ``npm`` co-located with the node
        this instance resolved — so the package's native deps are built for the SAME
        node the LaunchAgent will run (the install/run match that avoids ABI
        crash-loops). Records that node's version for :meth:`abi_warning`. Captures
        npm's output and logs it on failure; returns True on success. Blocking —
        callers should run it off the Indigo main thread.

        ``install_spec`` defaults to the spec's pinned ``name@version``.
        """
        install_spec = install_spec or self.spec.install_spec
        npm = os.path.join(self.resolved_bin_dir, "npm")
        if not self._exists(npm):
            self.logger.error(
                "npm was not found next to node at %s. Set the 'Node bin directory' "
                "pref or install Node (e.g. 'brew install node').", self.resolved_bin_dir,
            )
            return False
        # Gate on the node version BEFORE npm — npm's engines check is advisory and
        # would otherwise install an unrunnable server. Only block when we actually know
        # the version (a too-old node), never on an unreadable one.
        current = _parse_node_version(self._node._node_version() or "")  # pylint: disable=protected-access
        if current is not None and current[:2] < MIN_NODE_VERSION:
            self.logger.error(
                "%s requires Node >= %s but the resolved node (%s) is %s. Update Node "
                "(e.g. 'brew install node') or point the 'Node bin directory' pref at a "
                "newer node, then retry.",
                install_spec, ".".join(map(str, MIN_NODE_VERSION)), self.node_path,
                ".".join(map(str, current)),
            )
            return False
        os.makedirs(self.project_dir, exist_ok=True)
        self.logger.info("Installing %s into %s (node: %s) — this can take a minute…",
                         install_spec, self.project_dir, self.node_path)
        # npm is a `#!/usr/bin/env node` script, so `node` must be on PATH — but the
        # plugin's subprocess env (under launchd) usually isn't, which fails with
        # "env: node: No such file or directory". Prepend the resolved node bin dir.
        env = dict(os.environ)
        env["PATH"] = self.resolved_bin_dir + os.pathsep + env.get("PATH", "")
        try:
            result = self._run([npm, "install", "--prefix", self.project_dir, install_spec],
                               capture_output=True, text=True, check=False, env=env)
        except OSError as exc:
            self.logger.error("%s install could not start: %s", self.spec.package, exc)
            return False
        if result is None or result.returncode != 0:
            # Combine both streams (npm splits the cause across them) and keep the
            # HEAD — npm front-loads the real error; the tail is boilerplate footer.
            if result is None:
                detail = "npm unavailable"
            else:
                detail = "\n".join(p for p in ((result.stdout or "").strip(),
                                                (result.stderr or "").strip()) if p)
            self.logger.error("%s install failed:\n%s", self.spec.package,
                              (detail or "no output")[:3000])
            return False
        self._node._record_install_node()  # pylint: disable=protected-access
        self.logger.info("%s installed.", self.spec.package)
        return True

    def _warn_on_settings(self) -> None:
        """Hook: per-agent warnings emitted on EVERY :meth:`ensure_installed`.

        Base implementation says nothing. Subclasses override to surface a standing
        hazard (see ``ServerProcess`` and the attestation-relaxing flag).
        """

    def ensure_installed(self) -> Optional[bool]:
        """Create dirs, write the plist, and load it. Idempotent.

        Runs :meth:`preflight` first. A launchd job pointing at a missing node or an
        uninstalled package can only crash-loop (``KeepAlive`` respawns it) and
        the WS client sees a bare "connection refused". So on a preflight failure:
        log an actionable error, tear down any stale plist to stop an existing
        crash-loop, and do NOT (re)write it.

        Three outcomes, because callers need to tell them apart:

          * ``None``  — preflight failed; nothing was written and any stale plist was
            REMOVED. There is no job to restart, so a caller must stop here rather
            than "restarting" a LaunchAgent that no longer exists.
          * ``True``  — launchd was (re)loaded, so the process is already running the
            plist just written. A caller wanting a restart has nothing left to do.
          * ``False`` — the current definition was already loaded and healthy, so the
            process was deliberately left untouched (it survives plugin reloads without
            dropping slow-to-re-establish device sessions).

        Also emits :meth:`_warn_on_settings` on every call.
        """
        os.makedirs(self.storage_path, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.plist_path), exist_ok=True)
        problem = self.preflight()
        if problem:
            self.logger.error("%s cannot start: %s", self.spec.package, problem)
            if self._exists(self.plist_path):
                self.uninstall()  # stop an existing crash-loop; leaves storage intact
            return None
        abi = self.abi_warning()
        if abi:
            self.logger.warning("%s: %s", self.spec.package, abi)  # advisory — do NOT block
        self._warn_on_settings()
        desired = self.build_plist()
        with open(self.plist_path, "wb") as handle:
            handle.write(desired)
        self.logger.info("Wrote LaunchAgent %s", self.plist_path)
        return self._apply_plist(desired)

    def _apply_plist(self, desired: bytes) -> bool:
        """Make launchd run the plist just written; True if it was actually (re)loaded.

        The return value lets a caller that wants a restart tell "I already restarted
        it for you" from "the running job was left alone", so it doesn't stop and
        start the server a second time for nothing — two outages instead of one, each
        dropping every device's CASE session.


        launchd caches a job's ProgramArguments at bootstrap time, so overwriting the
        plist FILE does nothing to a job that is already loaded — a plugin upgrade that
        fixes a bad argument (the pre-2026.7.1 ``--port ""`` that crash-loops with
        "Invalid integer:") leaves the OLD, broken job running until we bootout and
        re-bootstrap. We record the digest of the plist we last applied so we can tell:

          * digest matches the running job → leave it (the server survives plugin
            reloads without dropping slow-to-re-establish device sessions);
          * digest differs, or nothing recorded (upgrading from a version that never
            wrote a marker — exactly the stuck user) → the loaded job is stale, so
            bootout and re-bootstrap. This makes the first reload after upgrade
            self-heal a crash-looping ``--port ""`` job.
        """
        digest = self._digest_of(desired)
        job = self._managed_job()          # one launchctl print: loaded + pid + live args
        running = job["loaded"]
        if running and self._read_applied_digest() == digest:
            # The current definition is already loaded. Normally we leave the healthy
            # server running (survives plugin reloads without dropping device sessions) —
            # but a matching plist does NOT prove it is healthy: if an orphaned
            # process holds the storage lock, the managed job is crash-looping
            # despite the right args. Reap the orphan (never the managed job — exclude its
            # pid); only if one was actually blocking it do we force a clean restart.
            managed_pid = job["pid"]
            if managed_pid is None and job["pid_line"]:
                # A pid line we couldn't parse: the job may well be alive, and we cannot
                # tell it from an orphan — don't risk killing it.
                return False
            if managed_pid is None:
                # Loaded with NO pid line: the job is dead and launchd has decided not to
                # respawn it (#104 fault 2 — matter-server exits 0 on a fatal startup
                # error, which KeepAlive {SuccessfulExit: false} reads as a clean exit).
                # There is no healthy server to protect here, so fall through to the
                # bootout + bootstrap below, which is the only thing that revives it.
                self.logger.warning(
                    "the %s LaunchAgent is loaded but not running, and launchd "
                    "will not respawn it on its own (%s exits 0 even on a fatal "
                    "startup error such as 'listen EADDRINUSE', which its KeepAlive policy "
                    "treats as a clean exit). Restarting it now; see %s for the cause.",
                    self.spec.package, self.spec.package,
                    os.path.join(self.log_dir, self.spec.err_log),
                )
            elif self.reap_orphan_servers(exclude_pid=managed_pid) == 0:
                # Healthy and unobstructed — survive the reload untouched. A matching
                # digest proves the right plist was WRITTEN, not that the live job is
                # using it, so check the running args before declaring victory.
                #
                # Sweep for a stale storage lock too (stale-lock review, finding A).
                # A reboot-reused pid can crash-loop THIS managed job while looking
                # exactly like this branch: launchd respawns it every ~10s (KeepAlive
                # Crashed:true), and whichever attempt is alive the instant we sample
                # has the right digest and no orphan to reap — the reused pid belongs
                # to someone else's legitimate process, so reap_orphan_servers()
                # correctly leaves it alone; it just never gets reaped either. This
                # branch is exactly the natural remedy a stuck user reaches for
                # (reload the plugin), so it must not require the sample to land in
                # the much narrower window where an orphan or a dead pid line instead
                # routes through one of the branches below that already fall through
                # to the bootstrap path's sweep. Must run before we return: this
                # branch never bootstraps, so nothing else here will sweep before
                # launchd's own KeepAlive respawns the crash-looping job again.
                self.clear_stale_storage_locks()
                self._warn_on_argument_drift(job["arguments"])
                # …and a pid does not prove the job is REACHABLE (issue #182). A server
                # that lost the port race stays alive without a WebSocket listener, so
                # every signal above still reads "healthy" while the plugin talks to a
                # foreign server. Deliberately does NOT restart: when someone else owns
                # the port, restarting ours only fails again and costs every device's
                # CASE session. The value here is an accurate, actionable diagnosis —
                # a port holder that IS one of ours was already reaped by the call above.
                conflict = self.port_conflict_report(managed_pid=managed_pid)
                if conflict:
                    self.logger.error(conflict)
                return False
            # else: an orphan was starving it; fall through to a clean bootout + bootstrap.
        if running and not self._bootout():
            # A loaded job wouldn't stop — bootstrap/load below will fail on the still
            # -loaded label, so surface it rather than letting the crash-loop persist
            # silently. We still fall through in case the job was actually gone.
            self.logger.warning(
                "could not stop the existing %s job to apply new settings; "
                "the previous definition may keep running until the next plugin reload",
                self.spec.package,
            )
        # A server can outlive the LaunchAgent that started it (bootout stops only
        # the managed job), and bootout may return before the process has fully exited and
        # released the storage lock. Reap any such straggler so the fresh instance below
        # isn't killed by "Storage is locked by another process".
        self.reap_orphan_servers()
        # A dead or reassigned pid can leave matter.js's OWN lock behind (see
        # clear_stale_storage_locks) even when there is no orphan process left to reap —
        # exactly the post-reboot case. Must run before bootstrap: launchd would otherwise
        # start a process that immediately loses to a lock nobody living holds.
        self.clear_stale_storage_locks()
        # bootstrap (modern) with a load fallback for older macOS. The marker records the
        # bytes we just wrote (== on disk), so it always reflects what launchd loaded.
        if self._bootstrap_and_record(desired):
            return True
        result = self._launchctl("load", self.plist_path)
        if result is None or result.returncode != 0:
            detail = result.stderr.strip() if result is not None else "launchctl unavailable"
            self.logger.error(
                "Failed to load %s LaunchAgent (%s); the server may not be "
                "running. Start it manually or check %s",
                self.spec.package, detail, self.plist_path,
            )
            return False
        self._record_applied_digest(digest)
        return True

    def _bootstrap_and_record(self, plist_bytes: Optional[bytes] = None) -> bool:
        """Bootstrap the plist and, on success, record the digest of what launchd loaded.

        Pass ``plist_bytes`` when the caller already holds the exact bytes it wrote to
        disk (``_apply_plist``); otherwise (``restart``/``start``, which bootstrap the
        existing file) the bytes are read back from ``plist_path`` so the applied-marker
        always reflects the file launchd was told to load — never a recomputed
        :meth:`build_plist` that could have drifted from disk. Returns the bootstrap
        outcome; a best-effort marker write never changes it.

        A successful bootstrap also arms :meth:`due_for_bootstrap_verification` (issue
        #187): none of ``_apply_plist``'s fresh-bootstrap path, ``start()``, or
        ``restart()`` themselves re-check that the process they just started actually
        holds the port — a rival can win the bind race in the gap between bootstrap and
        the server's own bind (matter-server was observed taking ~9s on jarvis).
        """
        if not self._bootstrap():
            return False
        if plist_bytes is None:
            plist_bytes = self._plist_on_disk()
        if plist_bytes is not None:
            self._record_applied_digest(self._digest_of(plist_bytes))
        self._bootstrap_verify_after = time.monotonic() + STARTUP_GRACE_SECONDS
        return True

    @staticmethod
    def _digest_of(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _plist_on_disk(self) -> Optional[bytes]:
        try:
            with open(self.plist_path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def _applied_marker_path(self) -> str:
        """Path of the applied-plist digest stamp — PER AGENT, in the shared project_dir.

        Two agents share ``project_dir`` (one npm install root), so the marker filename
        must not be: a shared marker would make each agent read the other's digest, see
        a mismatch, and bootout/bootstrap a perfectly healthy job on every reload. See
        :attr:`AgentSpec.applied_marker_name`.
        """
        return os.path.join(self.project_dir, self.spec.applied_marker_name)

    def _read_applied_digest(self) -> Optional[str]:
        try:
            with open(self._applied_marker_path(), "r", encoding="utf-8") as handle:
                return handle.read().strip() or None
        except OSError:
            return None

    def _record_applied_digest(self, digest: str) -> None:
        try:
            os.makedirs(self.project_dir, exist_ok=True)
            with open(self._applied_marker_path(), "w", encoding="utf-8") as handle:
                handle.write(digest + "\n")
        except OSError as exc:  # pragma: no cover - best-effort marker
            self.logger.debug("could not record applied LaunchAgent digest: %s", exc)

    def uninstall(self) -> None:
        """Unload and remove the LaunchAgent. NEVER touches the storage dir."""
        if not self._bootout():
            self._launchctl("unload", self.plist_path)
        try:
            os.remove(self.plist_path)
            self.logger.info("Removed LaunchAgent %s (storage left intact)", self.plist_path)
        except FileNotFoundError:
            pass

    def remove_package(self) -> bool:
        """Uninstall THIS agent's npm package for a clean reinstall. True if it went.

        Stops the managed job and reaps any orphan first (so nothing holds the files or
        the storage lock), then removes the package, then drops this agent's
        applied-plist marker so the next ensure_installed re-bootstraps. The storage dir
        is SACRED and never touched — commissioned devices and pairings survive a clean
        reinstall. Blocking; run off the Indigo main thread.

        **Per-package since E7, and it had to become so.** This used to ``rmtree`` the
        whole shared ``node_modules`` and delete ``package-lock.json``, which was
        tolerable while exactly one agent existed and destructive the moment a second one
        did: the sibling's package vanished underneath a launchd job that was still
        loaded and still pointing at it, so the next respawn crash-looped on
        module-not-found, its applied marker still matched, and nothing in the plugin
        ever said why. The lock file goes the same way — it describes the whole install
        root, not one package, so deleting it on behalf of one agent unpins the other's
        transitive dependency tree at its next install.

        ``npm uninstall`` is preferred over deleting the directory because it is the only
        thing that also prunes the transitive dependencies this package brought in and
        nothing else needs — matter.js is ~40MB of them. Removing the package directory
        is the fallback for a project dir npm cannot operate on at all.

        **The trade this makes explicit:** wiping ``node_modules`` wholesale also fixed a
        corrupt *shared* dependency, and this no longer does. That was never what the
        menu action claimed to do, and rebuilding a sibling agent's install as a side
        effect of recovering this one is worse than the fault it happened to cure.

        **The return value is the fix for a message that was always the same.**
        This used to log "Removed the … package" unconditionally: npm missing,
        npm refusing, an ``OSError`` starting it and an ``rmtree`` that raised
        were all reported as a completed removal, and the caller then reinstalled
        on top of the wedged install the user was trying to clear — with a log
        saying it had been cleared. The outcome is now decided by looking: the
        package directory is either gone or it is not.
        """
        self._bootout()
        self.reap_orphan_servers()
        if not self._npm_uninstall():
            self._remove_package_dir()
        try:
            os.remove(self._applied_marker_path())
        except OSError:
            pass
        if os.path.exists(self._package_dir()):
            self.logger.error(
                "Could NOT remove the %s package: %s is still there. Nothing was reinstalled over "
                "it, so the wedged install you are trying to clear is still in place — remove the "
                "directory by hand (or run 'npm uninstall --prefix %s %s'), then retry.",
                self.spec.package, self._package_dir(), self.project_dir, self.spec.package)
            return False
        self.logger.info("Removed the %s package under %s (storage left intact)",
                         self.spec.package, self.project_dir)
        return True

    def _npm_uninstall(self) -> bool:
        """``npm uninstall`` this agent's package. True if npm reported success.

        Returns False — quietly, at debug — when npm is absent or refuses, because the
        caller has a working fallback and a warning here would name a problem the user
        does not have.
        """
        npm = os.path.join(self.resolved_bin_dir, "npm")
        if not self._exists(npm):
            self.logger.debug("npm not found at %s; removing the package directory instead", npm)
            return False
        env = dict(os.environ)
        env["PATH"] = self.resolved_bin_dir + os.pathsep + env.get("PATH", "")
        try:
            result = self._run([npm, "uninstall", "--prefix", self.project_dir, self.spec.package],
                               capture_output=True, text=True, check=False, env=env)
        except OSError as exc:
            self.logger.debug("npm uninstall %s could not start (%s)", self.spec.package, exc)
            return False
        if result is None or result.returncode != 0:
            detail = "" if result is None else (result.stderr or result.stdout or "").strip()
            self.logger.debug("npm uninstall %s exited non-zero (%s); removing the package "
                              "directory instead", self.spec.package, detail[:500])
            return False
        return True

    def _remove_package_dir(self) -> None:
        """Delete ``node_modules/<package>`` and nothing else.

        The fallback when npm cannot run. Scoped to this agent's own directory: the
        sibling's package, the shared transitive dependencies it may also be using, and
        ``package-lock.json`` are all left alone. Some of this package's own transitive
        deps are therefore orphaned in ``node_modules`` — harmless, and the next
        ``npm install`` reconciles them.
        """
        target = os.path.join(self.project_dir, "node_modules", self.spec.package)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.remove(target)
        except OSError as exc:
            self.logger.warning("could not remove %s: %s", target, exc)

    def stop(self) -> bool:
        """Stop the agent (bootout) but keep the plist so ``start`` can reload it.

        Part of the ``server_control`` seam used by fabric restore. Returns True
        if the bootout succeeded.
        """
        return self._bootout()

    def start(self) -> bool:
        """Start the agent: reload the existing plist, or install it if absent.

        Part of the ``server_control`` seam used by fabric restore. Returns the
        REAL outcome so callers (notably fabric restore) are never told the server
        started when it did not: a successful ``bootstrap`` on the existing-plist
        path, or :meth:`is_alive` after the install path (``ensure_installed`` logs
        its own launchctl failure but returns None, so we verify independently).

        :meth:`is_alive`, NOT ``is_running``: the latter means "launchd knows this
        label", which a loaded-and-dead job satisfies — so a bootstrap that put a
        job on the books and a crash-loop that never stayed up reported the same
        success to fabric restore, the one caller least able to afford it.
        """
        if os.path.exists(self.plist_path):
            self.reap_orphan_servers()  # nothing legit runs after stop(); clear any orphan
            self.clear_stale_storage_locks()  # …nor holds a lock a dead/reassigned pid left
            return self._bootstrap_and_record()
        self.ensure_installed()
        return self.is_alive()

    def restart(self) -> bool:
        """Reload the agent from the on-disk plist so the CURRENT args take effect.

        NOT ``kickstart -k``: that respawns the job's *cached* in-memory definition, so
        a job first bootstrapped by a pre-fix plugin keeps its buggy ``--port ""`` (the
        "Invalid integer:" crash-loop) even after the plist has been corrected — only a
        bootout + bootstrap makes launchd re-read the file. This is also the path the
        plugin takes right after installing a new matter-server version: the args are
        unchanged but the code on disk is new, so the running process must be replaced
        (which is why the caller can't rely on :meth:`ensure_installed` alone — that
        deliberately leaves an up-to-date job untouched). Returns True on success.
        """
        if not self._exists(self.plist_path):
            # Nothing to bootstrap. Without this the bootstrap fails with a bare rc 5 and
            # the fallback below logs "falling back to reinstall" while reinstalling
            # nothing — burying the real cause (which ensure_installed already logged
            # when it tore the plist down) under a misleading message.
            self.logger.error(
                "no %s LaunchAgent at %s — nothing to restart. Fix the "
                "problem reported above, then reload the plugin.",
                self.spec.package, self.plist_path,
            )
            return False
        self._bootout()  # ok if not loaded — we bootstrap fresh next regardless
        # bootout only stops the LaunchAgent's own job; a server that outlived an
        # earlier LaunchAgent keeps holding the storage lock and would make the fresh
        # instance die with "Storage is locked by another process". Reap it first.
        self.reap_orphan_servers()
        self.clear_stale_storage_locks()  # …and a lock nobody living holds, e.g. post-reboot
        if self._bootstrap_and_record():  # records the digest of the plist actually loaded
            return True
        # fall back to a full unload/reinstall cycle
        self.logger.warning("%s reload failed; falling back to reinstall", self.spec.package)
        self.uninstall()
        self.ensure_installed()
        return self.is_alive()   # "loaded" is not "running" — see is_running()

    def is_running(self) -> bool:
        """Whether launchd knows this label. **"Loaded", NOT "alive".**

        Kept under its historical name and its historical meaning because callers
        that ask "is there a job here to bootout / to leave alone" want exactly
        this. A job that is loaded and DEAD passes it — the #104 fault-2 state
        this file already handles at :meth:`_apply_plist` — so anything that
        wants to report a process as running must use :meth:`run_state`.
        """
        result = self._launchctl("print", f"gui/{os.getuid()}/{self.spec.label}")
        return bool(result is not None and result.returncode == 0)

    #: :meth:`run_state` outcomes. Four, because collapsing them is how "the
    #: LaunchAgent is running" gets printed over a job that never started.
    NOT_LOADED = "not_loaded"
    RUNNING = "running"
    LOADED_NOT_RUNNING = "loaded_not_running"
    UNKNOWN = "unknown"

    def run_state(self) -> str:
        """What launchd says about the job, as one of four distinguishable facts.

        * :data:`NOT_LOADED` — launchd has no such label.
        * :data:`RUNNING` — loaded, with a pid we parsed. The ONLY positive
          signal; nothing may claim the process is up without it.
        * :data:`LOADED_NOT_RUNNING` — loaded with no ``pid =`` line at all.
          launchd reports ``state = not running`` and, under our ``KeepAlive
          {SuccessfulExit: false}``, has decided not to respawn it (a node that
          exits 0 on a fatal startup error reads as a clean exit). Loaded and
          dead, indefinitely.
        * :data:`UNKNOWN` — a ``pid =`` line we could not parse. The job may
          well be alive and we cannot prove it either way, so callers must
          neither claim success nor report a failure.

        The facts were already parsed by :meth:`_managed_job`; only the readers
        were missing, which is why ``ensure_installed() is not None`` was being
        printed as "the LaunchAgent is running".
        """
        job = self._managed_job()
        if not job["loaded"]:
            return self.NOT_LOADED
        if job["pid"] is not None:
            return self.RUNNING
        return self.UNKNOWN if job["pid_line"] else self.LOADED_NOT_RUNNING

    def is_alive(self) -> bool:
        """Whether a process is (or may be) running under this label.

        True for :data:`RUNNING` and for :data:`UNKNOWN` — an unparseable pid
        line is not evidence of death, and treating it as failure would report a
        healthy server as stopped. False only when launchd itself says there is
        no job, or says the job is loaded and not running.
        """
        return self.run_state() in (self.RUNNING, self.UNKNOWN)

    # ------------------------------------------------------------------
    # Orphan reaping — a server can outlive the LaunchAgent that started it
    # ------------------------------------------------------------------
    def reap_orphan_servers(self, exclude_pid: Optional[int] = None) -> int:
        """Stop any process of THIS agent's package bound to THIS agent's storage.

        launchd's ``bootout`` only stops the job it currently manages; a server that
        outlived an earlier LaunchAgent (common after the reload/reinstall churn this
        plugin has seen) keeps running and holds the storage lock, so every fresh
        instance dies with "Storage is locked by another process (pid N)" — matter-server
        only auto-clears a lock whose owner is *dead*. We find the live owner by matching
        our package dir AND our ``--storage-path`` in the process command line (so an
        unrelated node process, another agent, or another user's server, is never
        touched), OR by finding one of our package's processes squatting on our port —
        see :meth:`_running_server_pids` for why the storage-path match alone has a blind
        spot. Matches are SIGTERMed, we wait briefly, then SIGKILL any that ignore TERM —
        and wait once more (stale-lock review, finding B; see the comment at that second
        wait for why). A port holder we can't identify as ours is never signalled, only
        warned about.

        Pass ``exclude_pid`` (the managed job's pid) to leave a healthy running server
        alone while still clearing an orphan beside it. Returns how many were signalled.
        """
        pids = self._running_server_pids(exclude_pid=exclude_pid)
        self._warn_on_foreign_port_holder(reapable=pids, exclude_pid=exclude_pid)
        if not pids:
            return 0
        self.logger.warning(
            "Stopping %d stray %s process(es) (pid %s) that outlived their "
            "LaunchAgent and hold the storage lock, so a fresh server can start.",
            len(pids), self.spec.package, ", ".join(map(str, pids)),
        )
        for pid in pids:
            self._signal(pid, "TERM")
        # Poll for a clean SIGTERM shutdown (matter-server releases the lock in its exit
        # handler, normally sub-second). Bounded ~1.5s worst case; this blocks the
        # (already synchronous) start/restart path only in the rare orphan-present case —
        # a brief, deliberate stall to un-wedge a server that would otherwise never start.
        if self._wait_for_pids_gone(exclude_pid=exclude_pid):
            return len(pids)
        stragglers = self._running_server_pids(exclude_pid=exclude_pid)
        for pid in stragglers:
            self.logger.warning("%s pid %s ignored SIGTERM; sending SIGKILL",
                                self.spec.package, pid)
            self._signal(pid, "KILL")
        # SIGKILL never runs matter.js's exit handler — unlike the clean SIGTERM exit
        # above, a killed process has DEFINITELY not released its storage lock. All
        # three sweep call sites run clear_stale_storage_locks() microseconds after
        # this method returns, and _ps_map() still lists a just-KILLed pid, with its
        # full (still-matching) command line, for a beat after kill(2) returns — a
        # dying/zombie process, not a gone one. Without waiting here exactly as the
        # SIGTERM path does above, the sweep would match that corpse as LIVE_PID_OURS
        # and leave its lock in place: the "Storage is locked by another process"
        # failure the sweep exists to end, and the worst case to miss, because a
        # KILLed owner's lock is unambiguously stale, never merely undecided.
        if not self._wait_for_pids_gone(exclude_pid=exclude_pid):
            survivors = self._running_server_pids(exclude_pid=exclude_pid)
            self.logger.warning(
                "%s pid(s) %s did not exit even after SIGKILL; its storage lock may "
                "still be held the next time this plugin tries to clear it.",
                self.spec.package, ", ".join(map(str, survivors)),
            )
        return len(pids)

    def _wait_for_pids_gone(self, exclude_pid: Optional[int], attempts: int = 6,
                             interval: float = 0.25) -> bool:
        """Poll :meth:`_running_server_pids` until none remain, or give up.

        Shared by both the post-SIGTERM and post-SIGKILL waits in
        :meth:`reap_orphan_servers` — the same idiom, the same injectable
        ``self._sleep`` (instant in tests), and the same bound, because either
        signal can leave a corpse visible to ``ps`` for a beat before the kernel
        actually reaps it. Returns True once ``_running_server_pids`` reports none
        left; False if ``attempts`` polls all still saw at least one.
        """
        for _ in range(attempts):
            self._sleep(interval)
            if not self._running_server_pids(exclude_pid=exclude_pid):
                return True
        return False

    def _ps_map(self) -> dict[int, str]:
        """pid → full command line for every running process ({} if ps is unavailable)."""
        try:
            # -ww: never truncate the command column — our --storage-path sits late in
            # the arg list, and macOS ps truncates to a default width without it, which
            # would drop the match and hide the orphan.
            result = self._run(["ps", "-A", "-ww", "-o", "pid=,command="],
                               capture_output=True, text=True, check=False)
        except OSError:
            return {}
        if result is None or result.returncode != 0:
            return {}
        procs: dict[int, str] = {}
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            head, _, command = stripped.partition(" ")
            try:
                procs[int(head)] = command
            except ValueError:
                continue
        return procs

    def _port_listener_pids(self) -> Optional[list[int]]:
        """PIDs listening on our port, via ``lsof``.

        Returns a list (possibly empty — "nothing is listening", a real answer) or
        **None** meaning *we could not tell*. That distinction is the whole point of
        issue #182: the old signature collapsed "no listener" and "the probe is
        broken" into the same ``[]``, so when the probe silently failed the plugin
        concluded nobody held the port and left a foreign server driving the fabric.
        Callers that only want candidates use ``or []``; callers that diagnose must
        branch on None.

        The port — not the storage path — is the resource a second server actually
        contends for: a stray that holds it makes every fresh instance die with
        "listen EADDRINUSE" (issue #104). An agent with no port
        (``spec.port is None``) has nothing to contend for, so we don't look.
        """
        if self.spec.port is None:
            return []
        for candidate in LSOF_CANDIDATES:
            # Absolute candidates are skipped when absent; the bare name is always
            # attempted (PATH may still resolve it on an unusual install).
            if candidate.startswith("/") and not self._exists(candidate):
                continue
            try:
                result = self._run(
                    [candidate, "-nP", f"-iTCP:{self.spec.port}", "-sTCP:LISTEN", "-t"],
                    capture_output=True, text=True, check=False,
                )
            except OSError:
                continue
            # rc 1 is lsof's "nothing matched" — a successful probe with an empty
            # answer, NOT a failure. Any other non-zero rc means it could not tell us.
            if result is None or result.returncode not in (0, 1):
                continue
            pids: list[int] = []
            for line in (result.stdout or "").split():
                try:
                    pids.append(int(line))
                except ValueError:
                    continue
            # A working probe re-arms the warning, so a LATER failure is reported
            # again rather than being swallowed by a latch set weeks ago (review of
            # #183). The flag tracks "currently degraded", not "ever degraded".
            self._port_probe_warned = False
            return pids
        self._warn_port_probe_unusable()
        return None

    def _warn_port_probe_unusable(self) -> None:
        """Say once per degradation that we cannot see who holds our port.

        Not once per call: the reap path probes several times per pass and a repeated
        warning would bury the log. But the latch is cleared by the next SUCCESSFUL
        probe, so each distinct episode of blindness gets its own warning — a
        transient failure at startup must not silence the real one a month later.
        Silence here is what issue #182 was made of, so this must never be downgraded
        to debug.
        """
        if self._port_probe_warned:
            return
        self._port_probe_warned = True
        self.logger.warning(
            "cannot determine what is listening on port %s: lsof was not runnable "
            "(tried %s). The check that catches a second %s squatting this port is "
            "therefore disabled, so a port conflict would show up only as a server "
            "that behaves as if it never started. Diagnose by hand with: "
            "lsof -nP -iTCP:%s -sTCP:LISTEN",
            self.spec.port, ", ".join(LSOF_CANDIDATES), self.spec.package,
            self.spec.port,
        )

    def _running_server_pids(self, exclude_pid: Optional[int] = None) -> list[int]:
        """PIDs of running processes of this agent's package that it should reap.

        Two independent match signals, because either alone has a blind spot:

        * **storage path** — our package dir (``…/node_modules/{package}``) AND our
          ``storage_path`` in the command line. The pair is unique to this agent's
          process, so an unrelated node process — or the OTHER agent, which has its own
          package and storage — is never touched. Blind to a server started from a
          different install layout, an older path, or by hand.
        * **our port** — anything listening on ``spec.port`` whose command line names
          our package. This is the case that cost an hour in #104: a
          stray from another path held 5580, so the storage-path match never saw it
          while every new instance died with EADDRINUSE. A port holder that is *not*
          one of ours is deliberately NOT reaped (see :meth:`reap_orphan_servers`,
          which warns about it instead) — killing an unrelated listener would be a
          far worse failure than the one we are fixing.
        """
        procs = self._ps_map()
        if not procs:
            return []
        pkg_dir = self._package_dir()
        # None ("could not tell") degrades to the storage-path signal alone, which is
        # exactly the pre-#104 behaviour — the probe having failed is reported by
        # _warn_port_probe_unusable, not papered over here.
        port_pids = set(self._port_listener_pids() or [])
        pids: list[int] = []
        for pid, command in procs.items():
            if pid == exclude_pid:
                continue
            ours = pkg_dir in command and self.storage_path in command
            strays_on_our_port = pid in port_pids and self.spec.package in command
            if ours or strays_on_our_port:
                pids.append(pid)
        return sorted(pids)

    def _warn_on_foreign_port_holder(self, reapable: list[int],
                                     exclude_pid: Optional[int] = None) -> None:
        """Warn when our port is held by something we will not reap.

        The #104 failure mode was silent: matter-server logged "listen EADDRINUSE
        127.0.0.1:5580" and exited, the plugin reported "connected … listening"
        (against the *stray*), and nothing tied the two together. We refuse to kill a
        process we can't identify as ours, but staying quiet about it is what turned a
        one-line diagnosis into an hour. Say it once per reap, with the pid and command
        so ``lsof``/``kill`` are an obvious next step.
        """
        holders = [pid for pid in (self._port_listener_pids() or [])
                   if pid != exclude_pid and pid not in reapable]
        if not holders:
            return
        procs = self._ps_map()
        for pid in holders:
            self.logger.warning(
                "port %s is already held by pid %s (%s), which this plugin will not "
                "stop because it is not a %s it recognises. A new "
                "%s cannot bind and will exit with EADDRINUSE — stop that "
                "process, or set a different port in Configure….",
                self.spec.port, pid, procs.get(pid, "unknown command"),
                self.spec.package, self.spec.package,
            )

    def _process_age_seconds(self, pid: int) -> Optional[int]:
        """Seconds since ``pid`` started, or None if ``ps`` could not tell us.

        Used to separate "still starting up" from "genuinely headless" without
        consulting the append-only error log. ``ps -o etime=`` prints
        ``[[DD-]HH:]MM:SS``; anything we cannot parse returns None, and callers must
        treat None as "do not accuse" rather than as age zero.

        Failure is warned about (once per episode, like the port probe) because the
        "nothing is listening" diagnosis depends ENTIRELY on this age gate: a broken
        ``ps`` would silently disable the very check #182 exists to add, which is the
        shape of bug this whole change set is about.
        """
        try:
            result = self._run(["ps", "-o", "etime=", "-p", str(pid)],
                               capture_output=True, text=True, check=False)
        except OSError:
            self._warn_process_age_unusable(pid, "ps could not be run")
            return None
        if result is None or result.returncode != 0:
            # rc 1 here means "no such process" — the pid died between the launchctl
            # read and now, which is a real answer but not one we can age.
            self._warn_process_age_unusable(pid, "ps reported no such process")
            return None
        raw = (result.stdout or "").strip()
        days = 0
        try:
            if "-" in raw:
                day_part, _, raw = raw.partition("-")
                days = int(day_part)
            parts = [int(chunk) for chunk in raw.split(":")]
            if len(parts) == 2:                 # MM:SS — normalise to HH:MM:SS
                parts.insert(0, 0)
            hours, minutes, seconds = parts     # ValueError on any other arity
        except ValueError:
            # Covers an empty/blank line, a non-numeric field, and a shape we don't
            # know — all of which mean the same thing to the caller: don't know.
            self._warn_process_age_unusable(pid, f"could not parse ps etime {raw!r}")
            return None
        self._process_age_warned = False        # re-arm: see _port_listener_pids
        return days * 86400 + hours * 3600 + minutes * 60 + seconds

    def _warn_process_age_unusable(self, pid: int, why: str) -> None:
        """Say once per degradation that we cannot age our own process.

        Same latch discipline as :meth:`_warn_port_probe_unusable`, and warning rather
        than staying quiet for the same reason: without an age we can never conclude
        "running but nothing is listening", so this failing silently would disable that
        diagnosis with no operator-visible symptom at all.
        """
        if self._process_age_warned:
            return
        self._process_age_warned = True
        self.logger.warning(
            "cannot determine how long %s (pid %s) has been running: %s. The check for "
            "a server that is alive but has no listener on port %s needs that age, so "
            "it is disabled until this works again.",
            self.spec.package, pid, why, self.spec.port,
        )

    # ------------------------------------------------------------------
    # Stale storage-lock clearing — a lock can outlive the process that wrote
    # it, and a reboot defeats matter.js's own staleness check entirely.
    # ------------------------------------------------------------------
    def clear_stale_storage_locks(self) -> int:
        """Delete any matter.lock this agent's own storage still holds that no
        LIVING process of ours can be holding. Returns how many were cleared.

        matter.js's stale-lock check (node_modules/@matter/nodejs/src/fs/lock-utils.ts,
        ``staleReason``) reads matter.pid (``"<pid> <token>"``) and only compares the
        token when the recorded pid equals ITS OWN pid; for any other pid it does a
        bare ``process.kill(pid, 0)`` and calls the lock live the moment that succeeds.
        After a reboot, macOS is free to hand a recycled pid to anything, and it did:
        2026-09-15 22:03, jarvis rebooted, and matter.pid's pid 1621 (the pre-reboot
        matter-server) came back as IndigoPluginHost3 running Home Intelligence — a
        live, unrelated, still-running process. matter.js read that as "still mine"
        and never cleared the lock, so the server crash-looped on "Storage is locked
        by another process" for 28 minutes across 155 attempts. matter-server is
        upstream and 0.17.9 ships the byte-identical check, so this is our fix to
        carry, not theirs to wait for — run immediately before we ask launchd to
        bootstrap a process that would otherwise lose to a lock nobody living holds.

        Walks ``self.storage_path`` itself AND its immediate subdirectories (one
        level): matter-server keeps its lock at the storage root, but the bridge's
        tree keeps one per purpose (``config/``, ``certificates/``, ``vendors/``,
        ``ota/``, ``server-1-fff1/`` and friends), so there is no single fixed
        location that covers both agents' layouts.

        For each directory holding a ``matter.lock``, ``matter.pid`` decides it:

        * missing, or a readable file whose first field is not an integer
          (including undecodable/binary content — a power-cut mid-write leaves
          exactly this) → stale (matter.js itself treats "no PID file" as
          stale, so we do too).
        * present but UNREADABLE for any other reason (permissions, EIO, too
          many open files — the plugin host runs many plugins) → a PROBE
          FAILURE, not "no owner". The file may well name a live owner we
          simply could not see; the lock is left in place and this is warned
          about, never treated as a licence to delete (see
          :meth:`_read_matter_pid`).
        * the recorded pid is not currently running → stale.
        * the recorded pid IS running → an ORDERED decision, not two
          independent signals (see :meth:`_stale_reason_for_live_pid`): a
          readable command line settles it outright, one way or the other,
          and the start-time signal only gets a vote when the command line is
          inconclusive (empty/unreadable).

        A probe that cannot tell — ``ps`` unusable, an unreadable ``matter.pid``,
        or an unreadable command line *and* an unknowable process age — is never
        treated as evidence of staleness (the workspace CLAUDE.md
        degradation-path convention: an unusable precondition must not silently
        read as "safe to act"). Deleting a live server's lock corrupts a
        running fabric, which is a strictly worse outage than the crash-loop
        this method exists to end, so those cases are WARNED about and the lock
        is left exactly as found — a silent zero here is indistinguishable from
        "nothing was wrong", which is the shape of bug this method exists to end.
        """
        procs = self._ps_map()
        ps_usable = bool(procs)
        if ps_usable:
            self._ps_unusable_for_lock_warned = False  # re-arm: see _port_listener_pids
        cleared = 0
        for directory in self._lock_candidate_dirs():
            lock_path = os.path.join(directory, MATTER_LOCK_FILENAME)
            if not self._exists(lock_path):
                continue
            pid, unreadable = self._read_matter_pid(os.path.join(directory, MATTER_PID_FILENAME))
            if unreadable:
                self._warn_pid_file_unreadable(directory)
                continue
            if pid is None:
                if self._clear_lock(directory, "matter.pid is missing or unparseable"):
                    cleared += 1
                continue
            if not ps_usable:
                # Cannot even ask "is this pid alive" — say so and stop here rather than
                # falling through to a start-time check whose age probe (ps -p) would
                # fail for the exact same reason and could look like a second, unrelated
                # confirmation of "we don't know".
                self._warn_ps_unusable_for_lock(directory, pid)
                continue
            if pid not in procs:
                if self._clear_lock(directory, f"pid {pid} in matter.pid is no longer running"):
                    cleared += 1
                continue
            status, reason = self._stale_reason_for_live_pid(directory, pid, procs[pid])
            if status == LIVE_PID_STALE:
                if self._clear_lock(directory, reason):
                    cleared += 1
            elif status == LIVE_PID_UNDECIDED:
                self._warn_live_pid_undecided(directory, pid, reason)
            # LIVE_PID_OURS: positively identified as our own live server — nothing
            # to clear, nothing to warn about.
        return cleared

    def _lock_candidate_dirs(self) -> list[str]:
        """``self.storage_path`` plus its immediate subdirectories.

        One level deep covers both agents' layouts (see
        :meth:`clear_stale_storage_locks`) without hardcoding either one's
        subdirectory names. A storage root that does not exist yet (first run)
        degrades to just the root itself — nothing to clear, nothing to crash on.

        Symlinked entries are skipped, never descended into: ``os.path.isdir``
        follows symlinks, so a symlink planted in the storage root would have
        let ``matter.lock``/``matter.pid`` be deleted OUTSIDE the storage tree
        entirely — a much larger blast radius than a stale lock.
        """
        dirs = [self.storage_path]
        try:
            entries = os.listdir(self.storage_path)
        except FileNotFoundError:
            return dirs
        except OSError as exc:
            # PermissionError/NotADirectoryError/EIO etc — narrows the sweep to the
            # root only, which is exactly where the bridge keeps NONE of its locks.
            # Must be warned about, not folded into the quiet first-run case, or a
            # broken listdir silently reports the same "0 cleared" as a clean sweep.
            self._warn_listdir_unusable(self.storage_path, exc)
            return dirs
        for name in entries:
            path = os.path.join(self.storage_path, name)
            if os.path.islink(path):
                continue
            if os.path.isdir(path):
                dirs.append(path)
        return dirs

    def _warn_listdir_unusable(self, path: str, exc: OSError) -> None:
        """Say once per degradation that the storage root could not be listed.

        Same latch discipline as :meth:`_warn_port_probe_unusable`: cleared the
        next time :meth:`_lock_candidate_dirs` succeeds, so a later, separate
        episode is reported again rather than silenced by this one.
        """
        if self._listdir_warned:
            return
        self._listdir_warned = True
        self.logger.warning(
            "could not list %s (%s), so the stale-lock sweep is checking only the "
            "storage root — a lock in one of its subdirectories (config/, "
            "certificates/, …) would be missed entirely. Diagnose by hand with: "
            "ls -la %s",
            path, exc, path,
        )

    def _read_matter_pid(self, path: str) -> tuple[Optional[int], bool]:
        """Read ``matter.pid``'s leading pid (``"<pid> <token>"``).

        Returns ``(pid, unreadable)`` — THREE outcomes, not two:

        * ``(pid, False)`` — a pid we can trust.
        * ``(None, False)`` — genuinely no owner recorded, exactly what
          matter.js's own check also calls stale: the file is absent, empty,
          or its first field is not an integer. A ``UnicodeDecodeError`` (a
          binary/truncated file — precisely what a power cut mid-write leaves)
          is folded into this outcome too: it is unreadable CONTENT, not a
          probe failure, and a caller with a lock and no trustworthy owner in
          the file has nothing more to learn from it either way.
        * ``(None, True)`` — a PROBE FAILURE (permissions, EIO, too many open
          files — the plugin host runs many plugins): the file may well name a
          LIVE owner and we simply could not read it. Must never be treated as
          "no owner" — see :meth:`clear_stale_storage_locks`.

        A successful read (either a pid or a genuine "no owner") re-arms
        :meth:`_warn_pid_file_unreadable`, same discipline as the port and
        process-age probes.
        """
        try:
            with open(path, "r", encoding="utf-8") as handle:
                content = handle.read()
        except FileNotFoundError:
            self._pid_read_warned = False
            return None, False
        except UnicodeDecodeError:
            self._pid_read_warned = False
            return None, False
        except OSError:
            return None, True
        self._pid_read_warned = False
        parts = content.split()
        if not parts:
            return None, False
        try:
            return int(parts[0]), False
        except ValueError:
            return None, False

    def _warn_pid_file_unreadable(self, directory: str) -> None:
        """Say once per degradation that ``matter.pid`` could not be read.

        Distinct from "missing" (which is a genuine, actionable "no owner") —
        this is a probe failure, and the file may still name a live server
        whose lock would otherwise be deleted out from under it. Same latch
        discipline as :meth:`_warn_port_probe_unusable`.
        """
        if self._pid_read_warned:
            return
        self._pid_read_warned = True
        pid_path = os.path.join(directory, MATTER_PID_FILENAME)
        self.logger.warning(
            "%s exists in %s but %s could not be read, so this plugin cannot tell "
            "whether a live process still owns it. Leaving the lock in place — "
            "diagnose by hand with: ls -la %s",
            MATTER_LOCK_FILENAME, directory, MATTER_PID_FILENAME, pid_path,
        )

    def _warn_ps_unusable_for_lock(self, directory: str, pid: int) -> None:
        """Say once per degradation that ``ps`` is unusable for the lock sweep.

        The bridge keeps one lock per subdirectory, so a single dead ``ps``
        would otherwise repeat this warning once per directory in the SAME
        sweep. Latched so one outage produces one warning, cleared (in
        :meth:`clear_stale_storage_locks`) the next time ``ps`` works again.
        """
        if self._ps_unusable_for_lock_warned:
            return
        self._ps_unusable_for_lock_warned = True
        self.logger.warning(
            "%s in %s names pid %s, but ps is unusable so this plugin cannot "
            "tell whether that process still holds it. Leaving the lock in "
            "place — diagnose by hand with: ps -p %s",
            MATTER_LOCK_FILENAME, directory, pid, pid,
        )

    def _stale_reason_for_live_pid(self, directory: str, pid: int,
                                    command: str) -> tuple[str, str]:
        """Judge a LIVE pid's lock — an ORDERED decision, not two independent signals.

        1. A readable command line that does NOT name our package dir and our
           storage path is positive proof of staleness (reboot pid reuse) —
           :data:`LIVE_PID_STALE`.
        2. A readable command line that DOES match is positive proof this is
           our own running server — :data:`LIVE_PID_OURS`, returned
           immediately. The decision ENDS here: nothing below may override a
           positive "this is ours". Falling through used to let a rounding-
           inflated start-time signal accuse a positively-identified, running
           server and delete its lock — a worse outage than the crash-loop
           this method exists to end.
        3. Only when the command line is INCONCLUSIVE (empty/unreadable) does
           the start-time signal get a vote: a process that started strictly
           after matter.pid's mtime (see :data:`START_AFTER_LOCK_SLACK_SECONDS`)
           cannot be the process that wrote that file — the reboot-proof
           signal. If that too cannot be evaluated (mtime or age unknowable),
           the pid is :data:`LIVE_PID_UNDECIDED`.

        Returns ``(status, detail)``. ``detail`` is the reason to log when
        ``status`` is :data:`LIVE_PID_STALE` or :data:`LIVE_PID_UNDECIDED`;
        empty for :data:`LIVE_PID_OURS`. Callers must treat
        :data:`LIVE_PID_UNDECIDED` as "leave it alone" exactly like
        :data:`LIVE_PID_OURS` — but, unlike ``OURS``, they must also report it:
        mutation testing proved that collapsing the two into a bare
        ``(False, "")`` lets a broken probe silently disable this whole check
        (patching ``age = age or 0`` killed no test and deleted live locks).
        """
        pkg_dir = self._package_dir()
        if command:
            ours = pkg_dir in command and self.storage_path in command
            if not ours:
                return LIVE_PID_STALE, (
                    f"pid {pid} is alive but its command line is not {self.spec.package} "
                    f"for this storage — the pid was reassigned (reboot pid reuse)"
                )
            return LIVE_PID_OURS, ""
        pid_file_mtime = self._file_mtime(os.path.join(directory, MATTER_PID_FILENAME))
        age = self._process_age_seconds(pid)
        if pid_file_mtime is not None and age is not None:
            started_at = time.time() - age
            # The margin is not cosmetic — see START_AFTER_LOCK_SLACK_SECONDS. Without
            # it the probe's own rounding can make a live owner look too young.
            if started_at > pid_file_mtime + START_AFTER_LOCK_SLACK_SECONDS:
                return LIVE_PID_STALE, (
                    f"pid {pid} started after matter.pid was last written, so it "
                    f"cannot be the process that wrote that lock"
                )
            return LIVE_PID_OURS, ""
        unknowns = []
        if pid_file_mtime is None:
            unknowns.append(f"{MATTER_PID_FILENAME}'s mtime could not be read")
        if age is None:
            unknowns.append("its process age could not be determined")
        return LIVE_PID_UNDECIDED, (
            f"pid {pid}'s command line is unreadable and " + " and ".join(unknowns)
        )

    def _warn_live_pid_undecided(self, directory: str, pid: int, why: str) -> None:
        """Say once per degradation that a live pid's lock could not be judged.

        The lock is correctly left in place either way, but staying silent
        about WHY is exactly the gap mutation testing found: a sweep that looks
        at a lock and gives up leaves no operator-visible symptom, so the
        crash-loop this method exists to end just continues, unexplained. Same
        latch discipline as :meth:`_warn_port_probe_unusable`.
        """
        if self._live_pid_undecided_warned:
            return
        self._live_pid_undecided_warned = True
        self.logger.warning(
            "%s in %s names pid %s, which is running, but %s. Leaving the lock in "
            "place — diagnose by hand with: ps -p %s -o command=",
            MATTER_LOCK_FILENAME, directory, pid, why, pid,
        )

    @staticmethod
    def _file_mtime(path: str) -> Optional[float]:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def _clear_lock(self, directory: str, reason: str) -> bool:
        """Remove ``matter.lock`` (and best-effort ``matter.pid``) in ``directory``.

        Returns whether ``matter.lock`` itself is actually gone afterward — the
        caller's cleared-count and this method's own log line both hinge on
        that, never on removal merely being ATTEMPTED. This used to log
        "clearing stale … so <pkg> can start" at WARNING before attempting the
        removal and swallow a failed ``os.remove`` at DEBUG, so a read-only or
        permissions failure was reported as a completed clear while the lock
        was still on disk — the original outage, re-armed, now actively
        misleading (reviewers reproduced this against a read-only directory).

        ``FileNotFoundError`` on removal counts as success (already gone, e.g.
        a race with a concurrent clear) — not a failure to report.
        """
        lock_path = os.path.join(directory, MATTER_LOCK_FILENAME)
        try:
            os.remove(lock_path)
            removed = True
        except FileNotFoundError:
            removed = True
        except OSError as exc:
            removed = False
            self.logger.error(
                "could not remove stale %s in %s (%s): %s. %s will keep failing to "
                "start until this is cleared by hand: rm %s",
                MATTER_LOCK_FILENAME, directory, reason, exc, self.spec.package, lock_path,
            )
        if removed:
            self.logger.warning(
                "cleared stale %s in %s (%s) so %s can start.",
                MATTER_LOCK_FILENAME, directory, reason, self.spec.package,
            )
        # matter.pid is bookkeeping beside the actual lock — matter.js's own check
        # only cares about matter.lock — so it stays best-effort at debug, same
        # discipline as _record_applied_digest.
        try:
            os.remove(os.path.join(directory, MATTER_PID_FILENAME))
        except OSError as exc:
            self.logger.debug("could not remove %s: %s", os.path.join(directory, MATTER_PID_FILENAME), exc)
        return removed

    def _err_log_mentions_port_conflict(self) -> bool:
        """True if the agent's error log tail mentions an EADDRINUSE fatal.

        Corroboration only, never proof: the log is append-only across restarts, so a
        hit may be weeks stale. 200 lines because matter-server emits a multi-line
        stack trace after the fatal and then chats steadily, so the marker scrolls out
        of a 20-line tail within seconds.
        """
        return EADDRINUSE_MARKER in (self.tail_error_log(max_lines=200) or "")

    def port_conflict_report(self, managed_pid: Optional[int] = None) -> Optional[str]:
        """Describe why our managed job is not actually serving our port, else None.

        Issue #182: matter-server 1.2.2 logs a FATAL on ``listen EADDRINUSE`` and then
        **keeps running** — it holds its Matter operational port, maintains CASE
        sessions and writes its storage, it just never gets a WebSocket listener. So
        launchd reports a pid, :meth:`run_state` says ``RUNNING``, and the plugin's WS
        client connects happily to whatever OTHER server owns the port. On jarvis that
        state persisted 30 hours and orphaned 14 devices. "Has a pid" is therefore not
        evidence that our server is reachable, and this is the check that closes the gap.
        (It also corrects the older assumption, recorded in :meth:`_managed_job`, that
        such a server exits 0 and leaves the job visibly dead — 1.2.2 does not.)

        Signals, in order of trust:

        * **Authoritative** — the port is held by a pid that is not ours. Whatever our
          WS client reaches is then definitively not the server we manage.
        * **Age-gated** — nothing is listening at all, and our process is older than
          :data:`STARTUP_GRACE_SECONDS`. The age test is what separates this from a
          server that simply has not bound yet.
        * **Advisory** — the port probe is unusable but our error log mentions
          ``EADDRINUSE``. Advisory-only by the same discipline #93 applied to the ABI
          stamp: the err log is append-only across restarts, so a hit may be from a
          conflict resolved weeks ago. Report it, never act on it.

        The error log is deliberately NOT what gates the "nothing listening" case. Being
        append-only, one old ``EADDRINUSE`` would corroborate that accusation on every
        subsequent startup for ever — the log can establish that a conflict happened
        once, never that one is happening now.

        Returns a message for the caller to log, or None when nothing is wrong.
        """
        if self.spec.port is None:
            return None
        if managed_pid is None:
            managed_pid = self._managed_job()["pid"]
        if managed_pid is None:
            # No running managed job. That is a different fault with its own handling
            # in _apply(); calling it a port conflict would send the user hunting for
            # the wrong thing.
            return None
        holders = self._port_listener_pids()
        if holders:
            if managed_pid in holders:
                return None                     # the common case: all is well
            procs = self._ps_map()
            others = ", ".join(f"pid {pid} ({procs.get(pid, 'unknown command')})"
                               for pid in holders)
            message = (
                f"port {self.spec.port} is held by {others}, NOT by the "
                f"{self.spec.package} this plugin manages (pid {managed_pid}). The "
                f"plugin is therefore talking to a different server than the one it "
                f"starts, so its fabric and its devices may not be the ones you "
                f"configured. Stop the other process (or its LaunchAgent) and reload "
                f"the plugin."
            )
        elif holders is None:
            # The probe could not tell us anything, so the err log is all we have —
            # and being append-only it may describe a conflict resolved weeks ago.
            # Hence advisory wording and a hand-check recipe rather than a verdict.
            if not self._err_log_mentions_port_conflict():
                return None
            message = (
                f"{self.spec.package} (pid {managed_pid}) is running, but its error "
                f"log mentions {EADDRINUSE_MARKER} and this Mac cannot tell us who "
                f"holds port {self.spec.port}. If a second {self.spec.package} is "
                f"running, the plugin may be talking to THAT one and not to the "
                f"server it manages. Check: "
                f"lsof -nP -iTCP:{self.spec.port} -sTCP:LISTEN "
                f"(advisory — the log line may be from an old conflict)."
            )
        else:
            # Nothing is listening. That is either genuinely headless or simply a
            # server still inside its startup window, and the two are told apart by
            # PROCESS AGE — deliberately not by the err log. The log is append-only,
            # so a single old EADDRINUSE would accuse every future startup for ever
            # (caught in review of #183). Age uses no historical data at all.
            age = self._process_age_seconds(managed_pid)
            if age is None or age < STARTUP_GRACE_SECONDS:
                return None                     # too young, or we cannot tell → quiet
            message = (
                f"{self.spec.package} (pid {managed_pid}) has been running for {age}s "
                f"but NOTHING is listening on port {self.spec.port}, so the plugin "
                f"cannot reach it. This is what a lost port race leaves behind: "
                f"{self.spec.package} logs a fatal '{EADDRINUSE_MARKER}' and keeps "
                f"running without its WebSocket server. Restart it from "
                f"Plugins ▸ Matter, and see "
                f"{os.path.join(self.log_dir, self.spec.err_log)}."
            )
        return message

    def due_for_bootstrap_verification(self) -> Optional[float]:
        """The armed deadline once :data:`STARTUP_GRACE_SECONDS` has passed since
        this instance's most recent bootstrap and that bootstrap has not yet been
        port-checked (#187); ``None`` while nothing is pending or the grace window
        has not yet elapsed.

        Returns the DEADLINE ITSELF, not a bool (review finding, #187), so the
        caller can hand it straight back to :meth:`clear_bootstrap_verification` as
        a compare-and-clear token: a caller's own tick observes "due" here, then
        spends real wall-clock time inside a slow ``port_conflict_report()`` call
        (shells out to lsof/ps) before it gets around to clearing. If a FRESH
        bootstrap re-arms ``_bootstrap_verify_after`` on another thread during that
        gap — a menu restart, a config apply — an unconditional clear would wipe
        the new arming instead of the one the caller actually checked, silently
        cancelling ITS verification. Handing back the exact value observed, and
        having the clear compare against it, turns that race into a no-op instead
        of a lost verification.

        ``port_conflict_report`` already knows how to tell "still starting up" from
        "genuinely headless" (its own age-gated branch), but nothing was calling it
        specifically at the moment that matters most: right after a fresh bootstrap,
        when a rival is most likely to have won the port race in the bind-window gap.
        Blocking ``_apply_plist``/``start``/``restart`` with a sleep for the whole
        grace window would hold up plugin startup, so this is a flag for a caller's
        own periodic tick to poll instead — see :meth:`clear_bootstrap_verification`.
        """
        if self._bootstrap_verify_after is None:
            return None
        if time.monotonic() < self._bootstrap_verify_after:
            return None
        return self._bootstrap_verify_after

    def clear_bootstrap_verification(self, observed: float) -> None:
        """Disarm the pending post-bootstrap port check (#187) — compare-and-clear.

        ``observed`` must be the exact value :meth:`due_for_bootstrap_verification`
        handed the caller. Clears only when the CURRENTLY armed deadline still
        equals it, so a re-arm that landed after the caller observed "due" but
        before it got here (see that method's docstring) is left untouched — the
        stale clear becomes a no-op and the newer verification stays pending.
        """
        if self._bootstrap_verify_after == observed:
            self._bootstrap_verify_after = None

    def post_bootstrap_verdict(self) -> str:
        """Render #187's verdict once ``port_conflict_report()`` has answered
        ``None`` for a due verification — which happens for THREE different
        reasons a caller must not conflate (see that method's docstring): healthy,
        still inside (or unknowably within) the startup grace window, or the
        managed job has no pid at all. That last one is the #187 fault itself:
        bootstrap succeeded, but the process then lost the port bind race, logged
        EADDRINUSE, and exited — and ``KeepAlive {SuccessfulExit: False}`` never
        respawns it (see :meth:`_apply_plist`'s "loaded but not running" branch).
        It is exactly the case ``port_conflict_report`` deliberately declines to
        call a conflict, because calling it one would send a caller hunting for a
        rival process that does not exist.

        Returns one of:

          * :data:`VERDICT_DEAD` — the managed job currently has no pid. A
            verdict: the caller should report this and clear the verification.
          * :data:`VERDICT_CONFLICT_FREE` — a pid is present and its age has
            passed :data:`STARTUP_GRACE_SECONDS`. A verdict: healthy, clear.
          * :data:`VERDICT_PENDING` — a pid is present but its age is still
            inside the grace window, or unknowable. NOT a verdict — the caller
            must leave the verification armed and let the next tick retry.

        Reuses :meth:`_managed_job` rather than re-parsing ``launchctl print`` a
        second time in the same tick.
        """
        pid = self._managed_job()["pid"]
        if pid is None:
            return VERDICT_DEAD
        age = self._process_age_seconds(pid)
        if age is not None and age >= STARTUP_GRACE_SECONDS:
            return VERDICT_CONFLICT_FREE
        return VERDICT_PENDING

    def managed_job_has_pid(self) -> bool:
        """True if the managed launchd job currently reports a pid.

        Used to gate the "port conflict resolved" message (#187 review): a
        standing conflict must not be announced as resolved just because
        ``port_conflict_report()`` went quiet — it also goes quiet when the
        managed job has NO pid at all (see :meth:`post_bootstrap_verdict`), which
        is the opposite of resolved. Reuses :meth:`_managed_job`.
        """
        return self._managed_job()["pid"] is not None

    def adopt_pending_bootstrap_verification(self, previous: Optional["LaunchAgent"]) -> None:
        """Carry a pending #187 verification over from the instance THIS one replaces.

        The armed deadline lives on the instance, not anywhere prefs-durable, and
        ``bridge_agent_menu_mixin.py`` rebuilds a fresh agent on every call (ports and
        the mDNS interface are prefs, which may have changed since the last one).
        A rebuild whose own ``ensure_installed()`` takes the digest-match "leave
        alone" path — no fresh bootstrap on THIS instance — would otherwise
        silently drop whatever verification the replaced instance still had
        pending: a rapid re-export inside the grace window (every allow-list
        change rebuilds ``BridgeProcess``) is the case that actually hits this
        (review finding).

        Safe to call unconditionally after ``ensure_installed()`` (``previous``
        may be ``None`` — nothing to adopt on the very first call): if THIS
        instance's own call already bootstrapped, ``_bootstrap_verify_after`` is
        already set and the guard below is a no-op — a fresh arming always wins
        over an older one, never the reverse.
        """
        if previous is not None and self._bootstrap_verify_after is None:
            self._bootstrap_verify_after = previous._bootstrap_verify_after  # pylint: disable=protected-access

    def _managed_job(self) -> dict:
        """Parse ``launchctl print`` once into the three facts callers need.

        * ``loaded`` — the label exists (``launchctl print`` succeeded).
        * ``pid`` — the running job's pid, or None.
        * ``pid_line`` — whether a ``pid =`` line was present *at all*. This is the
          difference between "loaded but NOT running" (no such line; launchd reports
          ``state = not running``) and "running, but we could not parse the pid", and
          the two must not be conflated. #104's fault 2 lives in that gap: matter-server
          exits 0 on the ``EADDRINUSE`` FATAL, so launchd's ``KeepAlive
          {SuccessfulExit: false}`` policy classes it a clean exit and deliberately
          never respawns it — the job sits loaded-and-dead indefinitely. Treating that
          as "healthy, hands off" is why a plugin reload could not recover it either.
          **Version-dependent, and 1.2.2 does NOT behave this way** (issue #182): it
          logs the same fatal and keeps running, so the job has a pid and looks
          perfectly healthy from here while having no WebSocket listener at all. A pid
          from this method therefore means "launchd has a live process", never "our
          server is reachable" — :meth:`port_conflict_report` answers that second
          question. Both behaviours are in the wild, so neither may be assumed.
        * ``arguments`` — the ProgramArguments launchd actually cached at bootstrap,
          which is NOT necessarily what the plist on disk now says (fault 3).
        """
        job = {"loaded": False, "pid": None, "pid_line": False, "arguments": []}
        result = self._launchctl("print", f"gui/{os.getuid()}/{self.spec.label}")
        if result is None or result.returncode != 0:
            return job
        job["loaded"] = True
        in_arguments = False
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if in_arguments:
                if stripped == "}":
                    in_arguments = False
                elif stripped:
                    job["arguments"].append(stripped)
                continue
            if stripped.replace(" ", "") == "arguments={":
                in_arguments = True
            elif stripped.startswith("pid ="):
                job["pid_line"] = True
                try:
                    job["pid"] = int(stripped.split("=", 1)[1].strip())
                except (ValueError, IndexError):
                    job["pid"] = None
        return job

    def _managed_pid(self) -> Optional[int]:
        """The pid of the LaunchAgent's currently-running job, or None if not running.

        Parsed from ``launchctl print``'s ``pid = N`` line so reap can EXCLUDE the
        healthy managed server while still clearing an orphan beside it.
        """
        return self._managed_job()["pid"]

    def _warn_on_argument_drift(self, running_args: list[str]) -> None:
        """Warn when the live job's arguments differ from what we would launch now.

        launchd caches ProgramArguments at bootstrap, so a job can serve happily for
        days with arguments the plist no longer contains — which is precisely how #104
        presented: the plugin reported a healthy connection while matter-server ran with
        the *old* args, and a feature the user had just enabled simply appeared not to
        work. A matching applied-digest proves the right plist was written, never that
        the running job is using it. One warning turns that into a one-line diagnosis.
        """
        if not running_args:
            return  # launchctl gave us no arguments block — nothing to compare
        desired = self.program_arguments()
        if running_args == desired:
            return
        self.logger.warning(
            "the running %s was started with different arguments than the "
            "current settings would use — it is serving STALE configuration. Running: "
            "%s. Expected: %s. Reload the plugin (or Plugins ▸ Matter ▸ Restart "
            "the Matter controller) to apply the current settings.",
            self.spec.package, " ".join(running_args), " ".join(desired),
        )

    def _signal(self, pid: int, sig: str) -> None:
        try:
            result = self._run(["kill", f"-{sig}", str(pid)], capture_output=True, text=True, check=False)
        except OSError as exc:  # pragma: no cover - best-effort
            self.logger.debug("kill -%s %s failed: %s", sig, pid, exc)
            return
        # A non-zero kill (permission denied, ESRCH) is silently swallowed by check=False;
        # log it so a misbehaving reap is visible rather than reported as fully signalled.
        if result is not None and result.returncode != 0:
            self.logger.debug("kill -%s %s exited %s: %s", sig, pid, result.returncode,
                              (result.stderr or "").strip())

    # ------------------------------------------------------------------
    # launchctl helpers
    # ------------------------------------------------------------------
    def _bootstrap(self) -> bool:
        result = self._launchctl("bootstrap", f"gui/{os.getuid()}", self.plist_path)
        return bool(result is not None and result.returncode == 0)

    def _bootout(self) -> bool:
        result = self._launchctl("bootout", f"gui/{os.getuid()}/{self.spec.label}")
        return bool(result is not None and result.returncode == 0)

    def _launchctl(self, *args: str) -> Optional["subprocess.CompletedProcess"]:
        cmd = ["launchctl", *args]
        try:
            return self._run(cmd, capture_output=True, text=True, check=False)
        except OSError as exc:
            self.logger.warning("launchctl %s failed: %s", args[0], exc)
            return None
