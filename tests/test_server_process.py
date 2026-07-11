"""M1: LaunchAgent (PM-B) management — plist correctness + lifecycle commands.

Uses an injected fake subprocess runner and a temp HOME, so nothing touches the
real launchd or the real LaunchAgents directory.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

import os as _os

from server_process import LABEL, ServerProcess

# Captured before any monkeypatching so nvm tests can fake Homebrew "absent"
# while still letting real tmp_path files report as existing.
_real_exists = _os.path.exists


class FakeRunner:
    """Records launchctl invocations; returns a configurable returncode."""

    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="")

    def subcommands(self) -> list[str]:
        return [c[1] for c in self.calls if len(c) > 1]


@pytest.fixture
def prefs():
    return {"matterServerPort": "5580", "primaryInterface": "en0",
            "storagePath": "~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server"}


def _make_pkg(home, *, main: str | None = None, garbage: bool = False):
    """Create a fake node_modules/matter-server/package.json under ~/indigo-matter.

    With ``main`` set, writes a manifest with that entry. ``garbage=True`` writes a
    non-JSON file. With neither, no package.json is created (absent case).
    """
    pkg_dir = home / "indigo-matter" / "node_modules" / "matter-server"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    manifest = pkg_dir / "package.json"
    if garbage:
        manifest.write_text("{ not valid json ")
    elif main is not None:
        manifest.write_text(f'{{"main": "{main}"}}')
    return pkg_dir


def _make_entry(home, main: str = "dist/esm/MatterServer.js"):
    """Create the matter-server package + its entry JS so preflight() passes."""
    pkg_dir = _make_pkg(home, main=main)
    entry = pkg_dir / main
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// fake entry\n")
    return entry


@pytest.fixture
def sp(tmp_path, prefs, mock_logger):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    (home / "bin" / "node").write_text("#!/bin/sh\n")
    # a properly-installed matter-server so preflight() passes on the happy path
    _make_entry(home)
    return ServerProcess(
        prefs, mock_logger,
        home=str(home), npx_path=str(npx), runner=FakeRunner(),
    )


def test_program_arguments_run_node_on_package_main(sp):
    args = sp.program_arguments()
    # [0] is the node binary in the resolved bin dir, NOT npx
    assert args[0].endswith("/node")
    assert not args[0].endswith("/npx")
    # [1] is the resolved MatterServer.js entry point
    assert args[1] == sp._server_entry()
    assert args[1].endswith("/node_modules/matter-server/dist/esm/MatterServer.js")
    # full flag set still present with correct values
    assert args[args.index("--port") + 1] == "5580"
    assert args[args.index("--listen-address") + 1] == "127.0.0.1"
    assert "--storage-path" in args
    assert args[args.index("--primary-interface") + 1] == "en0"
    # the npx / --prefix / bare matter-server bin form is gone entirely
    assert "npx" not in args[0].rsplit("/", 1)[-1]
    assert "--prefix" not in args
    assert "matter-server" not in args  # no bare package-name arg
    # and never the wrong package name
    assert "matterjs-server" not in args
    assert "@matter-js/matterjs-server" not in args


def test_server_entry_reads_main_from_package_json(tmp_path, prefs, mock_logger):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    _make_pkg(home, main="dist/custom/Entry.js")
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    entry = sp._server_entry()
    assert entry.endswith("/node_modules/matter-server/dist/custom/Entry.js")


@pytest.mark.parametrize("kwargs", [{}, {"garbage": True}])
def test_server_entry_falls_back_when_manifest_absent_or_garbage(tmp_path, prefs, mock_logger, kwargs):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    if kwargs:
        _make_pkg(home, **kwargs)  # garbage package.json
    # else: no package.json at all
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    entry = sp._server_entry()
    assert entry.endswith("/node_modules/matter-server/dist/esm/MatterServer.js")


def test_program_arguments_bind_loopback_by_default(sp):
    # Security: with no listen-address pref, the managed server MUST bind loopback
    # only. matter-server binds ALL interfaces (and is unauthenticated) when
    # --listen-address is absent, so the flag must always be present and default to
    # 127.0.0.1.
    args = sp.program_arguments()
    assert "--listen-address" in args
    assert args[args.index("--listen-address") + 1] == "127.0.0.1"


def test_program_arguments_honour_custom_listen_address(tmp_path, mock_logger):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": "5580", "primaryInterface": "en0",
             "matterServerListenAddress": "192.168.1.50"}
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    args = sp.program_arguments()
    assert args[args.index("--listen-address") + 1] == "192.168.1.50"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_program_arguments_blank_listen_address_falls_back_to_loopback(tmp_path, mock_logger, blank):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": "5580", "primaryInterface": "en0",
             "matterServerListenAddress": blank}
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    args = sp.program_arguments()
    # never emit an empty listen address — that would re-expose all interfaces
    assert "--listen-address" in args
    idx = args.index("--listen-address")
    assert args[idx + 1] == "127.0.0.1"
    assert args[idx + 1].strip() != ""


def test_build_plist_is_valid_and_keepalive_on_crash(sp):
    spec = plistlib.loads(sp.build_plist())
    assert spec["Label"] == LABEL
    assert spec["RunAtLoad"] is True
    assert spec["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert spec["ProgramArguments"] == sp.program_arguments()
    assert spec["StandardOutPath"].endswith("matter-server.log")


def test_ensure_installed_writes_plist_and_loads(sp):
    sp.ensure_installed()
    # plist file written
    written = plistlib.loads(open(sp.plist_path, "rb").read())
    assert written["Label"] == LABEL
    # storage + log dirs created
    import os
    assert os.path.isdir(sp.storage_path)
    assert os.path.isdir(sp.log_dir)
    # attempted to bootstrap/load
    assert "bootstrap" in sp._run.subcommands() or "load" in sp._run.subcommands()


def test_uninstall_removes_plist_but_not_storage(sp):
    import os
    sp.ensure_installed()
    assert os.path.exists(sp.plist_path)
    assert os.path.isdir(sp.storage_path)
    sp.uninstall()
    assert not os.path.exists(sp.plist_path)
    # storage dir is sacred — must survive uninstall
    assert os.path.isdir(sp.storage_path)


def test_is_running_reflects_launchctl_returncode(tmp_path, prefs, mock_logger):
    home = tmp_path / "home"
    home.mkdir()
    running = ServerProcess(prefs, mock_logger, home=str(home),
                            npx_path="/opt/homebrew/bin/npx", runner=FakeRunner(returncode=0))
    assert running.is_running() is True
    stopped = ServerProcess(prefs, mock_logger, home=str(home),
                            npx_path="/opt/homebrew/bin/npx", runner=FakeRunner(returncode=1))
    assert stopped.is_running() is False


def test_restart_kickstarts(sp):
    sp._run = FakeRunner()
    sp.restart()
    assert "kickstart" in sp._run.subcommands()


def test_stop_boots_out_and_keeps_plist(sp):
    import os
    sp.ensure_installed()  # plist now on disk
    assert os.path.exists(sp.plist_path)
    sp._run = FakeRunner()
    ok = sp.stop()
    assert ok is True
    assert "bootout" in sp._run.subcommands()
    # stop() is the seam-half that must NOT remove the plist — start() reloads it
    assert os.path.exists(sp.plist_path)


def test_start_bootstraps_existing_plist(sp):
    sp.ensure_installed()  # plist present
    sp._run = FakeRunner()
    ok = sp.start()
    assert ok is True
    assert "bootstrap" in sp._run.subcommands()


def test_start_installs_when_plist_absent(sp):
    import os
    # no ensure_installed: plist absent
    assert not os.path.exists(sp.plist_path)
    sp._run = FakeRunner()  # all launchctl calls succeed → is_running() True
    ok = sp.start()
    assert ok is True
    # ensure_installed path: plist written + bootstrap/load attempted
    assert os.path.exists(sp.plist_path)
    assert "bootstrap" in sp._run.subcommands() or "load" in sp._run.subcommands()


def test_start_bootstrap_failure_returns_false(sp):
    # M7: start() on the existing-plist path must report the REAL bootstrap outcome,
    # not a hardcoded True — fabric restore (C1) relies on this.
    sp.ensure_installed()  # plist present
    sp._run = FakeRunner(returncode=1)  # bootstrap fails
    ok = sp.start()
    assert ok is False
    assert "bootstrap" in sp._run.subcommands()


def test_start_install_path_returns_is_running(sp):
    # M7: install path returns is_running() truthiness, not hardcoded True. With
    # launchctl failing, the install proceeds but is_running()/start() report False.
    import os
    assert not os.path.exists(sp.plist_path)
    sp._run = FakeRunner(returncode=1)  # bootstrap, load, AND print all fail
    ok = sp.start()
    assert ok is False  # is_running() with rc=1 → False
    assert os.path.exists(sp.plist_path)  # plist was still written
    assert "print" in sp._run.subcommands()  # is_running() was actually consulted


def test_npx_resolution_prefers_homebrew(tmp_path, prefs, mock_logger, monkeypatch):
    # neither candidate exists -> falls back to shutil.which, else homebrew default
    monkeypatch.setattr("server_process.os.path.exists", lambda p: p == "/opt/homebrew/bin/npx")
    sp = ServerProcess(prefs, mock_logger, home=str(tmp_path), runner=FakeRunner())
    assert sp.npx_path == "/opt/homebrew/bin/npx"


def test_restart_returns_false_and_reinstalls_on_kickstart_failure(sp):
    sp._run = FakeRunner(returncode=1)  # kickstart fails → fallback reinstall cycle
    ok = sp.restart()
    subs = sp._run.subcommands()
    assert "kickstart" in subs
    assert "bootstrap" in subs or "load" in subs  # reinstalled
    assert ok is False  # is_running() with rc=1 → False


# ----------------------------------------------------------------------
# nvm node resolution
# ----------------------------------------------------------------------
def _make_nvm(home, version: str, *, default_alias: str | None = None):
    """Create a fake ~/.nvm tree with a node version (and optional default alias)."""
    bindir = home / ".nvm" / "versions" / "node" / version / "bin"
    bindir.mkdir(parents=True)
    npx = bindir / "npx"
    npx.write_text("#!/bin/sh\n")
    (bindir / "node").write_text("#!/bin/sh\n")
    if default_alias is not None:
        alias_dir = home / ".nvm" / "alias"
        alias_dir.mkdir(parents=True, exist_ok=True)
        (alias_dir / "default").write_text(default_alias + "\n")
    return npx


def test_nvm_default_alias_resolution(tmp_path, prefs, mock_logger, monkeypatch):
    # No Homebrew npx, so nvm must win.
    monkeypatch.setattr("server_process.os.path.exists",
                        lambda p: _real_exists(p) and "homebrew" not in p and "/usr/local/" not in p)
    home = tmp_path / "home"
    home.mkdir()
    npx = _make_nvm(home, "v22.18.0", default_alias="v22")
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    assert sp.npx_path == str(npx)


def test_nvm_newest_version_fallback_when_no_default_alias(tmp_path, prefs, mock_logger, monkeypatch):
    monkeypatch.setattr("server_process.os.path.exists",
                        lambda p: _real_exists(p) and "homebrew" not in p and "/usr/local/" not in p)
    home = tmp_path / "home"
    home.mkdir()
    _make_nvm(home, "v22.9.0")          # lexically "higher" than v22.10.0
    newest = _make_nvm(home, "v22.10.0")  # but numerically the newest
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    assert sp.npx_path == str(newest)


def test_nvm_partial_alias_matches_highest_in_series(tmp_path, prefs, mock_logger, monkeypatch):
    monkeypatch.setattr("server_process.os.path.exists",
                        lambda p: _real_exists(p) and "homebrew" not in p and "/usr/local/" not in p)
    home = tmp_path / "home"
    home.mkdir()
    _make_nvm(home, "v20.5.0")
    _make_nvm(home, "v22.1.0")
    want = _make_nvm(home, "v22.18.0")
    # default alias "22" (no leading v, partial) -> highest v22.*
    (home / ".nvm" / "alias").mkdir(parents=True, exist_ok=True)
    (home / ".nvm" / "alias" / "default").write_text("22\n")
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    assert sp.npx_path == str(want)


def test_node_bin_dir_pref_wins_over_everything(tmp_path, prefs, mock_logger, monkeypatch):
    # Pretend Homebrew npx exists AND nvm exists; the explicit pref must still win.
    monkeypatch.setattr("server_process.os.path.exists", _real_exists)
    home = tmp_path / "home"
    home.mkdir()
    _make_nvm(home, "v22.18.0", default_alias="v22")
    # also fake-create homebrew-ish path under home so _real_exists sees something
    custom_bin = home / "custom" / "node" / "bin"
    custom_bin.mkdir(parents=True)
    custom_npx = custom_bin / "npx"
    custom_npx.write_text("#!/bin/sh\n")
    prefs = dict(prefs, nodeBinDir=str(custom_bin))
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    assert sp.npx_path == str(custom_npx)


def test_node_bin_dir_expands_tilde(tmp_path, prefs, mock_logger, monkeypatch):
    monkeypatch.setattr("server_process.os.path.exists", _real_exists)
    home = tmp_path / "home"
    home.mkdir()
    bindir = home / "mynode" / "bin"
    bindir.mkdir(parents=True)
    npx = bindir / "npx"
    npx.write_text("#!/bin/sh\n")
    prefs = dict(prefs, nodeBinDir="~/mynode/bin")
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    assert sp.npx_path == str(npx)


def test_homebrew_still_resolves_when_no_pref_or_nvm(tmp_path, prefs, mock_logger, monkeypatch):
    # Only the Apple-Silicon Homebrew npx "exists"; no nodeBinDir, no nvm tree.
    monkeypatch.setattr("server_process.os.path.exists", lambda p: p == "/opt/homebrew/bin/npx")
    home = tmp_path / "home"
    home.mkdir()
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    assert sp.npx_path == "/opt/homebrew/bin/npx"


def test_plist_path_env_contains_resolved_npx_dir(tmp_path, prefs, mock_logger, monkeypatch):
    monkeypatch.setattr("server_process.os.path.exists",
                        lambda p: _real_exists(p) and "homebrew" not in p and "/usr/local/" not in p)
    home = tmp_path / "home"
    home.mkdir()
    npx = _make_nvm(home, "v22.18.0", default_alias="v22")
    sp = ServerProcess(prefs, mock_logger, home=str(home), runner=FakeRunner())
    spec = plistlib.loads(sp.build_plist())
    path_env = spec["EnvironmentVariables"]["PATH"]
    assert path_env.startswith(str(npx.parent))
    assert path_env.endswith(":/usr/bin:/bin")


# ---------------------------------------------------------------------------
# preflight + ensure_installed guard (#89) and tail_error_log (#90)
# ---------------------------------------------------------------------------

def _sp_with(tmp_path, prefs, mock_logger, *, node: bool, entry: bool):
    """Build a ServerProcess whose node/entry presence is controlled for preflight."""
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    if node:
        (home / "bin" / "node").write_text("#!/bin/sh\n")
    if entry:
        _make_entry(home)
    return ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                         runner=FakeRunner())


def test_preflight_passes_when_node_and_entry_present(sp):
    assert sp.preflight() is None


def test_preflight_flags_missing_node(tmp_path, prefs, mock_logger):
    sp = _sp_with(tmp_path, prefs, mock_logger, node=False, entry=True)
    problem = sp.preflight()
    assert problem is not None and "node" in problem.lower()


def test_preflight_flags_missing_matter_server(tmp_path, prefs, mock_logger):
    sp = _sp_with(tmp_path, prefs, mock_logger, node=True, entry=False)
    problem = sp.preflight()
    assert problem is not None and "npm install matter-server" in problem


def test_ensure_installed_skips_plist_when_preflight_fails(tmp_path, prefs, mock_logger):
    sp = _sp_with(tmp_path, prefs, mock_logger, node=True, entry=False)
    sp.ensure_installed()
    assert not os.path.exists(sp.plist_path)          # no crash-looping job written
    assert "bootstrap" not in sp._run.subcommands()   # nothing loaded
    assert sp.logger.error.called                     # user got an actionable error


def test_ensure_installed_tears_down_stale_plist_on_preflight_fail(sp, tmp_path):
    sp.ensure_installed()                              # healthy: plist written
    assert os.path.exists(sp.plist_path)
    # matter-server disappears (e.g. user removed ~/indigo-matter); re-run
    import shutil
    shutil.rmtree(os.path.join(sp.project_dir, "node_modules"))
    sp._run = FakeRunner()
    sp.ensure_installed()
    assert not os.path.exists(sp.plist_path)           # stale job removed
    assert "bootout" in sp._run.subcommands()          # crash-loop stopped
    assert os.path.isdir(sp.storage_path)              # storage still sacred


def test_tail_error_log_returns_last_lines(sp):
    os.makedirs(sp.log_dir, exist_ok=True)
    with open(os.path.join(sp.log_dir, "matter-server.err.log"), "w") as handle:
        handle.write("".join(f"line {i}\n" for i in range(50)))
    tail = sp.tail_error_log(max_lines=5)
    assert tail is not None
    assert tail.splitlines() == ["line 45", "line 46", "line 47", "line 48", "line 49"]


def test_tail_error_log_none_when_absent_or_empty(sp):
    assert sp.tail_error_log() is None                 # no file yet
    os.makedirs(sp.log_dir, exist_ok=True)
    open(os.path.join(sp.log_dir, "matter-server.err.log"), "w").close()
    assert sp.tail_error_log() is None                 # empty file → None


# ---------------------------------------------------------------------------
# install() + node pinning + ABI (node-major) guard
# ---------------------------------------------------------------------------

class NodeVersionRunner(FakeRunner):
    """FakeRunner that answers `node --version` with a fixed version string."""

    def __init__(self, version: str = "v22.18.0", returncode: int = 0):
        super().__init__(returncode)
        self.version = version

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if len(cmd) >= 2 and cmd[0].endswith("node") and cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout=self.version + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="")


def _sp_with_tools(tmp_path, prefs, mock_logger, *, tools=("npx", "node", "npm"),
                   entry=False, runner=None):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    for tool in tools:
        (home / "bin" / tool).write_text("#!/bin/sh\n")
    if entry:
        _make_entry(home)
    return ServerProcess(prefs, mock_logger, home=str(home),
                         npx_path=str(home / "bin" / "npx"),
                         runner=runner or NodeVersionRunner())


def test_resolved_bin_dir_is_npx_parent(sp):
    assert sp.resolved_bin_dir == os.path.dirname(sp.npx_path)


def test_install_runs_npm_and_records_node_stamp(tmp_path, prefs, mock_logger):
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, runner=NodeVersionRunner("v22.18.0"))
    assert sp.install() is True
    npm_calls = [c for c in sp._run.calls if c[0].endswith("npm")]
    assert npm_calls, "expected an npm invocation"
    assert "install" in npm_calls[0] and "--prefix" in npm_calls[0]
    assert any("matter-server" in a for a in npm_calls[0])
    # stamps the node major so preflight can catch a later mismatch
    assert sp._read_install_node_major() == 22


def test_install_fails_when_npm_missing(tmp_path, prefs, mock_logger):
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, tools=("npx", "node"))  # no npm
    assert sp.install() is False
    assert sp.logger.error.called


class RaisingRunner(FakeRunner):
    """Raises OSError on the npm `install` call (exec failure); node --version works."""

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "install" in cmd:
            raise OSError("Permission denied")
        if len(cmd) >= 2 and cmd[0].endswith("node") and cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, stdout="v22.18.0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _write_stamp(sp, version):
    os.makedirs(sp.project_dir, exist_ok=True)
    with open(sp._install_stamp_path(), "w") as handle:
        handle.write(version)


def test_install_fails_on_npm_error(tmp_path, prefs, mock_logger):
    sp = _sp_with_tools(tmp_path, prefs, mock_logger,
                        runner=NodeVersionRunner(returncode=1))  # npm returns nonzero
    assert sp.install() is False
    assert sp._read_install_node_major() is None   # a FAILED install leaves no stamp


def test_install_returns_false_when_npm_exec_raises(tmp_path, prefs, mock_logger):
    # npm exists on disk but exec fails (permissions / wrong arch) → OSError branch.
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, runner=RaisingRunner())
    assert sp.install() is False
    assert sp.logger.error.called


# --- ABI check is now ADVISORY (abi_warning), never a fatal preflight block ---

def test_abi_warning_flags_node_major_mismatch(tmp_path, prefs, mock_logger):
    # installed with node 20, now resolving node 22
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, tools=("npx", "node"),
                        entry=True, runner=NodeVersionRunner("v22.5.0"))
    _write_stamp(sp, "v20.11.0\n")
    warning = sp.abi_warning()
    assert warning is not None and "20" in warning and "22" in warning
    assert sp.preflight() is None                  # NOT fatal — preflight still passes


def test_ensure_installed_warns_but_still_writes_plist_on_abi_mismatch(tmp_path, prefs, mock_logger):
    # a stale stamp must NOT block a possibly-working server or tear down its plist.
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, tools=("npx", "node"),
                        entry=True, runner=NodeVersionRunner("v22.5.0"))
    _write_stamp(sp, "v20.11.0\n")
    sp.ensure_installed()
    assert os.path.exists(sp.plist_path)           # server still gets to run
    assert sp.logger.warning.called                # but the user is warned


def test_abi_warning_none_when_majors_match(tmp_path, prefs, mock_logger):
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, tools=("npx", "node"),
                        entry=True, runner=NodeVersionRunner("v22.5.0"))
    _write_stamp(sp, "v22.1.0\n")
    assert sp.abi_warning() is None


def test_abi_warning_none_when_node_version_unreadable(tmp_path, prefs, mock_logger):
    # stamp present but `node --version` yields nothing → unknown current → no warning
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, tools=("npx", "node"),
                        entry=True, runner=FakeRunner())  # node --version → empty stdout
    _write_stamp(sp, "v20.11.0\n")
    assert sp.abi_warning() is None


def test_abi_warning_none_on_garbage_stamp(tmp_path, prefs, mock_logger):
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, tools=("npx", "node"),
                        entry=True, runner=NodeVersionRunner("v22.5.0"))
    _write_stamp(sp, "not-a-version\n")
    assert sp.abi_warning() is None                # corrupt stamp never false-warns


def test_abi_warning_and_preflight_none_without_stamp(sp):
    assert sp.abi_warning() is None
    assert sp.preflight() is None


# ---------------------------------------------------------------------------
# Blank-but-present prefs must fall back to defaults, not reach the CLI as ""
# (forum t=21404: `--port ""` → matter-server "Invalid integer:" crash-loop)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_program_arguments_blank_port_falls_back(tmp_path, mock_logger, blank):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": blank, "primaryInterface": "en0"}
    sp = ServerProcess(prefs, mock_logger, home=str(home),
                       npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    args = sp.program_arguments()
    assert args[args.index("--port") + 1] == "5580"   # never empty


@pytest.mark.parametrize("blank", ["", "   "])
def test_program_arguments_blank_primary_interface_falls_back(tmp_path, mock_logger, blank):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": "5580", "primaryInterface": blank}
    sp = ServerProcess(prefs, mock_logger, home=str(home),
                       npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    args = sp.program_arguments()
    assert args[args.index("--primary-interface") + 1] == "en0"


def test_program_arguments_blank_storage_falls_back(tmp_path, mock_logger):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": "5580", "primaryInterface": "en0", "storagePath": "   "}
    sp = ServerProcess(prefs, mock_logger, home=str(home),
                       npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    storage = sp.program_arguments()[sp.program_arguments().index("--storage-path") + 1]
    assert storage.strip() != "" and storage.endswith("/matter-server")


def _sp_port(tmp_path, mock_logger, prefs):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    sp = ServerProcess(prefs, mock_logger, home=str(home),
                       npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    args = sp.program_arguments()
    return args[args.index("--port") + 1]


def test_local_mode_forces_port_5580_even_with_stale_pref(tmp_path, mock_logger):
    # local WS client always dials 5580, so the server must too — a stale/other port
    # pref must not diverge them.
    port = _sp_port(tmp_path, mock_logger,
                    {"serverLocation": "local", "matterServerPort": "9999"})
    assert port == "5580"


def test_local_mode_forces_port_5580_when_blank(tmp_path, mock_logger):
    port = _sp_port(tmp_path, mock_logger,
                    {"serverLocation": "local", "matterServerPort": ""})
    assert port == "5580"


def test_remote_mode_honours_configured_port(tmp_path, mock_logger):
    port = _sp_port(tmp_path, mock_logger,
                    {"serverLocation": "remote", "matterServerPort": "5590"})
    assert port == "5590"


def test_install_puts_node_bin_dir_on_subprocess_path(tmp_path, prefs, mock_logger):
    # npm is a `#!/usr/bin/env node` script → node must be on PATH, or the install
    # fails with "env: node: No such file or directory" (caught live on jarvis).
    captured = {}

    class EnvCapturingRunner(NodeVersionRunner):
        def __call__(self, cmd, **kwargs):
            if "install" in cmd:
                captured["env"] = kwargs.get("env")
            return super().__call__(cmd, **kwargs)

    sp = _sp_with_tools(tmp_path, prefs, mock_logger, runner=EnvCapturingRunner())
    assert sp.install() is True
    assert captured["env"] is not None
    assert captured["env"]["PATH"].split(os.pathsep)[0] == sp.resolved_bin_dir


def test_install_blocks_on_too_old_node(tmp_path, prefs, mock_logger):
    # 1.2.2 needs Node >= 22.13.0; a too-old node must NOT "successfully" install an
    # unrunnable server (npm engines is advisory).
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, runner=NodeVersionRunner("v20.11.0"))
    assert sp.install() is False
    assert not any("install" in c for c in sp._run.calls)   # npm never ran
    assert sp.logger.error.called


def test_install_proceeds_when_node_meets_minimum(tmp_path, prefs, mock_logger):
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, runner=NodeVersionRunner("v22.13.0"))
    assert sp.install() is True                              # 22.13.0 is exactly the floor


def test_install_not_blocked_when_node_version_unreadable(tmp_path, prefs, mock_logger):
    # unknown current node must not false-block (never blocks on an unreadable version)
    sp = _sp_with_tools(tmp_path, prefs, mock_logger, runner=FakeRunner())  # node --version → ""
    assert sp.install() is True
