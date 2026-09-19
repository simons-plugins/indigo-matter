"""AgentSpec / LaunchAgent: the parts that only matter once there are TWO agents.

``tests/test_server_process.py`` covers the machinery itself (it exercises the same
code through the controller specialisation, unchanged by the extraction). This file
pins what the extraction ADDS: that an agent's identity is carried by its spec, that
two agents sharing one ``project_dir`` cannot clobber each other's applied-plist
digest (which would bootout a healthy job on every reload), and that each reaps only
its own orphans.

Every agent here is built against a ``tmp_path`` home. Never construct one against the
real ``$HOME``: ``ensure_installed()``'s preflight-failure path calls ``uninstall()``,
which would delete the developer's live LaunchAgent.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import plistlib
import subprocess
import time

import pytest

from launch_agent import (
    INSTALL_NODE_STAMP, VERDICT_CONFLICT_FREE, VERDICT_DEAD, VERDICT_PENDING, AgentSpec, LaunchAgent,
)
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


def test_agent_spec_env_passes_through_but_can_never_override_path(tmp_path, mock_logger):
    """`env` is merged FIRST and PATH written LAST (2026.32.1).

    A flipped merge order would let a spec's PATH entry strip the resolved node
    bin dir, and that agent would crash-loop on its next fresh bootstrap with
    node unable to find its co-located helpers — surfacing only as "connection
    refused". Extra variables must still pass through untouched.
    """
    import dataclasses
    spec = dataclasses.replace(_spec("com.example.thing", "thing", "/tmp/thing-store"),
                               env=(("PATH", "/evil"), ("FOO", "bar")))
    agent = _agent(tmp_path / "home", spec, mock_logger)
    env = plistlib.loads(agent.build_plist())["EnvironmentVariables"]
    assert env["PATH"] == f"{os.path.dirname(agent.npx_path)}:/usr/bin:/bin"
    assert env["FOO"] == "bar"


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


def _warnings(logger) -> str:
    return " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                    for c in logger.warning.call_args_list)


def _errors(logger) -> str:
    return " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                    for c in logger.error.call_args_list)


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
    lsof = [c for c in runner.calls if c and os.path.basename(c[0]) == "lsof"]
    assert lsof and "-iTCP:5580" in lsof[0]           # our port, not the sibling's


def test_portless_agent_never_shells_out_to_lsof(tmp_path, mock_logger):
    # An agent that listens on nothing has no port contention to police.
    home = tmp_path / "home"
    runner = ProcRunner()
    agent = _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")),
                   mock_logger, runner=runner)
    assert agent.reap_orphan_servers() == 0
    assert not [c for c in runner.calls if c and os.path.basename(c[0]) == "lsof"]


# ---------------------------------------------------------------------------
# Issue #182 — the port probe must not fail silently, and a pid is not health
# ---------------------------------------------------------------------------

class NoLsofRunner(ProcRunner):
    """``lsof`` cannot be executed at all — the #182 PATH failure.

    Python raises OSError when ``subprocess`` cannot find the binary, which the old
    probe swallowed into an empty list indistinguishable from "nothing is listening".
    """

    def __call__(self, cmd, **kwargs):
        if cmd and os.path.basename(cmd[0]) == "lsof":
            self.calls.append(cmd)
            raise OSError(2, "No such file or directory: 'lsof'")
        return super().__call__(cmd, **kwargs)


def _portful(tmp_path, mock_logger, runner, port=5580):
    home = tmp_path / "home"
    return _agent(home, _spec("com.example.a", "pkg-a", str(tmp_path / "a-store"),
                              port=port), mock_logger, runner=runner)


def test_port_probe_prefers_an_absolute_lsof_over_the_bare_name(tmp_path, mock_logger):
    # The bare name depends on the plugin host's inherited PATH, and lsof lives only in
    # /usr/sbin on macOS. Resolving it absolutely is the actual #182 root-cause fix.
    runner = ProcRunner(listen_pids=[321])
    agent = _portful(tmp_path, mock_logger, runner)
    agent._exists = lambda path: path == "/usr/sbin/lsof"
    assert agent._port_listener_pids() == [321]
    probes = [c[0] for c in runner.calls if c and os.path.basename(c[0]) == "lsof"]
    assert probes == ["/usr/sbin/lsof"]


def test_port_probe_falls_back_to_bare_name_when_no_absolute_lsof_exists(tmp_path, mock_logger):
    runner = ProcRunner(listen_pids=[321])
    agent = _portful(tmp_path, mock_logger, runner)
    agent._exists = lambda _path: False           # neither absolute candidate present
    assert agent._port_listener_pids() == [321]
    probes = [c[0] for c in runner.calls if c and os.path.basename(c[0]) == "lsof"]
    assert probes == ["lsof"]


def test_nothing_listening_is_an_empty_list_not_an_unknown(tmp_path, mock_logger):
    # lsof exits 1 when nothing matches. That is a SUCCESSFUL probe with a real answer
    # and must stay distinguishable from a broken probe, or the caller cannot diagnose.
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[]))
    assert agent._port_listener_pids() == []


def test_unusable_port_probe_returns_none_and_warns_once(tmp_path, mock_logger):
    runner = NoLsofRunner()
    agent = _portful(tmp_path, mock_logger, runner)
    assert agent._port_listener_pids() is None        # "could not tell", not "nobody"
    assert agent._port_listener_pids() is None
    warnings = [str(c) for c in mock_logger.warning.call_args_list if "lsof" in str(c)]
    assert len(warnings) == 1                         # once per agent, never per call
    assert "5580" in warnings[0]                      # names the port and the hand recipe
    # Every candidate was genuinely attempted before giving up.
    assert len({c[0] for c in runner.calls
                if c and os.path.basename(c[0]) == "lsof"}) >= 1


def test_reap_still_works_from_storage_path_alone_when_the_probe_is_unusable(tmp_path,
                                                                            mock_logger):
    # Degrading to the pre-#104 signal is correct; crashing or reaping nothing is not.
    runner = NoLsofRunner()
    agent = _portful(tmp_path, mock_logger, runner)
    runner.ps_lines = [_proc_line(agent, 111)]
    assert agent._running_server_pids() == [111]
    assert agent.reap_orphan_servers() == 1
    assert ("TERM", "111") in runner.signals


def test_no_conflict_reported_when_our_own_pid_holds_the_port(tmp_path, mock_logger):
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[5423]))
    assert agent.port_conflict_report(managed_pid=5423) is None


def test_foreign_port_holder_is_reported_authoritatively(tmp_path, mock_logger):
    """The exact jarvis incident: our job is alive, someone else owns 5580."""
    runner = ProcRunner(listen_pids=[659])
    agent = _portful(tmp_path, mock_logger, runner)
    runner.ps_lines = ["659 node /elsewhere/matter-server/dist/esm/MatterServer.js"]
    report = agent.port_conflict_report(managed_pid=43466)
    assert report is not None
    assert "659" in report and "43466" in report          # both sides named
    assert "5580" in report
    # No err-log corroboration needed: the port is demonstrably held by someone else.
    assert not agent._err_log_mentions_port_conflict()


def _write_stale_eaddrinuse(agent):
    """An EADDRINUSE line in the append-only err log, e.g. from weeks ago."""
    os.makedirs(agent.log_dir, exist_ok=True)
    with open(os.path.join(agent.log_dir, agent.spec.err_log), "w",
              encoding="utf-8") as handle:
        handle.write("FATAL WebServer Webserver error on 127.0.0.1:5580 "
                     "listen EADDRINUSE: address already in use\n")


def test_headless_job_is_reported_once_past_the_startup_grace(tmp_path, mock_logger):
    # Settled process (10:00 by default) + nothing listening = genuinely headless.
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[]))
    report = agent.port_conflict_report(managed_pid=5423)
    assert report is not None and "NOTHING is listening" in report


def test_young_process_with_no_listener_is_not_accused(tmp_path, mock_logger):
    # A server that started 5s ago simply has not bound yet (~9s on jarvis).
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[], proc_etime="00:05"))
    assert agent.port_conflict_report(managed_pid=5423) is None


def test_a_stale_eaddrinuse_never_accuses_a_freshly_started_server(tmp_path, mock_logger):
    """Review of #183: the err log is append-only, so one old EADDRINUSE would
    corroborate 'nothing is listening' on every future startup for ever. Age, not the
    log, is what gates that branch — so an ancient marker plus a young process is quiet.
    """
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[], proc_etime="00:03"))
    _write_stale_eaddrinuse(agent)
    assert agent._err_log_mentions_port_conflict()          # the stale marker IS there…
    assert agent.port_conflict_report(managed_pid=5423) is None   # …and is ignored here


def test_unknown_process_age_is_never_accused(tmp_path, mock_logger):
    # ps could not tell us. Fail closed: "cannot determine" must not become "guilty".
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[], proc_etime=None))
    _write_stale_eaddrinuse(agent)
    assert agent.port_conflict_report(managed_pid=5423) is None


@pytest.mark.parametrize("etime,expected", [
    ("00:05", 5), ("10:00", 600), ("01:02:03", 3723), ("01-06:30:50", 109850),
])
def test_process_age_parses_every_ps_etime_format(tmp_path, mock_logger, etime, expected):
    agent = _portful(tmp_path, mock_logger, ProcRunner(proc_etime=etime))
    assert agent._process_age_seconds(5423) == expected


@pytest.mark.parametrize("etime", ["", "not-a-time", "1:2:3:4", "xx-01:02:03"])
def test_unparseable_process_age_is_none_not_zero(tmp_path, mock_logger, etime):
    # Zero would read as "just started" and silence a real fault; None is "don't know".
    agent = _portful(tmp_path, mock_logger, ProcRunner(proc_etime=etime))
    assert agent._process_age_seconds(5423) is None


def test_unusable_probe_reports_advisory_only_with_err_log_corroboration(tmp_path,
                                                                        mock_logger):
    agent = _portful(tmp_path, mock_logger, NoLsofRunner())
    assert agent.port_conflict_report(managed_pid=5423) is None      # clean log → quiet

    os.makedirs(agent.log_dir, exist_ok=True)
    with open(os.path.join(agent.log_dir, agent.spec.err_log), "w",
              encoding="utf-8") as handle:
        handle.write("FATAL MatterServer Server failed to start listen EADDRINUSE\n")
    report = agent.port_conflict_report(managed_pid=5423)
    assert report is not None and "advisory" in report


def test_no_conflict_reported_when_there_is_no_running_managed_job(tmp_path, mock_logger):
    # A dead/absent job is a different fault with its own handling; calling it a port
    # conflict would send the user hunting for a process that isn't there.
    agent = _portful(tmp_path, mock_logger, ProcRunner(print_pid=None, listen_pids=[659]))
    assert agent.port_conflict_report() is None


def test_portless_agent_never_reports_a_port_conflict(tmp_path, mock_logger):
    agent = _agent(tmp_path / "home",
                   _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")),
                   mock_logger, runner=ProcRunner(listen_pids=[659]))
    assert agent.port_conflict_report(managed_pid=111) is None


# ---------------------------------------------------------------------------
# #187 — a fresh bootstrap arms a deferred post-bind-window port verification,
# consumed by the caller (the plugin's periodic tick) once STARTUP_GRACE_SECONDS
# has passed. Neither _apply_plist's fresh-bootstrap path nor start()/restart()
# used to check this themselves, leaving a rival free to win the bind race
# unnoticed until whatever tick happened to run next.
# ---------------------------------------------------------------------------

def test_nothing_pending_before_any_bootstrap(tmp_path, mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    assert agent.due_for_bootstrap_verification() is None


def test_a_fresh_bootstrap_arms_verification_but_not_before_the_grace_window(tmp_path,
                                                                             mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    assert agent.ensure_installed() is True          # fresh bootstrap (no plist existed)
    assert agent.due_for_bootstrap_verification() is None   # armed, but grace not elapsed
    agent._bootstrap_verify_after = 0.0              # simulate STARTUP_GRACE_SECONDS elapsed
    assert agent.due_for_bootstrap_verification() == 0.0


def test_clear_bootstrap_verification_disarms_it(tmp_path, mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    agent.ensure_installed()
    agent._bootstrap_verify_after = 0.0
    token = agent.due_for_bootstrap_verification()
    assert token == 0.0
    agent.clear_bootstrap_verification(token)
    assert agent.due_for_bootstrap_verification() is None


def test_clear_bootstrap_verification_ignores_a_stale_token(tmp_path, mock_logger):
    """Compare-and-clear (#187 review): a clear against a deadline that is no
    longer the armed one — because something re-armed in between the caller's
    due-observation and its clear call — must be a no-op, never wipe the NEW
    arming. This is what makes a concurrent re-arm (a menu restart landing on
    another thread mid-tick) safe."""
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    agent._bootstrap_verify_after = 100.0
    stale_token = agent._bootstrap_verify_after
    agent._bootstrap_verify_after = 200.0        # a concurrent re-arm
    agent.clear_bootstrap_verification(stale_token)
    assert agent._bootstrap_verify_after == 200.0   # untouched by the stale clear
    agent.clear_bootstrap_verification(200.0)
    assert agent._bootstrap_verify_after is None


def test_restart_also_arms_verification(tmp_path, mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    agent.ensure_installed()
    agent.clear_bootstrap_verification(agent._bootstrap_verify_after)
    assert agent.restart() is True
    assert agent.due_for_bootstrap_verification() is None   # armed, not yet due
    agent._bootstrap_verify_after = 0.0
    assert agent.due_for_bootstrap_verification() == 0.0


def test_start_also_arms_verification(tmp_path, mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    agent.ensure_installed()                          # writes the plist start() reloads
    agent.clear_bootstrap_verification(agent._bootstrap_verify_after)
    assert agent.start() is True
    agent._bootstrap_verify_after = 0.0
    assert agent.due_for_bootstrap_verification() == 0.0


def test_leaving_a_healthy_job_untouched_does_not_arm_verification(tmp_path, mock_logger):
    """The digest-matches 'leave alone' branch already checks the port itself
    (#182, unchanged) — it must not ALSO arm a #187 verification, since no
    bootstrap happened on that pass."""
    runner = ProcRunner(print_pid=4242, listen_pids=[4242])
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=runner)
    assert agent.ensure_installed() is True            # first pass: marker mismatch → bootstraps
    agent.clear_bootstrap_verification(agent._bootstrap_verify_after)
    assert agent.ensure_installed() is False            # second pass: digest matches → leave alone
    assert agent.due_for_bootstrap_verification() is None


# ---------------------------------------------------------------------------
# post_bootstrap_verdict() / managed_job_has_pid() — the tri-state #187 review
# adds so a caller can tell "healthy", the #187 fault itself (no pid at all),
# and "too soon to tell" apart once port_conflict_report() has answered None.
# ---------------------------------------------------------------------------

def test_verdict_is_dead_when_the_managed_job_has_no_pid(tmp_path, mock_logger):
    agent = _portful(tmp_path, mock_logger, ProcRunner(print_pid=None))
    assert agent.post_bootstrap_verdict() == VERDICT_DEAD
    assert agent.managed_job_has_pid() is False


def test_verdict_is_conflict_free_once_the_pid_is_past_the_grace_window(tmp_path, mock_logger):
    agent = _portful(tmp_path, mock_logger, ProcRunner(print_pid=5423, proc_etime="10:00"))
    assert agent.post_bootstrap_verdict() == VERDICT_CONFLICT_FREE
    assert agent.managed_job_has_pid() is True


def test_verdict_is_pending_while_the_pid_is_still_young(tmp_path, mock_logger):
    agent = _portful(tmp_path, mock_logger, ProcRunner(print_pid=5423, proc_etime="00:05"))
    assert agent.post_bootstrap_verdict() == VERDICT_PENDING


def test_verdict_is_pending_when_the_pids_age_is_unknowable(tmp_path, mock_logger):
    # Fail closed, same discipline as port_conflict_report's own age gate: "cannot
    # tell" must never read as "old enough" and render a verdict it hasn't earned.
    agent = _portful(tmp_path, mock_logger, ProcRunner(print_pid=5423, proc_etime=None))
    assert agent.post_bootstrap_verdict() == VERDICT_PENDING


# ---------------------------------------------------------------------------
# adopt_pending_bootstrap_verification() — a rebuild whose own ensure_installed()
# left a healthy job alone must not silently drop the replaced instance's
# still-pending #187 verification (bridge_agent_menu_mixin.py's _start_bridge_agent).
# ---------------------------------------------------------------------------

def test_adopt_carries_over_a_pending_verification(tmp_path, mock_logger):
    spec = _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580)
    old = _agent(tmp_path / "home", spec, mock_logger, runner=FakeRunner())
    old._bootstrap_verify_after = 123.0
    new = _agent(tmp_path / "home", spec, mock_logger, runner=FakeRunner())
    assert new._bootstrap_verify_after is None
    new.adopt_pending_bootstrap_verification(old)
    assert new._bootstrap_verify_after == 123.0


def test_adopt_never_overwrites_a_fresh_arming(tmp_path, mock_logger):
    """A bootstrap THIS instance just did must win — never be clobbered by an
    older instance's stale deadline."""
    spec = _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580)
    old = _agent(tmp_path / "home", spec, mock_logger, runner=FakeRunner())
    old._bootstrap_verify_after = 1.0
    new = _agent(tmp_path / "home", spec, mock_logger, runner=FakeRunner())
    new._bootstrap_verify_after = 999.0          # this instance's OWN fresh bootstrap
    new.adopt_pending_bootstrap_verification(old)
    assert new._bootstrap_verify_after == 999.0


def test_adopt_with_no_previous_instance_is_a_noop(tmp_path, mock_logger):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "s"), port=5580),
                   mock_logger, runner=FakeRunner())
    agent.adopt_pending_bootstrap_verification(None)   # first-ever call: nothing to adopt
    assert agent._bootstrap_verify_after is None


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


class FlakyLsofRunner(ProcRunner):
    """lsof fails only while ``broken`` is True — models a transient failure."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.broken = True

    def __call__(self, cmd, **kwargs):
        if cmd and os.path.basename(cmd[0]) == "lsof" and self.broken:
            self.calls.append(cmd)
            raise OSError(2, "No such file or directory: 'lsof'")
        return super().__call__(cmd, **kwargs)


def test_a_successful_probe_rearms_the_unusable_warning(tmp_path, mock_logger):
    """Review of #183: the latch was write-once per agent, which lives for the whole
    plugin session (weeks). One transient lsof failure would then permanently silence
    the warning — so the REAL blindness, months later, would be reported nowhere.
    """
    runner = FlakyLsofRunner(listen_pids=[321])
    agent = _portful(tmp_path, mock_logger, runner)

    assert agent._port_listener_pids() is None
    assert len([c for c in mock_logger.warning.call_args_list if "lsof" in str(c)]) == 1

    runner.broken = False                       # lsof works again
    assert agent._port_listener_pids() == [321]

    runner.broken = True                        # …and breaks again later
    assert agent._port_listener_pids() is None
    warnings = [c for c in mock_logger.warning.call_args_list if "lsof" in str(c)]
    assert len(warnings) == 2                   # the second episode is reported too


def test_unusable_ps_is_warned_about_not_silently_disabling_the_age_gate(tmp_path,
                                                                        mock_logger):
    # The "nothing is listening" diagnosis depends ENTIRELY on process age, so a
    # broken ps would disable it with no operator-visible symptom — the shape of bug
    # this whole change set exists to remove.
    agent = _portful(tmp_path, mock_logger, ProcRunner(listen_pids=[], proc_etime=None))
    assert agent._process_age_seconds(5423) is None
    warnings = [c for c in mock_logger.warning.call_args_list if "how long" in str(c)]
    assert len(warnings) == 1
    assert "5580" in str(warnings[0])           # names the check it disables


def test_our_pid_among_several_holders_is_treated_as_healthy(tmp_path, mock_logger):
    """Pins a deliberate decision rather than leaving it accidental.

    Two independent LISTENers on one port needs SO_REUSEPORT, so this is a corner —
    but if OUR pid is bound, the plugin's actual question ("am I reachable on my own
    port?") is answered yes, and we do not accuse anyone. Change this only on evidence
    that a co-holder can steal our connections.
    """
    runner = ProcRunner(listen_pids=[5423, 659])
    agent = _portful(tmp_path, mock_logger, runner)
    assert agent.port_conflict_report(managed_pid=5423) is None


def test_port_probe_tries_the_second_absolute_candidate(tmp_path, mock_logger):
    runner = ProcRunner(listen_pids=[321])
    agent = _portful(tmp_path, mock_logger, runner)
    agent._exists = lambda path: path == "/usr/bin/lsof"      # only the second exists
    assert agent._port_listener_pids() == [321]
    probes = [c[0] for c in runner.calls if c and os.path.basename(c[0]) == "lsof"]
    assert probes == ["/usr/bin/lsof"]


# ---------------------------------------------------------------------------
# clear_stale_storage_locks — the 2026-09-15 jarvis reboot pid-reuse bug.
#
# A stale matter.lock stops a fresh matter-server from EVER starting ("Storage is
# locked by another process"), and matter.js's own check is defeated by a rebooted
# Mac recycling the recorded pid onto an unrelated, still-live process. These tests
# are adversarial on purpose: each one asks "could this clear a lock it must not?"
# or "could this leave a lock it must clear?" rather than just the happy path.
# ---------------------------------------------------------------------------

def _write_lock(directory, pid, token="deadbeef", mtime=None):
    """Write a matter.lock + matter.pid pair as matter.js itself would.

    Returns (lock_path, pid_path). ``mtime``, if given, backdates matter.pid's
    mtime (epoch seconds) so a test can control the start-time signal precisely.
    """
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, "matter.lock")
    pid_path = os.path.join(directory, "matter.pid")
    with open(lock_path, "w", encoding="utf-8") as handle:
        handle.write("")
    with open(pid_path, "w", encoding="utf-8") as handle:
        handle.write(f"{pid} {token}\n")
    if mtime is not None:
        os.utime(pid_path, (mtime, mtime))
    return lock_path, pid_path


class NoPsRunner(ProcRunner):
    """``ps`` cannot be executed at all — models a totally unusable probe.

    Mirrors ``NoLsofRunner`` above: Python raises OSError when subprocess can't
    find the binary, which ``_ps_map`` must fold into "{}" (could not tell),
    never into "nothing is running".
    """

    def __call__(self, cmd, **kwargs):
        if cmd and cmd[0] == "ps":
            self.calls.append(cmd)
            raise OSError(2, "No such file or directory: 'ps'")
        return super().__call__(cmd, **kwargs)


def test_clear_stale_locks_clears_a_reboot_reused_pid(tmp_path, mock_logger):
    """The real bug: matter.pid names a LIVE pid that is someone else entirely.

    This is pid 1621 on jarvis, 2026-09-15: the pre-reboot matter-server's pid,
    reassigned by macOS to IndigoPluginHost3 running Home Intelligence.
    """
    storage = str(tmp_path / "a-store")
    runner = ProcRunner(ps_lines=[
        "1621 /usr/bin/python3 IndigoPluginHost3 -x indigo-home-intelligence",
    ])
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    lock, pidf = _write_lock(storage, 1621)
    assert agent.clear_stale_storage_locks() == 1
    assert not os.path.exists(lock)
    assert not os.path.exists(pidf)


def test_clear_stale_locks_clears_on_the_reboot_proof_start_time_signal(tmp_path, mock_logger):
    """Command line is inconclusive (no args at all); start time settles it.

    The live pid started only 5s ago, but matter.pid was last written an hour ago
    — the pid cannot be the process that wrote that file.
    """
    storage = str(tmp_path / "a-store")
    runner = ProcRunner(ps_lines=["570"], proc_etime="00:05")  # bare pid: unreadable command
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    lock, pidf = _write_lock(storage, 570, mtime=time.time() - 3600)
    assert agent.clear_stale_storage_locks() == 1
    assert not os.path.exists(lock)


def test_clear_stale_locks_leaves_a_genuinely_live_server_alone(tmp_path, mock_logger):
    """MUST NOT FIRE: both signals say this is our own, still-running server."""
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage, port=5580),
                   mock_logger, runner=ProcRunner())
    pkg = os.path.join(agent.project_dir, "node_modules", agent.spec.package)
    cmdline = f"4242 node {pkg}/dist/Main.js --storage-path {agent.storage_path} --port 5580"
    agent._run = ProcRunner(ps_lines=[cmdline], proc_etime="10:00")  # alive 10 minutes
    # matter.pid's mtime defaults to "now" — well AFTER the process's start time.
    lock, pidf = _write_lock(storage, 4242)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    assert os.path.exists(pidf)


def test_clear_stale_locks_does_not_accuse_an_owner_the_age_probe_merely_rounded(
        tmp_path, mock_logger):
    """MUST NOT FIRE: a live owner that wrote matter.pid within the probe's own error.

    ``ps -o etime=`` prints whole seconds truncated DOWN, and the age is subtracted
    from a ``time.time()`` read taken after that subprocess returns — both push the
    computed start time later. So a healthy server that acquired its lock a fraction
    of a second after exec can compute as "started after matter.pid was written". Here
    the process reports 1s of age against a pid file written 2s ago: a bare
    ``started_at > mtime`` test calls that stale and deletes a RUNNING server's lock,
    corrupting a live fabric. Only a gap no probe error explains may fire.
    """
    storage = str(tmp_path / "a-store")
    runner = ProcRunner(ps_lines=["4242"], proc_etime="00:01")  # unreadable command line
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    lock, pidf = _write_lock(storage, 4242, mtime=time.time() - 2)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    assert os.path.exists(pidf)


def test_clear_stale_locks_leaves_the_lock_when_the_probe_cannot_tell(tmp_path, mock_logger):
    """MUST NOT FIRE: ps itself is unusable, so nothing here is evidence of staleness.

    Deleting a live server's lock corrupts a running fabric — worse than the
    crash-loop this method exists to fix — so "could not tell" must never read
    as "safe to clear". The method must also say so, not silently report zero.
    """
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=NoPsRunner())
    lock, _pidf = _write_lock(storage, 4242)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    warnings = [str(c) for c in mock_logger.warning.call_args_list if "cannot tell" in str(c)]
    assert warnings


def test_clear_stale_locks_clears_a_dead_pid(tmp_path, mock_logger):
    storage = str(tmp_path / "a-store")
    # A non-empty, genuinely successful ps snapshot that simply does not list our pid —
    # not to be confused with an empty/unusable probe (that case has its own test).
    runner = ProcRunner(ps_lines=["1 /sbin/launchd", "50 /usr/libexec/some-service"])
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    lock, pidf = _write_lock(storage, 9999)
    assert agent.clear_stale_storage_locks() == 1
    assert not os.path.exists(lock)


def test_clear_stale_locks_clears_a_garbage_pid_file(tmp_path, mock_logger):
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=ProcRunner())
    os.makedirs(storage, exist_ok=True)
    lock = os.path.join(storage, "matter.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("")
    with open(os.path.join(storage, "matter.pid"), "w", encoding="utf-8") as handle:
        handle.write("not-a-pid\n")
    assert agent.clear_stale_storage_locks() == 1
    assert not os.path.exists(lock)


def test_clear_stale_locks_clears_a_missing_pid_file(tmp_path, mock_logger):
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=ProcRunner())
    os.makedirs(storage, exist_ok=True)
    lock = os.path.join(storage, "matter.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("")
    # No matter.pid at all — matter.js itself treats this as stale.
    assert agent.clear_stale_storage_locks() == 1
    assert not os.path.exists(lock)


def test_clear_stale_locks_walks_immediate_subdirectories_too(tmp_path, mock_logger):
    """The bridge keeps a lock per subdir (config/, certificates/, …), not at the root."""
    storage = str(tmp_path / "a-store")
    runner = ProcRunner(ps_lines=["1 /sbin/launchd"])
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    lock, _pidf = _write_lock(os.path.join(storage, "certificates"), 9999)
    assert agent.clear_stale_storage_locks() == 1
    assert not os.path.exists(lock)


def test_clear_stale_locks_no_op_when_nothing_is_locked(tmp_path, mock_logger):
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=ProcRunner())
    os.makedirs(storage, exist_ok=True)   # storage exists, but no matter.lock anywhere
    assert agent.clear_stale_storage_locks() == 0


def test_clear_stale_locks_leaves_a_positively_identified_owner_even_if_it_looks_young(
        tmp_path, mock_logger):
    """Pins the dangerous fall-through: a matching command line must END the
    decision. Before the fix, a matching command line fell through to the
    start-time check anyway, so a rounding-inflated age could still accuse a
    positively-identified, RUNNING server and delete its lock — corrupting a
    live fabric, the worst outcome this whole method exists to avoid.

    matter.pid's mtime here predates the process's computed start time by far
    more than the slack margin — exactly the shape that used to fire.
    """
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage, port=5580),
                   mock_logger, runner=ProcRunner())
    pkg = os.path.join(agent.project_dir, "node_modules", agent.spec.package)
    cmdline = f"4242 node {pkg}/dist/Main.js --storage-path {agent.storage_path} --port 5580"
    # etime "00:01" => age 1s => started_at ~= now. matter.pid's mtime is set far in
    # the past, so started_at is WELL past mtime + START_AFTER_LOCK_SLACK_SECONDS —
    # the exact condition the old code would have called stale.
    agent._run = ProcRunner(ps_lines=[cmdline], proc_etime="00:01")
    lock, pidf = _write_lock(storage, 4242, mtime=time.time() - 3600)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    assert os.path.exists(pidf)


def test_clear_stale_locks_treats_an_undecodable_pid_file_as_no_owner_without_raising(
        tmp_path, mock_logger):
    """A power cut mid-write leaves a binary/truncated matter.pid.

    `open(..., encoding="utf-8").read()` raises UnicodeDecodeError, which is a
    ValueError — NOT an OSError — so the old bare `except OSError` let it escape
    clear_stale_storage_locks (and from there start()/restart()/_apply_plist()).
    The deliberate, chosen behaviour: unreadable CONTENT is folded into "no
    owner recorded", same as garbage text — nothing more to learn from it.
    """
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=ProcRunner())
    os.makedirs(storage, exist_ok=True)
    lock = os.path.join(storage, "matter.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("")
    with open(os.path.join(storage, "matter.pid"), "wb") as handle:
        handle.write(b"\xff\xfe\x00binary-garbage-from-a-power-cut")
    assert agent.clear_stale_storage_locks() == 1   # did not raise, and cleared it
    assert not os.path.exists(lock)


def test_clear_stale_locks_leaves_an_unreadable_pid_file_and_warns(
        tmp_path, mock_logger, monkeypatch):
    """A PROBE FAILURE (e.g. EACCES) reading matter.pid must NOT read as "no
    owner": the file may still name a live server. Distinct from genuinely
    missing, which DOES clear (see the "missing_pid_file" test above).
    """
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=ProcRunner())
    lock, pidf = _write_lock(storage, 4242)
    real_open = open

    def _fail_on_pid_file(path, *a, **k):
        if path == pidf:
            raise PermissionError(13, "Permission denied")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _fail_on_pid_file)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    assert "could not be read" in _warnings(mock_logger)


def test_clear_lock_does_not_report_success_when_os_remove_fails(
        tmp_path, mock_logger, monkeypatch):
    """Reviewers reproduced this against a read-only directory: os.remove fails,
    but the old code logged "clearing stale ..." BEFORE attempting removal and
    swallowed the failure at DEBUG, so cleared += 1 ran regardless — the caller
    was told the lock was gone while it was still on disk, and the server kept
    crash-looping on it. The fix: count only a removal that actually succeeded,
    and log the failure at ERROR.
    """
    storage = str(tmp_path / "a-store")
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=ProcRunner())
    os.makedirs(storage, exist_ok=True)
    lock = os.path.join(storage, "matter.lock")
    with open(lock, "w", encoding="utf-8") as handle:
        handle.write("")
    # No matter.pid -> the "missing or unparseable" path, which calls _clear_lock.
    real_remove = os.remove

    def _fail_on_lock(path, *a, **k):
        if path == lock:
            raise PermissionError(13, "Permission denied")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(os, "remove", _fail_on_lock)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    errors = _errors(mock_logger)
    assert "matter.lock" in errors and f"rm {lock}" in errors
    # And the caller must never have claimed success for a removal that failed.
    assert "cleared stale" not in _warnings(mock_logger)


def test_clear_stale_locks_does_not_follow_a_symlinked_subdirectory(tmp_path, mock_logger):
    """A symlink inside the storage root must never be swept: os.path.isdir
    follows symlinks, so without an explicit islink() check a symlink pointing
    outside the storage tree would have its target's matter.lock DELETED
    outside the storage root entirely.
    """
    storage = str(tmp_path / "a-store")
    outside = tmp_path / "outside-the-storage-tree"
    os.makedirs(storage, exist_ok=True)
    # A dead pid: if this directory were swept, it would be judged stale and cleared.
    lock, pidf = _write_lock(str(outside), 99999)
    os.symlink(str(outside), os.path.join(storage, "linked"))
    runner = ProcRunner(ps_lines=["1 /sbin/launchd"])
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    assert os.path.exists(pidf)


def test_clear_stale_locks_warns_instead_of_a_silent_zero_when_listdir_fails(
        tmp_path, mock_logger, monkeypatch):
    """os.listdir raising must not silently narrow the sweep to the root: the
    bridge keeps its locks ONE level down, which is exactly what a failed
    listdir would skip while still reporting a "clean" 0.
    """
    storage = str(tmp_path / "a-store")
    os.makedirs(storage, exist_ok=True)
    lock, _pidf = _write_lock(os.path.join(storage, "certificates"), 99999)
    runner = ProcRunner(ps_lines=["1 /sbin/launchd"])
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    monkeypatch.setattr(os, "listdir",
                        lambda *_a, **_k: (_ for _ in ()).throw(PermissionError(13, "denied")))
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)   # never even looked at
    assert storage in _warnings(mock_logger)


def test_clear_stale_locks_warns_undecided_instead_of_a_silent_zero(tmp_path, mock_logger):
    """Kills the `age = age or 0` mutation: an inconclusive command line AND an
    unparseable etime must leave the lock alone AND say so. Before this test,
    that mutation made age default to 0 (falsy None), which read as "the
    process is 0 seconds old" — a real number the started_at comparison could
    act on — so the case silently resolved to LIVE_PID_OURS with no warning at
    all, the exact silent-zero the review's mutation testing caught.
    """
    storage = str(tmp_path / "a-store")
    runner = ProcRunner(ps_lines=["4242"], proc_etime=None)  # bare pid + unparseable age
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    lock, pidf = _write_lock(storage, 4242)
    assert agent.clear_stale_storage_locks() == 0
    assert os.path.exists(lock)
    assert os.path.exists(pidf)
    warnings = _warnings(mock_logger)
    assert "4242" in warnings and "process age could not be determined" in warnings


# ---------------------------------------------------------------------------
# Ordering: the sweep must run BEFORE bootstrap, or launchd starts a process
# that immediately loses to a lock nobody living holds.
# ---------------------------------------------------------------------------

def _agent_with_plist(tmp_path, mock_logger, runner):
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", str(tmp_path / "a-store")),
                   mock_logger, runner=runner)
    os.makedirs(os.path.dirname(agent.plist_path), exist_ok=True)
    with open(agent.plist_path, "wb") as handle:
        handle.write(agent.build_plist())
    return agent


def _fatal_if_bootstrap_runs_first(order):
    def _bootstrap_and_record(*_args, **_kwargs):
        if "clear" not in order:
            raise AssertionError("bootstrap ran before clear_stale_storage_locks")
        order.append("bootstrap")
        return True
    return _bootstrap_and_record


def test_stale_lock_sweep_runs_before_bootstrap_on_start(tmp_path, mock_logger):
    agent = _agent_with_plist(tmp_path, mock_logger, ProcRunner())
    order: list[str] = []
    agent.clear_stale_storage_locks = lambda: order.append("clear") or 0
    agent._bootstrap_and_record = _fatal_if_bootstrap_runs_first(order)
    assert agent.start() is True
    assert order == ["clear", "bootstrap"]


def test_stale_lock_sweep_runs_before_bootstrap_on_restart(tmp_path, mock_logger):
    agent = _agent_with_plist(tmp_path, mock_logger, ProcRunner())
    order: list[str] = []
    agent.clear_stale_storage_locks = lambda: order.append("clear") or 0
    agent._bootstrap_and_record = _fatal_if_bootstrap_runs_first(order)
    assert agent.restart() is True
    assert order == ["clear", "bootstrap"]


# ---------------------------------------------------------------------------
# Finding A (stale-lock review) — the "healthy and untouched" branch of
# _apply_plist used to return BEFORE ever reaching clear_stale_storage_locks()
# further down. That branch fires whenever the plist digest matches, the
# managed job has a live pid, and there is no orphan to reap — exactly what a
# reboot-reused pid's crash-loop looks like the instant a plugin reload
# samples it: launchd keeps respawning the job every ~10s, the reused pid
# belongs to someone else's legitimate process so reap_orphan_servers()
# correctly does not touch it, and the sweep further below was never reached.
# That is precisely the remedy ("reload the plugin") a stuck user reaches for
# first.
# ---------------------------------------------------------------------------

def test_healthy_untouched_apply_plist_still_reaches_the_sweep(tmp_path, mock_logger):
    """Fatal-dependency style: if the healthy/unobstructed branch stopped calling
    clear_stale_storage_locks(), this would return normally instead of raising."""
    agent = _agent_with_plist(tmp_path, mock_logger, ProcRunner(print_pid=5423))
    agent._record_applied_digest(agent._digest_of(agent.build_plist()))

    def _boom():
        raise AssertionError("swept")

    agent.clear_stale_storage_locks = _boom
    with pytest.raises(AssertionError, match="swept"):
        agent.ensure_installed()


def test_healthy_untouched_apply_plist_still_returns_false(tmp_path, mock_logger):
    """The sweep must not change the early-return contract: False still means
    'nothing was re-bootstrapped' — a crash-looping agent stays launchd's to
    respawn, and a cleared lock just lets the NEXT respawn succeed."""
    agent = _agent_with_plist(tmp_path, mock_logger, ProcRunner(print_pid=5423))
    agent._record_applied_digest(agent._digest_of(agent.build_plist()))
    swept = []
    agent.clear_stale_storage_locks = lambda: swept.append(1) or 0
    assert agent.ensure_installed() is False
    assert swept == [1]


# ---------------------------------------------------------------------------
# Finding B (stale-lock review) — reap_orphan_servers()'s SIGTERM path polls
# until the signalled pids are gone; the SIGKILL branch used to signal and
# return immediately. All three sweep call sites run
# clear_stale_storage_locks() microseconds later, and `ps` still lists a
# just-KILLed pid, with its full (still-matching) command line, for a beat
# after kill(2) returns — a dying/zombie process, not a gone one. That made
# the sweep match it as LIVE_PID_OURS and leave its lock in place: the worst
# case to miss, because a SIGKILLed matter-server never runs matter.js's exit
# handler and so has DEFINITELY not released its lock.
# ---------------------------------------------------------------------------

class KillLingersRunner(ProcRunner):
    """A SIGKILLed pid stays visible in ``ps`` — with its full, matching command
    line intact — for ``linger`` more ``ps`` polls after ``kill -KILL`` returns,
    before the kernel actually reaps it. Models the exact gap Finding B closes:
    SIGKILL signals and (pre-fix) returned immediately, with nothing waiting for
    the corpse to actually leave ``ps``.
    """

    def __init__(self, *args, linger: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self._linger = linger
        self._lingering: dict[str, int] = {}

    def __call__(self, cmd, **kwargs):
        if cmd and cmd[0] == "kill" and cmd[1].lstrip("-") == "KILL":
            pid = cmd[2]
            self.calls.append(cmd)
            self.signals.append(("KILL", pid))
            self._lingering[pid] = self._linger
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd and cmd[0] == "ps" and "etime=" not in cmd:
            self.calls.append(cmd)
            for pid, remaining in list(self._lingering.items()):
                if remaining <= 0:
                    self.ps_lines = [ln for ln in self.ps_lines if ln.split()[0] != pid]
                    del self._lingering[pid]
                else:
                    self._lingering[pid] = remaining - 1
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(self.ps_lines) + "\n", stderr="")
        return super().__call__(cmd, **kwargs)


def test_reap_waits_after_sigkill_so_the_sweep_never_sees_a_corpse_as_live(tmp_path,
                                                                            mock_logger):
    storage = str(tmp_path / "a-store")
    runner = KillLingersRunner(ignore_term=True, linger=2)
    agent = _agent(tmp_path / "home", _spec("com.example.a", "pkg-a", storage), mock_logger,
                   runner=runner)
    # A baseline entry that outlives 545's removal, so an empty `ps` output never
    # reads as "the probe is unusable" (see e.g. test_clear_stale_locks_clears_a_dead_pid).
    runner.ps_lines = ["1 /sbin/launchd", _proc_line(agent, 545)]
    _write_lock(storage, 545)  # the pid we are about to KILL also holds the storage lock

    assert agent.reap_orphan_servers() == 1
    assert ("TERM", "545") in runner.signals
    assert ("KILL", "545") in runner.signals

    # By the time reap_orphan_servers() has returned, 545 must actually be gone from
    # `ps` — proving the wait, not just the signal, happened — so the sweep that every
    # real call site runs right after can tell the lock is stale and clear it.
    assert agent.clear_stale_storage_locks() == 1
