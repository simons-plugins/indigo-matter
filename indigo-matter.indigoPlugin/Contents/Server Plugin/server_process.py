"""matter-server process management — LaunchAgent (PM-B).

Manages a launchd LaunchAgent that runs matter-server, per IMPLEMENTATION.md §1.4.
The ``matter-server`` npm package (v0.6.2) ships ``"bin": null`` — there is NO
``matter-server`` executable — so ``npx matter-server`` fails every time with
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

LABEL = "com.simon.indigo-matter"
DEFAULT_PROJECT_DIRNAME = "indigo-matter"   # ~/indigo-matter (npm install location)
NPX_CANDIDATES = ("/opt/homebrew/bin/npx", "/usr/local/bin/npx")
MATTER_SERVER_PACKAGE = "matter-server"
# Fallback entry point if the package's package.json is missing/unreadable. Matches
# matter-server v0.6.2's "main": "dist/esm/MatterServer.js".
DEFAULT_SERVER_ENTRY = "dist/esm/MatterServer.js"


def _expand(path: str, home: str) -> str:
    if path.startswith("~"):
        return home + path[1:]
    return path


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
    ) -> None:
        self.logger = logger
        self._run = runner
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
        self.port = str(prefs.get("matterServerPort", "5580"))
        self.primary_interface = str(prefs.get("primaryInterface", "en0"))
        # Address the matter-server WebSocket control API binds to. matter-server
        # v0.6.2 binds to ALL interfaces when no --listen-address is given, and the
        # control WS is UNAUTHENTICATED — so the safe default is loopback only. An
        # empty/whitespace pref must never leak through (that would re-expose all
        # interfaces), hence the explicit fall back to 127.0.0.1. This is distinct
        # from matterServerHost ("localhost"), which is the CLIENT connect target.
        self.listen_address = str(prefs.get("matterServerListenAddress", "127.0.0.1")).strip() or "127.0.0.1"
        self.project_dir = os.path.join(self.home, DEFAULT_PROJECT_DIRNAME)
        default_storage = f"~/Library/Application Support/{LABEL}/matter-server"
        self.storage_path = _expand(str(prefs.get("storagePath", default_storage)), self.home)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------
    @property
    def plist_path(self) -> str:
        return os.path.join(self.home, "Library", "LaunchAgents", f"{LABEL}.plist")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.home, "Library", "Logs", "indigo-matter")

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
        Falls back to ``dist/esm/MatterServer.js`` (v0.6.2's value) if the manifest
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
    def ensure_installed(self) -> None:
        """Create dirs, write the plist, and load it. Idempotent."""
        os.makedirs(self.storage_path, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.plist_path), exist_ok=True)
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
