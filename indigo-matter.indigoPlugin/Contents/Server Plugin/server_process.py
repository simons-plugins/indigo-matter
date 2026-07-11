"""matter-server process management — LaunchAgent (PM-B).

Manages a launchd LaunchAgent that runs matter-server, per IMPLEMENTATION.md §1.4.
The ``matter-server`` npm package ships ``"bin": null`` (true through at least 1.2.2)
— there is NO ``matter-server`` executable — so ``npx matter-server`` fails with
"could not determine executable to run", and launchd KeepAlive-respawns it forever.
We therefore launch node directly on the package main (``dist/esm/MatterServer.js``,
read from the package's ``package.json``), exactly like the working hand-rolled
run.sh. This is the recommended process-management approach because the server
survives Indigo plugin reloads (frequent during development) without restarting —
it holds device sessions that are slow to re-establish.

Gated by the ``manageLaunchAgent`` plugin pref (default off): when off, the plugin
simply connects to a matter-server the user starts themselves. The final PM choice
+ its ADR are deferred to M10 per the PRD.

The storage directory is sacred — losing it loses the fabric and all pairings. This
module creates it but NEVER deletes it; uninstall removes only the LaunchAgent.

Paths and the subprocess runner are injectable so the whole module is unit-testable
without touching the real launchd or filesystem.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
from typing import Any, Callable, Optional

LABEL = "com.simons-plugins.indigo-matter"
DEFAULT_PROJECT_DIRNAME = "indigo-matter"   # ~/indigo-matter (npm install location)
NPX_CANDIDATES = ("/opt/homebrew/bin/npx", "/usr/local/bin/npx")
MATTER_SERVER_PACKAGE = "matter-server"
# Version installed by install(). Pinned (exact, not caret) for reproducibility — the
# package is fast-moving pre-1.0-style Alpha/Beta. Kept in one place so a version bump
# is a one-line change (matches docs/INSTALL.md). NOTE: 1.2.2 requires Node >= 22.13.0.
DEFAULT_INSTALL_SPEC = "matter-server@1.2.2"
# matter-server 1.2.2's package.json declares engines node >= 22.13.0. npm's engines
# check is advisory by default (exits 0 on an older node), so install() gates on this
# itself — otherwise a too-old node "successfully" installs an unrunnable server.
MIN_NODE_VERSION = (22, 13)
# Fallback entry point if the package's package.json is missing/unreadable. Matches the
# "main" of both 0.6.x and 1.2.x ("dist/esm/MatterServer.js"); _server_entry() reads the
# real value from the installed package.json and only falls back to this.
DEFAULT_SERVER_ENTRY = "dist/esm/MatterServer.js"
# Records the node version the package was installed with, so preflight can catch an
# install-node vs run-node mismatch (native-binding ABI crash) before it crash-loops.
INSTALL_NODE_STAMP = ".indigo-node"


def _expand(path: str, home: str) -> str:
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


class ServerProcess:
    """Install / control the matter-server LaunchAgent."""

    def __init__(
        self,
        prefs: dict,
        logger: Any,
        *,
        home: Optional[str] = None,
        npx_path: Optional[str] = None,
        runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
        exists: Callable[[str], bool] = os.path.exists,
    ) -> None:
        self.logger = logger
        self._run = runner
        # Injectable existence check keeps preflight() unit-testable without the FS.
        self._exists = exists
        self.home = home or os.path.expanduser("~")
        # Optional explicit override: directory containing node/npx. nvm users can
        # pin a specific version here (e.g. ~/.nvm/versions/node/v22.18.0/bin);
        # blank means auto-detect (Homebrew → nvm → PATH).
        raw_bin_dir = str(prefs.get("nodeBinDir", "") or "").strip()
        self.node_bin_dir = _expand(raw_bin_dir, self.home) if raw_bin_dir else ""
        self.npx_path = npx_path or self._resolve_npx()
        # The node interpreter lives in the same bin dir as npx (Homebrew + nvm both
        # ship node and npx side-by-side). We launch node directly because the
        # matter-server npm package exposes no bin executable (see module docstring).
        self.node_path = os.path.join(os.path.dirname(self.npx_path), "node")
        # Port. In LOCAL mode the WS client hardcodes ws://localhost:5580/ws, so the
        # server must listen on 5580 too — the matterServerPort field is hidden in
        # local mode (its defaultValue never applies) and would otherwise reach the
        # CLI as "" → matter-server "Invalid integer:" crash-loop, or as a stale value
        # that diverges from what the client dials. Force 5580 in local mode; in remote
        # mode honour the pref but still fall back on blank (prefs.get only defaults on
        # an ABSENT key, not a present-but-empty one).
        if str(prefs.get("serverLocation") or "").strip().lower() == "local":
            self.port = "5580"
        else:
            self.port = str(prefs.get("matterServerPort") or "").strip() or "5580"
        self.primary_interface = str(prefs.get("primaryInterface") or "").strip() or "en0"
        # Address the matter-server WebSocket control API binds to. It binds to ALL
        # interfaces when no --listen-address is given, and the
        # control WS is UNAUTHENTICATED — so the safe default is loopback only. An
        # empty/whitespace pref must never leak through (that would re-expose all
        # interfaces), hence the explicit fall back to 127.0.0.1. This is distinct
        # from matterServerHost ("localhost"), which is the CLIENT connect target.
        self.listen_address = str(prefs.get("matterServerListenAddress", "127.0.0.1")).strip() or "127.0.0.1"
        self.project_dir = os.path.join(self.home, DEFAULT_PROJECT_DIRNAME)
        default_storage = f"~/Library/Application Support/{LABEL}/matter-server"
        raw_storage = str(prefs.get("storagePath") or "").strip() or default_storage
        self.storage_path = _expand(raw_storage, self.home)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def plist_path(self) -> str:
        return os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")

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

    def _server_entry(self) -> str:
        """Absolute path to the matter-server package main (the JS to run with node).

        Reads ``main`` from ``{project_dir}/node_modules/matter-server/package.json``
        so the launch adapts automatically if the package bumps its entry path.
        Falls back to ``dist/esm/MatterServer.js`` (the 0.6.x/1.2.x value) if the manifest
        is missing, unreadable, or malformed.
        """
        pkg_dir = os.path.join(self.project_dir, "node_modules", MATTER_SERVER_PACKAGE)
        main = DEFAULT_SERVER_ENTRY
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
        # node <package main> … — NOT `npx matter-server`: the matter-server npm
        # package ships "bin": null, so npx cannot resolve an executable and the
        # LaunchAgent respawn-loops with "could not determine executable to run".
        return [
            self.node_path,
            self._server_entry(),
            "--port", self.port,
            "--listen-address", self.listen_address,
            "--storage-path", self.storage_path,
            "--primary-interface", self.primary_interface,
        ]

    def build_plist(self) -> bytes:
        out_log = os.path.join(self.log_dir, "matter-server.log")
        err_log = os.path.join(self.log_dir, "matter-server.err.log")
        # dirname(npx) is the resolved node bin dir (Homebrew/nvm ship node + npx
        # together). We invoke node directly because the matter-server package
        # exposes no bin; prepending this dir to launchd's restricted PATH lets the
        # spawned node find its own co-located libexec/helpers. /usr/bin:/bin stays
        # appended.
        npx_dir = os.path.dirname(self.npx_path)
        spec = {
            "Label": LABEL,
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
        """Return a FATAL reason matter-server can't launch, else None.

        Guards the two failures that otherwise surface only as a launchd crash-loop
        (and a bare "connection refused" at the WS client): a missing node
        interpreter, or the matter-server npm package not being installed. Both are
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
                f"the matter-server package is not installed ({entry} is missing). "
                f"Use the plugin menu: Plugins ▸ Matter ▸ Install/update matter-server "
                f"(or run 'npm install {DEFAULT_INSTALL_SPEC}' in {self.project_dir} "
                f"with the same node, {self.node_path}), then restart the plugin."
            )
        return None

    def abi_warning(self) -> Optional[str]:
        """Return an ADVISORY warning if node's major differs from the install stamp.

        A mismatch *may* mean the package's native bindings won't load — but the stamp
        is only written by this plugin's install(), so a user who reinstalls
        out-of-band (Terminal npm) leaves a STALE stamp behind. Blocking on it would
        refuse a perfectly good server, so this is a warning only: let the server try,
        and if it really is a mismatch it crash-loops and matter-server.err.log
        surfaces the cause. Never fires when either version is unknown.
        """
        stamped = self._read_install_node_major()
        if stamped is None:
            return None
        current = _node_major(self._node_version())
        if current is None or stamped == current:
            return None
        return (
            f"matter-server was installed with Node {stamped}.x but the resolved node "
            f"({self.node_path}) is {current}.x. If it fails to start, reinstall via "
            f"Plugins ▸ Matter ▸ Install/update matter-server, or clear the stale stamp "
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
        """Return the last ``max_lines`` of matter-server.err.log, else None.

        Surfaces WHY the launchd-managed server keeps dying (module-not-found,
        native-binding ABI mismatch, a bad ``--flag``, …) where the WS client only
        sees "connection refused". Returns None if the log is absent or empty.
        """
        path = os.path.join(self.log_dir, "matter-server.err.log")
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

    def install(self, spec: str = DEFAULT_INSTALL_SPEC) -> bool:
        """npm-install the matter-server package with the resolved node. Idempotent.

        Installs into ``~/indigo-matter`` using the ``npm`` co-located with the node
        this instance resolved — so the package's native deps are built for the SAME
        node the LaunchAgent will run (the install/run match that avoids ABI
        crash-loops). Records that node's version for :meth:`abi_warning`. Captures
        npm's output and logs it on failure; returns True on success. Blocking —
        callers should run it off the Indigo main thread.
        """
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
                spec, ".".join(map(str, MIN_NODE_VERSION)), self.node_path,
                ".".join(map(str, current)),
            )
            return False
        os.makedirs(self.project_dir, exist_ok=True)
        self.logger.info("Installing %s into %s (node: %s) — this can take a minute…",
                         spec, self.project_dir, self.node_path)
        # npm is a `#!/usr/bin/env node` script, so `node` must be on PATH — but the
        # plugin's subprocess env (under launchd) usually isn't, which fails with
        # "env: node: No such file or directory". Prepend the resolved node bin dir.
        env = dict(os.environ)
        env["PATH"] = self.resolved_bin_dir + os.pathsep + env.get("PATH", "")
        try:
            result = self._run([npm, "install", "--prefix", self.project_dir, spec],
                               capture_output=True, text=True, check=False, env=env)
        except OSError as exc:
            self.logger.error("matter-server install could not start: %s", exc)
            return False
        if result is None or result.returncode != 0:
            # Combine both streams (npm splits the cause across them) and keep the
            # HEAD — npm front-loads the real error; the tail is boilerplate footer.
            if result is None:
                detail = "npm unavailable"
            else:
                detail = "\n".join(p for p in ((result.stdout or "").strip(),
                                                (result.stderr or "").strip()) if p)
            self.logger.error("matter-server install failed:\n%s", (detail or "no output")[:3000])
            return False
        self._record_install_node()
        self.logger.info("matter-server installed.")
        return True

    def ensure_installed(self) -> None:
        """Create dirs, write the plist, and load it. Idempotent.

        Runs :meth:`preflight` first. A launchd job pointing at a missing node or an
        uninstalled matter-server can only crash-loop (``KeepAlive`` respawns it) and
        the WS client sees a bare "connection refused". So on a preflight failure:
        log an actionable error, tear down any stale plist to stop an existing
        crash-loop, and do NOT (re)write it.
        """
        os.makedirs(self.storage_path, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.plist_path), exist_ok=True)
        problem = self.preflight()
        if problem:
            self.logger.error("matter-server cannot start: %s", problem)
            if self._exists(self.plist_path):
                self.uninstall()  # stop an existing crash-loop; leaves storage intact
            return
        abi = self.abi_warning()
        if abi:
            self.logger.warning("matter-server: %s", abi)  # advisory — do NOT block
        with open(self.plist_path, "wb") as handle:
            handle.write(self.build_plist())
        self.logger.info("Wrote LaunchAgent %s", self.plist_path)
        # bootstrap (modern) with a load fallback for older macOS
        if self._bootstrap():
            return
        result = self._launchctl("load", self.plist_path)
        if result is None or result.returncode != 0:
            detail = result.stderr.strip() if result is not None else "launchctl unavailable"
            self.logger.error(
                "Failed to load matter-server LaunchAgent (%s); the server may not be "
                "running. Start it manually or check %s",
                detail, self.plist_path,
            )

    def uninstall(self) -> None:
        """Unload and remove the LaunchAgent. NEVER touches the storage dir."""
        if not self._bootout():
            self._launchctl("unload", self.plist_path)
        try:
            os.remove(self.plist_path)
            self.logger.info("Removed LaunchAgent %s (storage left intact)", self.plist_path)
        except FileNotFoundError:
            pass

    def stop(self) -> bool:
        """Stop matter-server (bootout) but keep the plist so ``start`` can reload it.

        Part of the ``server_control`` seam used by fabric restore. Returns True
        if the bootout succeeded.
        """
        return self._bootout()

    def start(self) -> bool:
        """Start matter-server: reload the existing plist, or install it if absent.

        Part of the ``server_control`` seam used by fabric restore. Returns the
        REAL outcome so callers (notably fabric restore) are never told the server
        started when it did not: a successful ``bootstrap`` on the existing-plist
        path, or ``is_running()`` after the install path (``ensure_installed`` logs
        its own launchctl failure but returns None, so we verify independently).
        """
        if os.path.exists(self.plist_path):
            return self._bootstrap()
        self.ensure_installed()
        return self.is_running()

    def restart(self) -> bool:
        """Kick the agent so matter-server respawns. Returns True on success."""
        result = self._launchctl("kickstart", "-k", f"gui/{os.getuid()}/{LABEL}")
        if result is not None and result.returncode == 0:
            return True
        # fall back to a full unload/load cycle
        self.logger.warning("matter-server kickstart failed; falling back to reinstall")
        self.uninstall()
        self.ensure_installed()
        return self.is_running()

    def is_running(self) -> bool:
        result = self._launchctl("print", f"gui/{os.getuid()}/{LABEL}")
        return bool(result is not None and result.returncode == 0)

    # ------------------------------------------------------------------
    # launchctl helpers
    # ------------------------------------------------------------------
    def _bootstrap(self) -> bool:
        result = self._launchctl("bootstrap", f"gui/{os.getuid()}", self.plist_path)
        return bool(result is not None and result.returncode == 0)

    def _bootout(self) -> bool:
        result = self._launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        return bool(result is not None and result.returncode == 0)

    def _launchctl(self, *args: str) -> Optional["subprocess.CompletedProcess"]:
        cmd = ["launchctl", *args]
        try:
            return self._run(cmd, capture_output=True, text=True, check=False)
        except OSError as exc:
            self.logger.warning("launchctl %s failed: %s", args[0], exc)
            return None
