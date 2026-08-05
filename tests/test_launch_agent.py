"""AgentSpec / LaunchAgent: the parts that only matter once there are TWO agents.

``tests/test_server_process.py`` covers the machinery itself (it exercises the same
code through the controller specialisation, unchanged by the extraction). This file
pins what the extraction ADDS: that an agent's identity is carried by its spec, that
two agents sharing one ``project_dir`` cannot clobber each other's applied-plist
digest (which would bootout a healthy job on every reload), and that each reaps only
its own orphans.

Every agent here is built against a ``tmp_path`` home. Never construct one against the
real ``$HOME``: ``ensure_installed()``'s preflight-failure path calls ``uninstall()``,
which would delete the developer's live LaunchAgent (docs/HANDOVER.md).
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import plistlib

import pytest

from launch_agent import INSTALL_NODE_STAMP, AgentSpec, LaunchAgent
from server_process import APPLIED_PLIST_MARKER, LABEL, ServerProcess

from test_server_process import FakeRunner, ProcRunner


def _argv(agent):
    """A minimal argv hook: enough to build a valid, distinct plist."""
    return [agent.node_path, agent._server_entry(), "--port", str(agent.spec.port)]


def _spec(label: str, package: str, storage: str, port: int | None = None) -> AgentSpec:
    return AgentSpec(
        label=label,
        package=package,
        install_spec=f"{package}@1.0.0",
        default_entry="dist/Main.js",
        storage_path=storage,
        out_log=f"{package}.log",
        err_log=f"{package}.err.log",
        argv=_argv,
        port=port,
    )


def _agent(home, spec, mock_logger, runner=None) -> LaunchAgent:
    """A LaunchAgent whose node + package entry exist, so preflight() passes."""
    bindir = home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "npx").write_text("#!/bin/sh\n")
    (bindir / "node").write_text("#!/bin/sh\n")
    entry = home / "indigo-matter" / "node_modules" / spec.package / "dist" / "Main.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// fake entry\n")
    return LaunchAgent(spec, {}, mock_logger, home=str(home),
                       npx_path=str(bindir / "npx"), runner=runner or FakeRunner(),
                       sleep=lambda *_a: None)


# ---------------------------------------------------------------------------
# AgentSpec
# ---------------------------------------------------------------------------

def test_agent_spec_carries_the_agents_whole_identity():
    spec = _spec("com.example.thing", "thing", "/tmp/thing-store", port=5541)
    assert (spec.label, spec.package, spec.install_spec) == (
        "com.example.thing", "thing", "thing@1.0.0")
    assert spec.default_entry == "dist/Main.js"
    assert spec.storage_path == "/tmp/thing-store"
    assert (spec.out_log, spec.err_log) == ("thing.log", "thing.err.log")
    assert spec.port == 5541


def test_agent_spec_is_frozen():
    # The identity must not drift under a loaded launchd job — a changed label or
    # storage path mid-life orphans the running process.
    spec = _spec("com.example.thing", "thing", "/tmp/thing-store")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.label = "com.example.other"


def test_agent_spec_port_is_optional():
    # An agent that listens on nothing has no EADDRINUSE contention to police.
    assert _spec("com.example.thing", "thing", "/tmp/s").port is None


def test_applied_marker_defaults_to_a_per_label_name():
    # Two agents share project_dir, so the default MUST be per-agent.
    a = _spec("com.example.a", "pkg-a", "/tmp/a")
    b = _spec("com.example.b", "pkg-b", "/tmp/b")
    assert a.applied_marker_name == ".launchagent-com.example.a.sha256"
    assert a.applied_marker_name != b.applied_marker_name


def test_controller_keeps_the_legacy_marker_filename(tmp_path, mock_logger):
    """Back-compat: every existing install already has `.launchagent.sha256`.

    Renaming it (or migrating it) risks reading as "no marker recorded", which forces a
    bootout + bootstrap of a healthy server and drops every device's CASE session. The
    controller therefore pins the original name; only later agents take the per-label
    default.
    """
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "npx").write_text("#!/bin/sh\n")
    sp = ServerProcess({}, mock_logger, home=str(home),
                       npx_path=str(home / "bin" / "npx"), runner=FakeRunner())
    assert APPLIED_PLIST_MARKER == ".launchagent.sha256"
    assert sp._applied_marker_path() == os.path.join(sp.project_dir, ".launchagent.sha256")
    assert sp.spec.label == LABEL


# ---------------------------------------------------------------------------
# Per-agent stamp isolation (the reason the extraction can't just share one marker)
# ---------------------------------------------------------------------------

def test_two_agents_on_one_project_dir_keep_separate_digests(tmp_path, mock_logger):
    """A shared marker would make each agent see the other's digest as "stale".

    Both agents install into the same ~/indigo-matter, so with one marker file the
    second agent's write would overwrite the first's digest — and the first agent's
    next reload would bootout + bootstrap a perfectly healthy job, every time.
    """
    home = tmp_path / "home"
    first = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")), mock_logger)
    second = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store")), mock_logger)
    assert first.project_dir == second.project_dir          # the shared install root
    assert first._applied_marker_path() != second._applied_marker_path()

    assert first.ensure_installed() is True                 # no marker yet → bootstrap
    assert second.ensure_installed() is True

    # Both markers survive, and each records ITS OWN plist.
    assert first._read_applied_digest() == first._digest_of(first.build_plist())
    assert second._read_applied_digest() == second._digest_of(second.build_plist())

    # …so the first agent's next reload leaves its healthy job alone.
    first._run = FakeRunner()
    assert first.ensure_installed() is False
    assert "bootout" not in first._run.subcommands()


def test_agents_write_distinct_plists_and_logs(tmp_path, mock_logger):
    home = tmp_path / "home"
    first = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")), mock_logger)
    second = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store")), mock_logger)
    assert first.plist_path != second.plist_path
    first.ensure_installed()
    second.ensure_installed()
    a_spec = plistlib.loads(open(first.plist_path, "rb").read())
    b_spec = plistlib.loads(open(second.plist_path, "rb").read())
    assert a_spec["Label"] == "com.example.a" and b_spec["Label"] == "com.example.b"
    assert a_spec["StandardOutPath"].endswith("pkg-a.log")
    assert b_spec["StandardErrorPath"].endswith("pkg-b.err.log")
    # Shared log DIRECTORY, distinct filenames.
    assert first.log_dir == second.log_dir


def test_install_node_stamp_is_deliberately_shared(tmp_path, mock_logger):
    # One node_modules, installed by one node, run by every agent — a per-agent stamp
    # would claim they can diverge, which is the ABI crash this guards against.
    home = tmp_path / "home"
    first = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")), mock_logger)
    second = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store")), mock_logger)
    assert first._install_stamp_path() == second._install_stamp_path()
    assert first._install_stamp_path().endswith(INSTALL_NODE_STAMP)
    with open(first._install_stamp_path(), "w", encoding="utf-8") as handle:
        handle.write("v22.18.0\n")
    assert second._read_install_node_major() == 22


def test_preflight_fail_returns_none_and_spares_the_sibling(tmp_path, mock_logger):
    """The tri-state's None leg, pinned at the GENERIC level with a sibling present.

    ensure_installed() on a failed preflight must return None, log an error, and
    remove ITS OWN stale plist — via uninstall(), which is exactly where a careless
    implementation would tear down more than its own label. The sibling's plist
    survives and no launchctl call names the sibling's label.
    """
    home = tmp_path / "home"
    healthy = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")), mock_logger)
    assert healthy.ensure_installed() is True

    broken = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store")), mock_logger)
    assert broken.ensure_installed() is True        # healthy first: leaves a plist behind
    os.remove(os.path.join(broken.project_dir, "node_modules", "pkg-b", "dist", "Main.js"))
    broken._run = FakeRunner()
    assert broken.ensure_installed() is None        # preflight now fails
    mock_logger.error.assert_called()
    assert not os.path.exists(broken.plist_path)    # its own stale plist removed
    assert os.path.exists(healthy.plist_path)       # sibling's job definition intact
    assert all("com.example.a" not in " ".join(call) for call in broken._run.calls)


def test_remove_package_leaves_the_sibling_agent_entirely_alone(tmp_path, mock_logger):
    """E7 closed the deferred hazard: remove_package() is now PER PACKAGE.

    It used to ``rmtree`` the shared ``node_modules`` wholesale while booting out only
    its own label — so the sibling kept a loaded job definition and a matching applied
    marker pointing at a package that no longer existed, and crash-looped on the next
    respawn with nothing in the plugin log saying why. That was pinned-not-endorsed
    while exactly one agent existed. This now pins the isolation instead: npm is asked
    to uninstall one package by name, and the fallback (below) removes one directory.
    """
    home = tmp_path / "home"
    controller = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")), mock_logger)
    sibling = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store")), mock_logger)
    assert controller.ensure_installed() is True
    assert sibling.ensure_installed() is True
    sibling_entry = os.path.join(sibling.project_dir, "node_modules", "pkg-b", "dist", "Main.js")
    sibling_digest = sibling._read_applied_digest()
    assert os.path.exists(sibling_entry)
    with open(os.path.join(controller.resolved_bin_dir, "npm"), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")

    controller._run = FakeRunner()
    controller.remove_package()

    assert os.path.exists(sibling_entry)            # ⊗ the whole point
    assert sibling.preflight() is None              # so the sibling still passes preflight
    assert os.path.exists(sibling.plist_path)
    assert sibling._read_applied_digest() == sibling_digest
    # And it is still a targeted npm call, naming only our package.
    npm_calls = [call for call in controller._run.calls if "uninstall" in call]
    assert npm_calls and "pkg-a" in npm_calls[0] and "pkg-b" not in " ".join(npm_calls[0])
    assert all("com.example.b" not in " ".join(call) for call in controller._run.calls)
    # Our own marker is dropped, so the next ensure_installed re-bootstraps.
    assert controller._read_applied_digest() is None


def test_remove_package_fallback_deletes_only_its_own_package_dir(tmp_path, mock_logger):
    """No npm available: the directory delete is scoped to our package too."""
    home = tmp_path / "home"
    controller = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")), mock_logger)
    sibling = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store")), mock_logger)
    assert controller.ensure_installed() is True
    assert sibling.ensure_installed() is True
    ours = os.path.join(controller.project_dir, "node_modules", "pkg-a")
    theirs = os.path.join(sibling.project_dir, "node_modules", "pkg-b")

    controller._run = FakeRunner()
    # `exists` is the injected seam preflight uses; make npm specifically absent.
    npm = os.path.join(controller.resolved_bin_dir, "npm")
    controller._exists = lambda path: path != npm and os.path.exists(path)
    controller.remove_package()

    assert not os.path.exists(ours)
    assert os.path.exists(theirs)
    assert not any("uninstall" in call for call in controller._run.calls)


# ---------------------------------------------------------------------------
# remove_package's outcome — it used to be the same sentence whatever happened
# ---------------------------------------------------------------------------

def test_remove_package_reports_TRUE_only_when_the_package_is_actually_gone(tmp_path,
                                                                            mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s")),
                   mock_logger)
    assert agent.ensure_installed() is True
    npm = os.path.join(agent.resolved_bin_dir, "npm")
    agent._exists = lambda path: path != npm and os.path.exists(path)
    assert agent.remove_package() is True
    assert "Removed the pkg-a package" in _infos(mock_logger)


def test_a_REFUSED_npm_uninstall_falls_back_and_the_package_still_goes(tmp_path,
                                                                       mock_logger):
    """⊗ Replacing `_npm_uninstall`'s non-zero-exit branch with `return True` left
    the suite green: nothing exercised a *present* npm that fails, so the
    fallback that actually removes the directory was never reached in a test.
    """
    home = tmp_path / "home"
    agent = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "s")), mock_logger,
                   runner=FakeRunner())
    assert agent.ensure_installed() is True
    with open(os.path.join(agent.resolved_bin_dir, "npm"), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
    ours = os.path.join(agent.project_dir, "node_modules", "pkg-a")
    assert os.path.isdir(ours)

    class _NpmRefuses(FakeRunner):
        def __call__(self, cmd, **kwargs):
            result = super().__call__(cmd, **kwargs)
            if "uninstall" in cmd:
                result.returncode = 1
                result.stderr = "npm ERR! code EBUSY"
            return result

    agent._run = _NpmRefuses()
    assert agent.remove_package() is True
    assert not os.path.exists(ours), \
        "npm refused, so the directory fallback had to run — and did not"


def test_remove_package_says_so_when_it_could_NOT_remove_it(tmp_path, mock_logger,
                                                            monkeypatch):
    """⊗ Replacing `_npm_uninstall`'s non-zero-exit branch with `return True` left
    the suite green, because every route out of here logged "Removed the …
    package" regardless: npm missing, npm refusing, an OSError starting it and an
    `rmtree` that raised were all reported as a completed removal — and the
    caller then reinstalled on top of the wedge, saying it had cleared it.
    """
    import shutil

    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s")),
                   mock_logger)
    assert agent.ensure_installed() is True
    npm = os.path.join(agent.resolved_bin_dir, "npm")
    agent._exists = lambda path: path != npm and os.path.exists(path)
    # The fallback's own failure mode: a directory rmtree cannot remove.
    monkeypatch.setattr(shutil, "rmtree",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("permission denied")))

    assert agent.remove_package() is False
    said = " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                    for c in mock_logger.error.call_args_list)
    assert "Could NOT remove" in said
    assert "Removed the pkg-a package" not in _infos(mock_logger)


def _infos(logger) -> str:
    return " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                    for c in logger.info.call_args_list)


# ---------------------------------------------------------------------------
# run_state — "loaded" is not "running", and the difference is #104's fault 2
# ---------------------------------------------------------------------------

def test_run_state_distinguishes_the_four_things_launchd_can_say(tmp_path, mock_logger):
    """⊗ `ensure_installed() is not None` was being printed as "the LaunchAgent
    is running", and `is_running()` (which means "launchd knows this label")
    passes for a job that is loaded and DEAD — the state this file already
    recovers from at `_apply_plist`, and the one launchd will not respawn."""
    home = tmp_path / "home"
    spec = _spec("com.example.a", "pkg-a", str(tmp_path / "s"))

    running = _agent(home, spec, mock_logger, runner=FakeRunner(pid=1234))
    assert running.run_state() == LaunchAgent.RUNNING
    assert running.is_alive() is True
    assert running.is_running() is True

    # Loaded, `launchctl print` succeeds, but there is no `pid =` line at all.
    dead = _agent(home, spec, mock_logger, runner=FakeRunner(pid=None))
    assert dead.run_state() == LaunchAgent.LOADED_NOT_RUNNING
    assert dead.is_alive() is False
    assert dead.is_running() is True, "still 'loaded' — that is what is_running means"

    absent = _agent(home, spec, mock_logger, runner=FakeRunner(returncode=1))
    assert absent.run_state() == LaunchAgent.NOT_LOADED
    assert absent.is_alive() is False


def test_an_unparseable_pid_line_is_UNKNOWN_and_counts_as_alive(tmp_path, mock_logger):
    """We cannot prove it either way; reporting failure would call a healthy
    server stopped, and killing it would be worse."""
    class _GarbledPid(FakeRunner):
        def __call__(self, cmd, **kwargs):
            result = super().__call__(cmd, **kwargs)
            if len(cmd) >= 2 and cmd[0] == "launchctl" and cmd[1] == "print":
                result.stdout = "\tstate = running\n\tpid = not-a-number\n"
            return result

    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s")),
                   mock_logger, runner=_GarbledPid())
    assert agent.run_state() == LaunchAgent.UNKNOWN
    assert agent.is_alive() is True


def test_start_over_a_loaded_but_dead_job_does_not_report_success(tmp_path, mock_logger):
    """`start()` is what fabric restore believes, and it used to answer
    `is_running()` — "launchd knows this label" — after installing."""
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s")),
                   mock_logger, runner=FakeRunner(pid=None))
    assert not os.path.exists(agent.plist_path)   # the ensure_installed branch
    assert agent.start() is False


# ---------------------------------------------------------------------------
# Reaping is per-agent: an agent must never signal the other agent's process
# ---------------------------------------------------------------------------

def _proc_line(agent, pid):
    pkg = os.path.join(agent.project_dir, "node_modules", agent.spec.package)
    return (f"{pid} node {pkg}/dist/Main.js --storage-path {agent.storage_path} "
            f"--port {agent.spec.port}")


def test_each_agent_reaps_only_its_own_package(tmp_path, mock_logger):
    home = tmp_path / "home"
    runner = ProcRunner()
    first = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store"), port=5580),
                   mock_logger, runner=runner)
    second = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store"), port=5541),
                    mock_logger, runner=ProcRunner())
    runner.ps_lines = [_proc_line(first, 111), _proc_line(second, 222)]

    assert first._running_server_pids() == [111]      # never the sibling agent's process
    assert first.reap_orphan_servers() == 1
    assert ("TERM", "111") in runner.signals
    assert not any(sig for sig in runner.signals if sig[1] == "222")


def test_each_agent_only_polices_its_own_port(tmp_path, mock_logger):
    # The sibling agent legitimately holds ITS port; ours is what we may complain about.
    home = tmp_path / "home"
    runner = ProcRunner(listen_pids=[222])
    first = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store"), port=5580),
                   mock_logger, runner=runner)
    second = _agent(home, _spec("com.example.b", "pkg-b", str(tmp_path / "b-store"), port=5541),
                    mock_logger, runner=ProcRunner())
    runner.ps_lines = [_proc_line(second, 222)]
    # lsof is asked about OUR port only; the fake answers unconditionally, so pid 222
    # looks like a holder — but it is not one of OUR package's processes, so it is
    # warned about and never signalled.
    assert first.reap_orphan_servers() == 0
    assert not runner.signals
    lsof = [c for c in runner.calls if c and c[0] == "lsof"]
    assert lsof and "-iTCP:5580" in lsof[0]           # our port, not the sibling's


def test_portless_agent_never_shells_out_to_lsof(tmp_path, mock_logger):
    # An agent that listens on nothing has no port contention to police.
    home = tmp_path / "home"
    runner = ProcRunner()
    agent = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")),
                   mock_logger, runner=runner)
    assert agent.reap_orphan_servers() == 0
    assert not [c for c in runner.calls if c and c[0] == "lsof"]


# ---------------------------------------------------------------------------
# Golden: the controller's plist must be byte-identical to the pre-extraction one
# ---------------------------------------------------------------------------

GOLDEN_PREFS = {
    "serverLocation": "remote",
    "matterServerPort": "5590",
    "primaryInterface": "en5",
    "matterServerListenAddress": "192.168.1.50",
    "enableTestNetDcl": True,
    "storagePath": "~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server",
}


def test_controller_plist_matches_the_pre_extraction_golden(tmp_path, mock_logger):
    """Captured from ServerProcess.build_plist() on main (pre-AgentSpec).

    The extraction is behaviour-preserving or it is nothing: this is the exact
    structure launchd is handed, including the ``--enable-test-net-dcl`` must-be-last
    ordering that a generic flag builder would have lost.
    """
    home = tmp_path / "home"
    home.mkdir()
    sp = ServerProcess(GOLDEN_PREFS, mock_logger, home=str(home),
                       npx_path="/opt/homebrew/bin/npx", runner=FakeRunner())
    entry = os.path.join(str(home), "indigo-matter", "node_modules", "matter-server",
                         "dist", "esm", "MatterServer.js")
    storage = os.path.join(str(home), "Library", "Application Support",
                           "com.simons-plugins.indigo-matter", "matter-server")
    logs = os.path.join(str(home), "Library", "Logs", "indigo-matter")
    assert plistlib.loads(sp.build_plist()) == {
        "Label": "com.simons-plugins.indigo-matter",
        "ProgramArguments": [
            "/opt/homebrew/bin/node", entry,
            "--port", "5590",
            "--listen-address", "192.168.1.50",
            "--storage-path", storage,
            "--primary-interface", "en5",
            "--enable-test-net-dcl",
        ],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False, "Crashed": True},
        "StandardOutPath": os.path.join(logs, "matter-server.log"),
        "StandardErrorPath": os.path.join(logs, "matter-server.err.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"},
    }


def test_controller_plist_bytes_are_stable(mock_logger):
    """Pin the RAW serialized bytes, not just the parsed dict.

    The applied-plist marker is sha256 over these bytes: a serialization change
    (format, key order, XML header) that leaves the parsed dict equal would pass the
    golden test above yet invalidate every existing install's marker — forcing a
    bootout+bootstrap of a healthy server and dropping every device's CASE session,
    exactly what the legacy-marker-filename decision exists to avoid. A deliberate
    plist content change updates this hash; a serialization drift must never.
    Inputs are fully literal (fixed home, no tmp_path) so the bytes are reproducible.
    """
    sp = ServerProcess(GOLDEN_PREFS, mock_logger, home="/Users/example",
                       npx_path="/opt/homebrew/bin/npx", runner=FakeRunner())
    digest = hashlib.sha256(sp.build_plist()).hexdigest()
    assert digest == "6096e2609fc24ceda25eb2d1d617b35985101cf14922b8f7e240568e7a00c37e"


def test_controller_spec_pins_matter_server(tmp_path, mock_logger):
    home = tmp_path / "home"
    home.mkdir()
    sp = ServerProcess({"serverLocation": "local"}, mock_logger, home=str(home),
                       npx_path="/opt/homebrew/bin/npx", runner=FakeRunner())
    assert sp.spec.package == "matter-server"
    assert sp.spec.install_spec == "matter-server@1.2.2"
    assert sp.spec.default_entry == "dist/esm/MatterServer.js"
    assert sp.spec.port == 5580                 # int for lsof; sp.port stays the CLI string
    assert sp.port == "5580"


def test_non_numeric_port_pref_disables_the_port_signal(tmp_path, mock_logger):
    # lsof would simply have failed and reported nothing; skip the shell-out instead.
    home = tmp_path / "home"
    home.mkdir()
    sp = ServerProcess({"serverLocation": "remote", "matterServerPort": "not-a-port"},
                       mock_logger, home=str(home), npx_path="/opt/homebrew/bin/npx",
                       runner=FakeRunner())
    assert sp.port == "not-a-port"              # unchanged: still what the CLI is handed
    assert sp.spec.port is None
    assert sp._port_listener_pids() == []
