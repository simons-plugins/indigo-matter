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

DEFAULT_PROJECT_DIRNAME = "indigo-matter"   # ~/indigo-matter (npm install location)
NPX_CANDIDATES = ("/opt/homebrew/bin/npx", "/usr/local/bin/npx")
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
# matter-server 1.2.2's package.json declares engines node >= 22.13.0. npm's engines
# check is advisory by default (exits 0 on an older node), so install() gates on this
# itself — otherwise a too-old node "successfully" installs an unrunnable server.
# Shared by every agent for the same reason INSTALL_NODE_STAMP is shared: they all run
# on the one node this plugin resolved.
MIN_NODE_VERSION = (22, 13)
# Records the node version the package was installed with, so preflight can catch an
# install-node vs run-node mismatch (native-binding ABI crash) before it crash-loops.
# DELIBERATELY SHARED between agents (not per-label): project_dir holds ONE node_modules
# installed by ONE node, and every agent's LaunchAgent runs that same node. A per-agent
# stamp would claim they can diverge, which is exactly the ABI crash this guards against.
# CAVEAT the sharing does not cover, REVIEWED AT E7 AND KEPT: if nodeBinDir is repointed
# BETWEEN two agents' installs, the later install rewrites the stamp with the new node and
# the earlier agent's already-built native bindings go unwarned (npm install of package B
# does not rebuild package A's bindings). Kept shared because the alternative — per-package
# versions inside one stamp — would make `abi_warning` claim the two agents CAN legitimately
# run on different nodes, which is the opposite of true: both LaunchAgents run whatever
# single node this plugin resolved, so a per-package stamp that disagreed with the other
# would be describing a state that cannot exist. The residual risk is one stale ADVISORY
# warning after a manual nodeBinDir change, and the remedy is in the message the warning
# already prints: run both Install/update menu actions.
INSTALL_NODE_STAMP = ".indigo-node"


def expand_home(path: str, home: str) -> str:
    """Expand a leading ``~`` against the given home dir (no os.environ lookup)."""
    if path.startswith("~"):
        return home + path[1:]
    return path


def _node_major(version: Optional[str]) -> Optional[int]:
    """Major version int from a node version string (``v22.18.0`` → 22), else None."""
    parsed = _parse_node_version(version) if version else None
    return parsed[0] if parsed else None


def _parse_node_version(name: str) -> Optional[tuple[int, ...]]:
    """Parse an nvm node dir / alias label into a comparable version tuple.

    Accepts ``v22.18.0``, ``22.18.0``, ``v22``, ``22`` → ``(22, 18, 0)`` /
    ``(22,)``. Returns ``None`` for non-numeric labels (e.g. ``lts/*``,
    ``default``) which cannot be matched to a version directory directly.
    """
    cleaned = name.strip()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    parts = cleaned.split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


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
        # Latch for the once-per-agent "cannot probe the port" warning (issue #182).
        self._port_probe_warned = False
        self.home = home or os.path.expanduser("~")
        # Optional explicit override: directory containing node/npx. nvm users can
        # pin a specific version here (e.g. ~/.nvm/versions/node/v22.18.0/bin);
        # blank means auto-detect (Homebrew → nvm → PATH). The ONLY pref read here:
        # everything else about an agent comes from its spec.
        raw_bin_dir = str(prefs.get("nodeBinDir", "") or "").strip()
        self.node_bin_dir = expand_home(raw_bin_dir, self.home) if raw_bin_dir else ""
        self.npx_path = npx_path or self._resolve_npx()
        # The node interpreter lives in the same bin dir as npx (Homebrew + nvm both
        # ship node and npx side-by-side). We launch node directly because the
        # matter-server npm package exposes no bin executable (see server_process).
        self.node_path = os.path.join(os.path.dirname(self.npx_path), "node")
        self.project_dir = os.path.join(self.home, DEFAULT_PROJECT_DIRNAME)
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

    def _resolve_npx(self) -> str:
        """Locate the ``npx`` binary, honouring an explicit pref then auto-detect.

        Resolution order:
          a. ``nodeBinDir`` pref (``{nodeBinDir}/npx``) — explicit override / pin.
          b. ``/opt/homebrew/bin/npx`` (Apple-Silicon Homebrew).
          c. ``/usr/local/bin/npx`` (Intel Homebrew).
          d. nvm auto-detect (``~/.nvm/versions/node/<version>/bin/npx``) —
             prefers ``~/.nvm/alias/default``, else highest installed version.
          e. ``shutil.which("npx")`` (whatever's on PATH).
          f. Apple-Silicon Homebrew default as a last resort; ``ensure_installed``
             will log if it's absent. We WARN here so a misconfigured user gets a
             hint rather than a silent dead LaunchAgent.

        Note: nvm's version dir is version-specific and changes when the user
        upgrades node. ``ensure_installed()`` re-resolves on every plugin startup,
        so a node upgrade is picked up on the next plugin restart. Set ``nodeBinDir``
        to pin a specific version explicitly.
        """
        # a. explicit override
        if self.node_bin_dir:
            candidate = os.path.join(self.node_bin_dir, "npx")
            if os.path.exists(candidate):
                return candidate
            self.logger.warning(
                "nodeBinDir is set to %s but no npx found there; falling back to "
                "auto-detect", self.node_bin_dir,
            )
        # b + c. Homebrew
        for candidate in NPX_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        # d. nvm
        nvm_npx = self._resolve_nvm_npx()
        if nvm_npx:
            return nvm_npx
        # e. PATH
        found = shutil.which("npx")
        if found:
            return found
        # f. last resort
        self.logger.warning(
            "Could not locate npx (checked nodeBinDir, Homebrew, nvm, and PATH). "
            "Set the 'Node bin directory' plugin pref to the folder containing "
            "node/npx (e.g. a ~/.nvm/versions/node/<version>/bin path). Falling "
            "back to %s.", NPX_CANDIDATES[0],
        )
        return NPX_CANDIDATES[0]

    def _resolve_nvm_npx(self) -> Optional[str]:
        """Find an nvm-installed ``npx``.

        Prefers the version named in ``~/.nvm/alias/default`` (a label like
        ``v22``, ``22``, ``v22.18.0`` or ``lts/*``); a partial label like ``22``
        matches the highest installed ``v22.*``. Falls back to the highest
        installed version directory overall. Returns the ``bin/npx`` path if it
        exists, else ``None``. The chosen ``bin`` dir holds BOTH node and npx, so
        the plist PATH (``dirname(npx)``) lets launchd run npx→node.
        """
        versions_dir = os.path.join(self.home, ".nvm", "versions", "node")
        if not os.path.isdir(versions_dir):
            return None
        try:
            installed = [d for d in os.listdir(versions_dir)
                         if os.path.isdir(os.path.join(versions_dir, d))]
        except OSError:
            return None
        if not installed:
            return None

        chosen: Optional[str] = None

        # Prefer the default alias if it resolves to an installed version.
        alias_file = os.path.join(self.home, ".nvm", "alias", "default")
        try:
            with open(alias_file, "r", encoding="utf-8") as handle:
                alias = handle.read().strip()
        except OSError:
            alias = ""
        if alias:
            chosen = self._match_nvm_version(alias, installed)

        # Otherwise (or if the alias didn't resolve) take the highest installed.
        if chosen is None:
            chosen = max(installed, key=lambda d: (_parse_node_version(d) or (-1,)))

        npx = os.path.join(versions_dir, chosen, "bin", "npx")
        return npx if os.path.exists(npx) else None

    @staticmethod
    def _match_nvm_version(alias: str, installed: list[str]) -> Optional[str]:
        """Resolve an nvm alias label to one of the installed version dirs.

        Exact match wins; a partial numeric label (``22`` → ``v22.*``) picks the
        highest matching version. Non-numeric labels (``lts/*``) return ``None``.
        """
        if alias in installed:
            return alias
        if ("v" + alias) in installed:
            return "v" + alias
        wanted = _parse_node_version(alias)
        if wanted is None:
            return None
        matches = [d for d in installed
                   if (_parse_node_version(d) or ())[:len(wanted)] == wanted]
        if not matches:
            return None
        return max(matches, key=lambda d: _parse_node_version(d) or (-1,))

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
            "EnvironmentVariables": {"PATH": f"{npx_dir}:/usr/bin:/bin"},
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

        A mismatch *may* mean the package's native bindings won't load — but the stamp
        is only written by this plugin's install(), so a user who reinstalls
        out-of-band (Terminal npm) leaves a STALE stamp behind. Blocking on it would
        refuse a perfectly good server, so this is a warning only: let the server try,
        and if it really is a mismatch it crash-loops and the agent's error log
        surfaces the cause. Never fires when either version is unknown.
        """
        stamped = self._read_install_node_major()
        if stamped is None:
            return None
        current = _node_major(self._node_version())
        if current is None or stamped == current:
            return None
        return (
            f"{self.spec.package} was installed with Node {stamped}.x but the resolved node "
            f"({self.node_path}) is {current}.x. If it fails to start, reinstall via "
            f"Plugins ▸ Matter ▸ {self.spec.install_menu_name}, or clear the stale stamp "
            f"({self._install_stamp_path()})."
        )

    def _node_version(self) -> Optional[str]:
        """Return the resolved node's version string (e.g. ``v22.18.0``), or None."""
        try:
            result = self._run([self.node_path, "--version"], capture_output=True,
                               text=True, check=False)
        except OSError:
            return None
        if result is None or result.returncode != 0:
            return None
        return (result.stdout or "").strip() or None

    def _install_stamp_path(self) -> str:
        return os.path.join(self.project_dir, INSTALL_NODE_STAMP)

    def _read_install_node_major(self) -> Optional[int]:
        try:
            with open(self._install_stamp_path(), "r", encoding="utf-8") as handle:
                return _node_major(handle.read().strip())
        except OSError:
            return None

    def _record_install_node(self) -> None:
        version = self._node_version()
        if not version:
            return
        try:
            with open(self._install_stamp_path(), "w", encoding="utf-8") as handle:
                handle.write(version + "\n")
        except OSError as exc:  # pragma: no cover - best-effort stamp
            self.logger.warning("could not record install node version: %s", exc)

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
        current = _parse_node_version(self._node_version() or "")
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
        self._record_install_node()
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
        """
        if not self._bootstrap():
            return False
        if plist_bytes is None:
            plist_bytes = self._plist_on_disk()
        if plist_bytes is not None:
            self._record_applied_digest(self._digest_of(plist_bytes))
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
        spot. Matches are SIGTERMed, we wait briefly, then SIGKILL any that ignore TERM. A
        port holder we can't identify as ours is never signalled, only warned about.

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
        for _ in range(6):
            self._sleep(0.25)
            if not self._running_server_pids(exclude_pid=exclude_pid):
                return len(pids)
        for pid in self._running_server_pids(exclude_pid=exclude_pid):
            self.logger.warning("%s pid %s ignored SIGTERM; sending SIGKILL",
                                self.spec.package, pid)
            self._signal(pid, "KILL")
        return len(pids)

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
            return pids
        self._warn_port_probe_unusable()
        return None

    def _warn_port_probe_unusable(self) -> None:
        """Say once that we cannot see who holds our port.

        Once per agent, not per call: the reap path probes several times per pass and
        a repeated warning would bury the log. Silence here is what issue #182 was
        made of, so this must never be downgraded to debug.
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

        * **Authoritative** — the port is held by a pid that is not ours, or is held by
          nobody at all. Either way what our WS client reaches is not our server.
        * **Advisory** — the port probe is unusable but our error log mentions
          ``EADDRINUSE``. Advisory-only by the same discipline #93 applied to the ABI
          stamp: the err log is append-only across restarts, so a hit may be from a
          conflict resolved weeks ago. Report it, never act on it.

        Call this for an **established** job only. A server that started seconds ago has
        not necessarily bound yet (~9s to the bind on jarvis), and this would read that
        startup window as "nothing listening".

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
            return (
                f"port {self.spec.port} is held by {others}, NOT by the "
                f"{self.spec.package} this plugin manages (pid {managed_pid}). The "
                f"plugin is therefore talking to a different server than the one it "
                f"starts, so its fabric and its devices may not be the ones you "
                f"configured. Stop the other process (or its LaunchAgent) and reload "
                f"the plugin."
            )
        # Past here `holders` is None ("the probe could not tell us") or [] ("nobody is
        # listening"). Both are ALSO what entirely benign states look like — a Mac
        # without a usable lsof, and a server still inside its ~9s startup window before
        # it binds — so neither may accuse anyone on its own. Require the err log to
        # corroborate: a freshly started server has logged no EADDRINUSE, which is what
        # stops a quick double reload from crying wolf.
        if not self._err_log_mentions_port_conflict():
            return None
        if holders is None:
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
            message = (
                f"{self.spec.package} (pid {managed_pid}) is running but NOTHING is "
                f"listening on port {self.spec.port}, so the plugin cannot reach it. "
                f"This is what a lost port race leaves behind: {self.spec.package} "
                f"logs a fatal '{EADDRINUSE_MARKER}' and keeps running without its "
                f"WebSocket server. Restart it from Plugins ▸ Matter, and see "
                f"{os.path.join(self.log_dir, self.spec.err_log)}."
            )
        return message

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
