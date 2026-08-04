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
