"""M1: LaunchAgent (PM-B) management — plist correctness + lifecycle commands.

Uses an injected fake subprocess runner and a temp HOME, so nothing touches the
real launchd or the real LaunchAgents directory.
"""
from __future__ import annotations

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
            "storagePath": "~/Library/Application Support/com.simon.indigo-matter/matter-server"}


@pytest.fixture
def sp(tmp_path, prefs, mock_logger):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    return ServerProcess(
        prefs, mock_logger,
        home=str(home), npx_path=str(npx), runner=FakeRunner(),
    )


def test_program_arguments_use_matter_server_via_npx(sp):
    args = sp.program_arguments()
    assert args[0].endswith("/npx")
    assert "--prefix" in args and "matter-server" in args
    # storage + interface flags present
    assert "--storage-path" in args
    assert args[args.index("--primary-interface") + 1] == "en0"
    assert args[args.index("--port") + 1] == "5580"
    # crucially: not the wrong package name
    assert "matterjs-server" not in args
    assert "@matter-js/matterjs-server" not in args


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
