"""matter-server process management — LaunchAgent (PM-B).

Manages a launchd LaunchAgent that runs matter-server, per IMPLEMENTATION.md §1.4
(corrected: npm package ``matter-server``, invoked via ``npx --prefix``). This is
the recommended process-management approach because the server survives Indigo
plugin reloads (frequent during development) without restarting — it holds device
sessions that are slow to re-establish.

Gated by the ``manageLaunchAgent`` plugin pref (default off): when off, the plugin
simply connects to a matter-server the user starts themselves. The final PM choice
+ its ADR are deferred to M10 per the PRD.

The storage directory is sacred — losing it loses the fabric and all pairings. This
module creates it but NEVER deletes it; uninstall removes only the LaunchAgent.

Paths and the subprocess runner are injectable so the whole module is unit-testable
without touching the real launchd or filesystem.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from typing import Any, Callable, Optional

LABEL = "com.simon.indigo-matter"
DEFAULT_PROJECT_DIRNAME = "indigo-matter"   # ~/indigo-matter (npm install location)
NPX_CANDIDATES = ("/opt/homebrew/bin/npx", "/usr/local/bin/npx")


def _expand(path: str, home: str) -> str:
    if path.startswith("~"):
        return home + path[1:]
    return path


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
        self.npx_path = npx_path or self._resolve_npx()
        self.port = str(prefs.get("matterServerPort", "5580"))
        self.primary_interface = str(prefs.get("primaryInterface", "en0"))
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
        for candidate in NPX_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        found = shutil.which("npx")
        if found:
            return found
        # Fall back to the Apple-Silicon default; ensure_installed will log if absent.
        return NPX_CANDIDATES[0]

    # ------------------------------------------------------------------
    # Plist
    # ------------------------------------------------------------------
    def program_arguments(self) -> list[str]:
        return [
            self.npx_path,
            "--prefix", self.project_dir,
            "matter-server",
            "--port", self.port,
            "--storage-path", self.storage_path,
            "--primary-interface", self.primary_interface,
        ]

    def build_plist(self) -> bytes:
        out_log = os.path.join(self.log_dir, "matter-server.log")
        err_log = os.path.join(self.log_dir, "matter-server.err.log")
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
