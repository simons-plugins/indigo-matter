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
    """Records launchctl invocations; returns a configurable returncode.

    A successful ``launchctl print`` also emits a ``pid = N`` line, because that is
    what launchd actually prints for a *running* job. Without it these fakes describe
    a job that is loaded but dead — a state the plugin now deliberately recovers from
    (#104 fault 2) — so tests meaning "healthy job" have to say so.
    """

    def __init__(self, returncode: int = 0, pid: int = 4242):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.pid = pid

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        stdout = ""
        if (len(cmd) >= 2 and cmd[0] == "launchctl" and cmd[1] == "print"
                and self.returncode == 0 and self.pid is not None):
            stdout = f"\tstate = running\n\tpid = {self.pid}\n"
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=stdout, stderr="")

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


def test_program_arguments_omit_test_net_dcl_by_default(sp):
    # Attestation is a security check: the flag must be opt-in, never emitted
    # because a pref key is simply absent.
    assert "--enable-test-net-dcl" not in sp.program_arguments()


@pytest.mark.parametrize("truthy", [True, "true", "True", "yes", "on", "1"])
def test_program_arguments_enable_test_net_dcl_when_pref_set(tmp_path, mock_logger, truthy):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": "5580", "primaryInterface": "en0",
             "enableTestNetDcl": truthy}
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    args = sp.program_arguments()
    # Bare flag, no value — matter-server declares it as "--enable-test-net-dcl [value]"
    # (optional value), so the flag alone means on. Assert the PROPERTY that makes the
    # bare form safe rather than a fixed position: nothing that isn't itself a flag may
    # follow it, or commander swallows that token as the option's value and the server
    # aborts at startup.
    idx = args.index("--enable-test-net-dcl")
    assert idx == len(args) - 1 or args[idx + 1].startswith("--")
    assert args[args.index("--primary-interface") + 1] == "en0"


@pytest.mark.parametrize("falsey", [False, "", "   ", 0, None, "false", "False", "no", "0"])
def test_program_arguments_falsey_test_net_dcl_stays_off(tmp_path, mock_logger, falsey):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    prefs = {"matterServerPort": "5580", "primaryInterface": "en0",
             "enableTestNetDcl": falsey}
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    assert "--enable-test-net-dcl" not in sp.program_arguments()


def test_toggling_test_net_dcl_changes_the_plist_digest(sp, prefs, mock_logger):
    # The applied-plist marker is what makes _apply_plist reload launchd, so the
    # two plists must differ — otherwise flipping the pref would leave the old
    # (flag-less) job running until some unrelated setting changed.
    # Derived from the SAME prefs dict as `sp` so the flag is the only difference;
    # building a fresh dict would isolate it only by coincidence.
    on = ServerProcess({**prefs, "enableTestNetDcl": True}, mock_logger,
                       home=sp.home, npx_path=sp.npx_path, runner=FakeRunner())
    # Compare the digests _apply_plist actually compares against the applied marker,
    # not just the bytes they are derived from.
    assert on._digest_of(on.build_plist()) != sp._digest_of(sp.build_plist())


def test_ensure_installed_writes_test_net_dcl_into_the_plist(sp, prefs, mock_logger):
    # End to end through the file: program_arguments() → build_plist() → disk. The
    # generic link is covered for the default instance elsewhere, but never with the
    # flag on, and the plist on disk is what launchd actually bootstraps.
    on = ServerProcess({**prefs, "enableTestNetDcl": True}, mock_logger,
                       home=sp.home, npx_path=sp.npx_path, runner=FakeRunner())
    on.ensure_installed()
    with open(on.plist_path, "rb") as handle:
        spec = plistlib.loads(handle.read())
    assert "--enable-test-net-dcl" in spec["ProgramArguments"]


def test_ensure_installed_warns_while_attestation_is_relaxed(sp, prefs, mock_logger):
    # The whole hazard is that this gets ticked once and forgotten, so the warning
    # must fire on EVERY startup, not only when the value changes.
    on = ServerProcess({**prefs, "enableTestNetDcl": True}, mock_logger,
                       home=sp.home, npx_path=sp.npx_path, runner=FakeRunner())
    on.ensure_installed()
    on.ensure_installed()
    relaxed = [c for c in mock_logger.warning.call_args_list
               if "--enable-test-net-dcl" in str(c)]
    assert len(relaxed) == 2


def test_ensure_installed_reports_whether_launchd_was_reloaded(sp):
    """The tri-state that stops the restart menu double-restarting the server.

    True  = launchd was (re)loaded, so a caller wanting a restart is already done;
    False = the current definition was left running untouched.
    """
    assert sp.ensure_installed() is True          # no applied marker yet → bootstrapped
    subs = sp._run.subcommands()
    assert "bootstrap" in subs
    # Second call: marker matches and the job is loaded, so it is deliberately left
    # alone. _managed_pid() finds no pid in the fake launchctl output, which is the
    # "can't tell healthy from orphan — don't touch it" path.
    assert sp.ensure_installed() is False


def test_ensure_installed_returns_none_when_preflight_fails(tmp_path, prefs, mock_logger):
    # No matter-server package installed → preflight fails, any stale plist is removed,
    # and there is nothing left to restart. Callers must be able to tell this apart from
    # "left it running".
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    (home / "bin" / "node").write_text("#!/bin/sh\n")
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    assert sp.ensure_installed() is None
    mock_logger.error.assert_called()


def test_restart_refuses_when_no_plist_exists(tmp_path, prefs, mock_logger):
    # Without this guard restart() bootstraps a missing file, gets a bare failure, and
    # logs "falling back to reinstall" while reinstalling nothing — burying the real
    # cause that ensure_installed already reported when it tore the plist down.
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    npx = home / "bin" / "npx"
    npx.write_text("#!/bin/sh\n")
    sp = ServerProcess(prefs, mock_logger, home=str(home), npx_path=str(npx),
                       runner=FakeRunner())
    assert sp.restart() is False
    assert "bootstrap" not in sp._run.subcommands()
    assert not [c for c in mock_logger.warning.call_args_list if "reinstall" in str(c)]


@pytest.mark.parametrize("junk", ["enabled", "y", "t", "please"])
def test_unrecognised_test_net_dcl_pref_reads_off_and_says_so(tmp_path, mock_logger, junk):
    # Fail-closed is right, but silently discarding the user's value is the one way this
    # setting can still evaporate with nothing in the log.
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    sp = ServerProcess({"matterServerPort": "5580", "enableTestNetDcl": junk}, mock_logger,
                       home=str(home), npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    assert sp.enable_test_net_dcl is False
    assert "--enable-test-net-dcl" not in sp.program_arguments()
    assert [c for c in mock_logger.warning.call_args_list if "unrecognised" in str(c)]


@pytest.mark.parametrize("known", [True, False, "true", "false", "off", ""])
def test_recognised_test_net_dcl_pref_is_silent(tmp_path, mock_logger, known):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    sp = ServerProcess({"matterServerPort": "5580", "enableTestNetDcl": known}, mock_logger,
                       home=str(home), npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    assert not [c for c in mock_logger.warning.call_args_list if "unrecognised" in str(c)]


def test_ensure_installed_silent_when_test_net_dcl_is_off(sp, mock_logger):
    sp.ensure_installed()
    assert not [c for c in mock_logger.warning.call_args_list if "--enable-test-net-dcl" in str(c)]


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


def test_restart_reloads_plist_not_kickstart(sp):
    # restart() must bootout + bootstrap so launchd re-reads the (corrected) plist —
    # kickstart -k would respawn the stale in-memory args (e.g. a pre-fix `--port ""`).
    sp.ensure_installed()
    sp._run = FakeRunner()
    assert sp.restart() is True
    subs = sp._run.subcommands()
    assert "bootout" in subs and "bootstrap" in subs
    assert "kickstart" not in subs


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


def test_restart_returns_false_and_reinstalls_on_reload_failure(sp):
    sp.ensure_installed()
    sp._run = FakeRunner(returncode=1)  # bootstrap fails → fallback reinstall cycle
    ok = sp.restart()
    subs = sp._run.subcommands()
    assert "bootout" in subs
    assert "bootstrap" in subs or "load" in subs  # reinstalled
    assert "kickstart" not in subs
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


# ---------------------------------------------------------------------------
# Stale launchd job: rewriting the plist FILE never updates an already-loaded
# job's cached args, so ensure_installed must reload when they diverge — this is
# what unsticks a pre-fix `--port ""` crash-loop (forum t=21404) on upgrade.
# ---------------------------------------------------------------------------

def test_ensure_installed_leaves_healthy_matching_job_untouched(sp):
    # First apply records the marker + bootstraps. A second reload with the SAME args
    # must NOT restart the server (device sessions are slow to re-establish).
    sp.ensure_installed()
    sp._run = FakeRunner()  # rc0 → is_running() True
    sp.ensure_installed()
    subs = sp._run.subcommands()
    assert "print" in subs          # is_running() consulted
    assert "bootout" not in subs    # healthy up-to-date job left running
    assert "bootstrap" not in subs


def test_ensure_installed_reloads_stale_job_when_no_marker(sp):
    # The stuck user: a job is already loaded (bootstrapped by an old plugin with the
    # buggy `--port ""`) but no applied-marker exists. The corrected plist must be
    # forced in via bootout + bootstrap so the crash-loop clears on the first reload.
    if os.path.exists(sp._applied_marker_path()):
        os.remove(sp._applied_marker_path())
    sp._run = FakeRunner()  # rc0 → is_running() True and launchctl calls succeed
    sp.ensure_installed()
    subs = sp._run.subcommands()
    assert "bootout" in subs        # stale in-memory args dropped
    assert "bootstrap" in subs      # corrected plist bootstrapped
    # marker now recorded so subsequent unchanged reloads won't needlessly restart
    assert sp._read_applied_digest() is not None


def test_ensure_installed_reloads_when_args_changed(sp):
    os.makedirs(os.path.dirname(sp._applied_marker_path()), exist_ok=True)
    with open(sp._applied_marker_path(), "w") as handle:
        handle.write("stale-digest-from-old-args\n")
    sp._run = FakeRunner()
    sp.ensure_installed()
    subs = sp._run.subcommands()
    assert "bootout" in subs and "bootstrap" in subs
    assert sp._read_applied_digest() != "stale-digest-from-old-args"


def test_applied_marker_lives_in_project_dir_and_survives_log_wipe(sp):
    # The marker must NOT live in the logs dir: losing it forces a needless restart of
    # a healthy server. It belongs beside .indigo-node in the (durable) project dir.
    import shutil
    sp.ensure_installed()
    marker = sp._applied_marker_path()
    assert os.path.dirname(marker) == sp.project_dir
    assert os.path.exists(marker)
    shutil.rmtree(sp.log_dir, ignore_errors=True)   # e.g. user clears plugin logs
    assert os.path.exists(marker)                    # marker unaffected
    assert sp._read_applied_digest() is not None


def test_apply_and_restart_record_digest_of_on_disk_plist(sp):
    # The marker must reflect the bytes launchd actually loaded (the file), not a
    # recomputed build_plist() that could drift from disk.
    import hashlib
    sp.ensure_installed()
    sp._run = FakeRunner()
    assert sp.restart() is True
    with open(sp.plist_path, "rb") as handle:
        disk_digest = hashlib.sha256(handle.read()).hexdigest()
    assert sp._read_applied_digest() == disk_digest


def test_apply_plist_warns_when_stale_job_wont_bootout(sp):
    # is_running() True but the stale job refuses to bootout — the user must be told the
    # old definition may persist, not have it fail silently.
    class PerCommandRunner(FakeRunner):
        def __init__(self, codes):
            super().__init__()
            self.codes = codes

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            sub = cmd[1] if len(cmd) > 1 else ""
            return subprocess.CompletedProcess(cmd, self.codes.get(sub, 0), stdout="", stderr="")

    sp.ensure_installed()
    with open(sp._applied_marker_path(), "w") as handle:
        handle.write("stale-digest\n")                # force a mismatch → reload path
    sp._run = PerCommandRunner({"print": 0, "bootout": 1, "bootstrap": 0})
    sp.logger.reset_mock()
    sp.ensure_installed()
    assert "bootout" in sp._run.subcommands()          # a stop was attempted
    assert sp.logger.warning.called                    # and its failure surfaced


# ---------------------------------------------------------------------------
# Orphan reaping: a matter-server can outlive its LaunchAgent and hold the storage
# lock, so every fresh start crashes with "Storage is locked by another process"
# (forum t=21404). bootout only stops the managed job — we must reap the stray.
# ---------------------------------------------------------------------------

class ProcRunner(FakeRunner):
    """Models `ps`, `kill` (simulating process exit), and `launchctl print` (pid)."""

    def __init__(self, ps_lines=None, print_pid=None, ignore_term=False,
                 omit_pid_line=False, garbled_pid=False, listen_pids=None,
                 job_arguments=None, returncode=0):
        super().__init__(returncode)
        self.ps_lines = list(ps_lines or [])
        self.print_pid = print_pid
        self.ignore_term = ignore_term
        # When True, `launchctl print` succeeds (job loaded) but emits no "pid = N" line
        # at all — launchd's "loaded but NOT running" state. The plugin treats this as a
        # dead job to revive (#104 fault 2), NOT as the safety-valve state.
        self.omit_pid_line = omit_pid_line
        # When True, a "pid =" line IS present but unparseable. THIS is the safety valve:
        # the job may be alive and we can't identify it, so nothing may be signalled.
        self.garbled_pid = garbled_pid
        # PIDs `lsof` reports as listening on the port.
        self.listen_pids = list(listen_pids or [])
        # ProgramArguments launchd reports for the live job (fault 3 drift detection).
        self.job_arguments = list(job_arguments or [])
        self.signals: list[tuple[str, str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd and cmd[0] == "ps":
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(self.ps_lines) + "\n", stderr="")
        if cmd and cmd[0] == "lsof":
            out = "".join(f"{pid}\n" for pid in self.listen_pids)
            # lsof exits 1 when nothing matches, which is not an error.
            return subprocess.CompletedProcess(cmd, 0 if self.listen_pids else 1, stdout=out, stderr="")
        if cmd and cmd[0] == "kill":
            sig, pid = cmd[1].lstrip("-"), cmd[2]
            self.signals.append((sig, pid))
            if sig == "KILL" or not self.ignore_term:  # process exits (KILL always; TERM unless stubborn)
                self.ps_lines = [ln for ln in self.ps_lines if ln.split()[0] != pid]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if len(cmd) >= 2 and cmd[0] == "launchctl" and cmd[1] == "print":
            if self.print_pid is None:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            out = "\tstate = running\n"
            if self.garbled_pid:
                out += "\tpid = (unknown)\n"
            elif not self.omit_pid_line:
                out += f"\tpid = {self.print_pid}\n"
            if self.job_arguments:
                out += "\targuments = {\n"
                out += "".join(f"\t\t{arg}\n" for arg in self.job_arguments)
                out += "\t}\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="")


def _sp_proc(tmp_path, mock_logger, runner, prefs=None):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True, exist_ok=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    prefs = prefs or {"serverLocation": "local", "matterServerPort": "5580",
                      "primaryInterface": "en0",
                      "storagePath": "~/Library/Application Support/x/matter-server"}
    return ServerProcess(prefs, mock_logger, home=str(home),
                         npx_path=str(home / "bin" / "npx"), runner=runner,
                         sleep=lambda *_a: None)


def _server_cmd(sp, pid, storage=None):
    pkg = os.path.join(sp.project_dir, "node_modules", "matter-server")
    return f"{pid} node {pkg}/dist/esm/MatterServer.js --storage-path {storage or sp.storage_path} --port 5580"


def test_running_server_pids_matches_ours_and_excludes_others(tmp_path, mock_logger):
    runner = ProcRunner()
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = [
        _server_cmd(sp, 545),                                   # ours (matches pkg + storage)
        "600 node /Users/x/other/app.js --storage-path /tmp",   # unrelated node process
        _server_cmd(sp, 700, storage="/some/other/storage"),    # our package, DIFFERENT storage
    ]
    assert sp._running_server_pids() == [545]
    assert sp._running_server_pids(exclude_pid=545) == []       # exclude spares the managed job


def _stray_cmd(pid, port=5580):
    """A matter-server from a DIFFERENT install path — invisible to storage-path matching."""
    return (f"{pid} node /opt/old-install/node_modules/matter-server/dist/esm/MatterServer.js "
            f"--storage-path /somewhere/else --port {port}")


def test_running_server_pids_matches_stray_holding_our_port(tmp_path, mock_logger):
    """#104 fault 1: the stray that actually blocked startup held the PORT, not our storage.

    `_running_server_pids` required BOTH our package dir and our --storage-path, so a
    matter-server started from another path was invisible — it kept serving on 5580 with
    stale args while every fresh instance died with EADDRINUSE and the plugin reported a
    healthy connection. Matching on the port as a fallback signal is what catches it.
    """
    runner = ProcRunner()
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = [_stray_cmd(66659)]
    runner.listen_pids = []
    assert sp._running_server_pids() == []        # storage-path matching alone is blind…
    runner.listen_pids = [66659]                  # …until we notice it holds our port
    assert sp._running_server_pids() == [66659]


def test_running_server_pids_never_reaps_a_foreign_port_holder(tmp_path, mock_logger):
    # Killing an unrelated listener would be a far worse failure than the one we fix.
    runner = ProcRunner(listen_pids=[900])
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = ["900 /usr/local/bin/some-other-server --port 5580"]
    assert sp._running_server_pids() == []


def test_reap_warns_when_our_port_is_held_by_something_else(tmp_path, mock_logger):
    # The one log line that would have ended the hour of misdiagnosis in #104.
    runner = ProcRunner(listen_pids=[900])
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = ["900 /usr/local/bin/some-other-server --port 5580"]
    assert sp.reap_orphan_servers() == 0          # nothing signalled…
    assert not runner.signals
    warning = " ".join(str(c) for c in sp.logger.warning.call_args_list)
    assert "900" in warning and "5580" in warning  # …but the conflict is surfaced


def test_reap_stays_quiet_when_the_port_holder_is_the_managed_job(tmp_path, mock_logger):
    # The healthy managed server obviously holds its own port — never warn about that.
    runner = ProcRunner(listen_pids=[5423])
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = [_server_cmd(sp, 5423)]
    assert sp.reap_orphan_servers(exclude_pid=5423) == 0
    assert not sp.logger.warning.called


def test_reap_orphan_servers_terminates_matching(tmp_path, mock_logger):
    runner = ProcRunner(ps_lines=None)
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = [_server_cmd(sp, 545)]
    assert sp.reap_orphan_servers() == 1
    assert ("TERM", "545") in runner.signals
    assert sp.logger.warning.called


def test_reap_escalates_to_sigkill_when_term_ignored(tmp_path, mock_logger):
    runner = ProcRunner(ignore_term=True)
    sp = _sp_proc(tmp_path, mock_logger, runner)
    runner.ps_lines = [_server_cmd(sp, 545)]
    assert sp.reap_orphan_servers() == 1
    assert ("TERM", "545") in runner.signals
    assert ("KILL", "545") in runner.signals


def test_reap_returns_zero_and_stays_quiet_when_none_running(tmp_path, mock_logger):
    runner = ProcRunner(ps_lines=[])
    sp = _sp_proc(tmp_path, mock_logger, runner)
    assert sp.reap_orphan_servers() == 0
    assert not runner.signals


def test_managed_pid_parses_launchctl_print(tmp_path, mock_logger):
    runner = ProcRunner(print_pid=5423)
    sp = _sp_proc(tmp_path, mock_logger, runner)
    assert sp._managed_pid() == 5423
    runner.print_pid = None
    assert sp._managed_pid() is None


def test_managed_job_parses_pid_and_arguments(tmp_path, mock_logger):
    runner = ProcRunner(print_pid=5423, job_arguments=["/bin/node", "--port", "5580"])
    sp = _sp_proc(tmp_path, mock_logger, runner)
    job = sp._managed_job()
    assert job["loaded"] is True
    assert job["pid"] == 5423 and job["pid_line"] is True
    assert job["arguments"] == ["/bin/node", "--port", "5580"]


def test_managed_job_distinguishes_dead_job_from_unparseable_pid(tmp_path, mock_logger):
    # These two states look identical through _managed_pid() (both None) but must drive
    # opposite decisions: revive the dead job, keep hands off the ambiguous one.
    dead = _sp_proc(tmp_path, mock_logger, ProcRunner(print_pid=5423, omit_pid_line=True))
    assert dead._managed_job() == {"loaded": True, "pid": None, "pid_line": False,
                                   "arguments": []}
    garbled = _sp_proc(tmp_path, mock_logger, ProcRunner(print_pid=5423, garbled_pid=True))
    job = garbled._managed_job()
    assert job["pid"] is None and job["pid_line"] is True


def _sp_proc_installed(tmp_path, mock_logger, runner):
    """A _sp_proc whose matter-server entry exists so ensure_installed's preflight passes."""
    sp = _sp_proc(tmp_path, mock_logger, runner)
    entry = os.path.join(sp.project_dir, "node_modules", "matter-server", "dist", "esm", "MatterServer.js")
    os.makedirs(os.path.dirname(entry), exist_ok=True)
    with open(entry, "w") as handle:
        handle.write("// fake\n")
    with open(os.path.join(sp.home, "bin", "node"), "w") as handle:
        handle.write("#!/bin/sh\n")
    return sp


def test_reload_leaves_healthy_job_untouched_when_no_orphan(tmp_path, mock_logger):
    runner = ProcRunner(print_pid=5423)  # managed job loaded; ps has no matter-server
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()                # records marker
    runner.calls.clear()
    sp.ensure_installed()                # reload: healthy + no orphan → no restart
    subs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" not in subs
    assert not runner.signals


def test_reload_warns_when_the_live_job_runs_stale_arguments(tmp_path, mock_logger):
    """#104 fault 3: a matching applied-digest proves the right plist was WRITTEN.

    launchd caches ProgramArguments at bootstrap, so the live job can keep serving with
    the arguments it was started with long after the plist changed. That is exactly how
    #104 presented — the plugin reported a healthy connection while matter-server ran the
    OLD args, and a freshly-enabled setting simply appeared not to work.
    """
    runner = ProcRunner(print_pid=5423)
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()                       # records the marker
    runner.job_arguments = ["/bin/node", "/old/MatterServer.js", "--port", "5580"]
    sp.logger.warning.reset_mock()
    assert sp.ensure_installed() is False       # healthy job still left running…
    warning = " ".join(str(c) for c in sp.logger.warning.call_args_list)
    assert "STALE" in warning                   # …but the drift is called out


def test_reload_stays_quiet_when_the_live_job_matches_the_plist(tmp_path, mock_logger):
    runner = ProcRunner(print_pid=5423)
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()
    runner.job_arguments = sp.program_arguments()   # live job matches what we'd launch
    sp.logger.warning.reset_mock()
    assert sp.ensure_installed() is False
    assert not sp.logger.warning.called


def test_reload_never_reaps_when_pid_line_is_unparseable(tmp_path, mock_logger):
    # Safety valve: a "pid =" line IS present but we can't read it, so the job may well
    # be alive. We must NOT reap (can't tell the healthy job from an orphan) and must
    # leave it untouched. Distinct from the loaded-but-dead case below.
    runner = ProcRunner(print_pid=5423, garbled_pid=True)
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()                # records marker
    runner.ps_lines = [_server_cmd(sp, 545)]   # an orphan is present…
    runner.calls.clear()
    runner.signals.clear()
    sp.ensure_installed()
    assert runner.signals == []          # …but we refuse to signal anything
    subs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" not in subs and "bootstrap" not in subs
    assert not any(c and c[0] == "ps" for c in runner.calls)   # reap never even scanned


def test_reload_revives_a_job_that_is_loaded_but_dead(tmp_path, mock_logger):
    """#104 fault 2: loaded with NO pid line means launchd is not running the job.

    matter-server exits 0 on a fatal startup error ("listen EADDRINUSE"), so
    KeepAlive {SuccessfulExit: false} reads it as a clean exit and deliberately never
    respawns it — the job stays dead indefinitely. Treating that as "healthy, hands
    off" is why a plugin reload could not recover it either. A reload must restart it.
    """
    runner = ProcRunner(print_pid=5423, omit_pid_line=True)  # loaded, but no "pid =" line
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()                # records marker (digest matches on next reload)
    runner.calls.clear()
    runner.signals.clear()
    assert sp.ensure_installed() is True          # reported as "launchd was reloaded"
    subs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" in subs and "bootstrap" in subs
    assert sp.logger.warning.called               # and the dead job was explained


def test_reload_reaps_orphan_and_restarts_when_it_blocks_a_matching_job(tmp_path, mock_logger):
    runner = ProcRunner(print_pid=5423)
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()                # records marker (digest matches on next reload)
    runner.ps_lines = [_server_cmd(sp, 545)]   # an orphan now holds the lock
    runner.calls.clear()
    runner.signals.clear()
    sp.ensure_installed()
    assert ("TERM", "545") in runner.signals               # orphan reaped (excluding pid 5423)
    subs = [c[1] for c in runner.calls if len(c) > 1]
    assert "bootout" in subs and "bootstrap" in subs        # and a clean restart forced


def test_restart_reaps_orphan_before_bootstrap(tmp_path, mock_logger):
    runner = ProcRunner(print_pid=None)
    sp = _sp_proc_installed(tmp_path, mock_logger, runner)
    sp.ensure_installed()                # writes the plist
    runner.ps_lines = [_server_cmd(sp, 545)]
    runner.signals.clear()
    assert sp.restart() is True
    assert ("TERM", "545") in runner.signals


def test_remove_package_uninstalls_via_npm_and_keeps_storage(sp):
    """E7: the clean reinstall is `npm uninstall <package>`, not `rm -rf node_modules`.

    The shared install root holds a second agent's package since E7, so the old
    wholesale delete took the bridge down with the controller. npm is preferred over
    deleting the directory because it also prunes the transitive deps nothing else
    needs — see LaunchAgent.remove_package.
    """
    os.makedirs(sp.storage_path, exist_ok=True)
    node_modules = os.path.join(sp.project_dir, "node_modules")
    assert os.path.isdir(node_modules)
    with open(os.path.join(sp.resolved_bin_dir, "npm"), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
    sp._run = FakeRunner()
    sp.remove_package()
    uninstalls = [call for call in sp._run.calls
                  if "uninstall" in call and "matter-server" in call]
    assert uninstalls, f"expected an npm uninstall, got {sp._run.calls}"
    assert "--prefix" in uninstalls[0] and sp.project_dir in uninstalls[0]
    assert os.path.isdir(sp.storage_path)        # storage is sacred — pairings survive
    assert "bootout" in sp._run.subcommands()    # server stopped first


def test_remove_package_falls_back_to_deleting_only_its_own_dir(sp, monkeypatch):
    """No npm (or npm refuses): delete OUR package dir — never the whole root.

    package-lock.json describes the entire install root, not one package, so it is
    left alone too: deleting it on behalf of one agent unpins the other's transitive
    dependency tree at its next install.
    """
    ours = os.path.join(sp.project_dir, "node_modules", "matter-server")
    sibling = os.path.join(sp.project_dir, "node_modules", "indigo-matter-bridge")
    os.makedirs(sibling, exist_ok=True)
    lock = os.path.join(sp.project_dir, "package-lock.json")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("{}")
    assert os.path.isdir(ours)
    sp._run = FakeRunner()
    monkeypatch.setattr(sp, "_npm_uninstall", lambda: False)
    sp.remove_package()
    assert not os.path.exists(ours)
    assert os.path.isdir(sibling)                # the other agent's package survives
    assert os.path.exists(lock)                  # and so does the shared lock file


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
