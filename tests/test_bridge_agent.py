"""E7: the export bridge node's LaunchAgent (`bridge_agent`).

``test_launch_agent.py`` pins what the ``AgentSpec`` extraction ADDS in general;
this file pins the bridge's own specialisation — the identity it carries, the
command line it builds, and the two things that would be silently wrong if they
drifted: the storage path (which the fabric backup archives and the node writes)
and the loopback port (which the plugin's client dials).

Every agent here is built against a ``tmp_path`` home. Never construct one
against the real ``$HOME``: ``ensure_installed()``'s preflight-failure path calls
``uninstall()``, which would delete the developer's live LaunchAgent.
"""
from __future__ import annotations

import os

import pytest

import bridge_agent
import bridge_protocol
from bridge_agent import BridgeProcess
from server_process import APPLIED_PLIST_MARKER, LABEL as CONTROLLER_LABEL, ServerProcess

from test_server_process import FakeRunner


def _bridge(tmp_path, mock_logger, prefs=None, installed: bool = True) -> BridgeProcess:
    """A BridgeProcess whose node and package entry exist, so preflight() passes."""
    home = tmp_path / "home"
    bindir = home / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / "npx").write_text("#!/bin/sh\n")
    (bindir / "node").write_text("#!/bin/sh\n")
    if installed:
        entry = (home / "indigo-matter" / "node_modules" / bridge_agent.BRIDGE_PACKAGE
                 / "dist" / "main.js")
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake entry\n")
    return BridgeProcess(dict(prefs or {}), mock_logger, home=str(home),
                         npx_path=str(bindir / "npx"), runner=FakeRunner(),
                         sleep=lambda *_a: None)


# ---------------------------------------------------------------------------
# Identity: the bridge must never be mistakable for the controller
# ---------------------------------------------------------------------------

def test_the_bridge_is_a_second_agent_with_its_own_everything(tmp_path, mock_logger):
    """Label, package, storage, logs and marker all distinct from the controller.

    Every one of these being separate is what lets both agents live in one npm
    root and one LaunchAgents directory without either standing on the other.
    """
    bridge = _bridge(tmp_path, mock_logger)
    controller = ServerProcess({}, mock_logger, home=str(tmp_path / "home"),
                               npx_path=str(tmp_path / "home" / "bin" / "npx"),
                               runner=FakeRunner())
    assert bridge.spec.label == "com.simons-plugins.indigo-matter.bridge"
    assert bridge.spec.label != controller.spec.label
    assert bridge.spec.package == "indigo-matter-bridge" != controller.spec.package
    assert bridge.storage_path != controller.storage_path
    assert bridge.plist_path != controller.plist_path
    assert bridge.spec.out_log != controller.spec.out_log
    assert bridge.spec.err_log != controller.spec.err_log
    # The controller keeps the legacy un-suffixed marker (every install has it);
    # a later agent takes the per-label default. Sharing it would make each read
    # the other's digest and bootout a healthy job on every reload.
    assert bridge.spec.applied_marker_name != APPLIED_PLIST_MARKER
    assert bridge.spec.label in bridge.spec.applied_marker_name


def test_the_install_spec_is_a_registry_pin(tmp_path, mock_logger):
    """One constant, exact-pinned, exactly like matter-server@1.2.2.

    This is the whole point of the AgentSpec extraction: installing the bridge is
    ``npm install --prefix ~/indigo-matter <spec>`` with a different string, not
    a second install mechanism.
    """
    bridge = _bridge(tmp_path, mock_logger)
    assert bridge.spec.install_spec == bridge_agent.DEFAULT_INSTALL_SPEC
    name, _, version = bridge_agent.DEFAULT_INSTALL_SPEC.partition("@")
    assert name == bridge_agent.BRIDGE_PACKAGE
    assert version and not version.startswith(("^", "~")), "must be exact-pinned, not a range"


def test_the_default_entry_matches_the_packages_main(tmp_path, mock_logger):
    """The fallback entry is only a fallback if it agrees with package.json."""
    import json
    from pathlib import Path

    manifest = json.loads((Path(__file__).parent.parent / "bridge-node" / "package.json")
                          .read_text(encoding="utf-8"))
    # ⊗ The `name` was the one field of the three left unasserted, and it is the
    # one `npm install <spec>` resolves, `ps` is matched against when reaping
    # orphans, and `node_modules/<name>` is looked up under. A rename on either
    # side would install one package and look for another.
    assert manifest["name"] == bridge_agent.BRIDGE_PACKAGE
    assert manifest["main"] == bridge_agent.DEFAULT_BRIDGE_ENTRY
    # The pin may TRAIL package.json (node source is published on its own
    # schedule; the plugin adopts a published version when it is ready) but must
    # never be AHEAD of it: a pin newer than the source tree names a version
    # that cannot have been published from here, and every install would 404.
    pin = tuple(int(p) for p in bridge_agent.DEFAULT_INSTALL_SPEC.partition("@")[2].split("."))
    package = tuple(int(p) for p in manifest["version"].split("."))
    assert pin <= package, "DEFAULT_INSTALL_SPEC must not pin a version ahead of package.json"
    bridge = _bridge(tmp_path, mock_logger)
    assert bridge.spec.default_entry == bridge_agent.DEFAULT_BRIDGE_ENTRY


# ---------------------------------------------------------------------------
# Storage: one derivation, three consumers
# ---------------------------------------------------------------------------

def test_storage_is_the_siblING_of_the_controllers(tmp_path, mock_logger):
    bridge = _bridge(tmp_path, mock_logger)
    home = str(tmp_path / "home")
    expected = os.path.join(home, "Library", "Application Support", CONTROLLER_LABEL,
                            "bridge-node")
    assert bridge.storage_path == expected


def test_storage_follows_a_relocated_controller_storage_path(tmp_path, mock_logger):
    """A user who moves the controller's storage moves the bridge's with it.

    Derived, not configured — and derived from the pref the fabric backup also
    reads, so the directory backed up and the directory the node writes cannot
    disagree.
    """
    bridge = _bridge(tmp_path, mock_logger, {"storagePath": "~/elsewhere/matter-server"})
    assert bridge.storage_path == os.path.join(str(tmp_path / "home"), "elsewhere",
                                               "bridge-node")


def test_the_plugin_and_the_agent_derive_the_same_storage_path():
    """`bridge_storage_path` is the single derivation both sides call."""
    assert bridge_agent.bridge_storage_path("/x/y/matter-server") == "/x/y/bridge-node"
    # A trailing slash must not produce ".../matter-server/bridge-node".
    assert bridge_agent.bridge_storage_path("/x/y/matter-server/") == "/x/y/bridge-node"


# ---------------------------------------------------------------------------
# argv: the node validates every flag and refuses to start on a bad one
# ---------------------------------------------------------------------------

def test_program_arguments_run_node_on_the_package_main(tmp_path, mock_logger):
    bridge = _bridge(tmp_path, mock_logger)
    args = bridge.program_arguments()
    assert args[0].endswith("/node") and not args[0].endswith("/npx")
    assert args[1].endswith(f"/node_modules/{bridge_agent.BRIDGE_PACKAGE}/dist/main.js")
    assert args[args.index("--storage-path") + 1] == bridge.storage_path
    assert args[args.index("--ws-port") + 1] == "5581"
    assert args[args.index("--matter-port") + 1] == "5540"


def test_ports_come_from_prefs(tmp_path, mock_logger):
    bridge = _bridge(tmp_path, mock_logger,
                     {bridge_protocol.PREF_WS_PORT: "5599",
                      bridge_agent.PREF_MATTER_PORT: "5541"})
    args = bridge.program_arguments()
    assert args[args.index("--ws-port") + 1] == "5599"
    assert args[args.index("--matter-port") + 1] == "5541"
    # The reaper's EADDRINUSE signal is the PROTOCOL port, and it must track it.
    assert bridge.spec.port == 5599


@pytest.mark.parametrize("bad", ["", "   ", "not-a-port", "0", "70000", None])
def test_an_unusable_port_pref_falls_back_rather_than_reaching_the_cli(bad, tmp_path,
                                                                      mock_logger):
    """⊗ The controller's pre-2026.7.1 crash-loop, not repeated here.

    `config.ts` throws on a value it cannot parse AND on a flag with no value —
    deliberately, so a bad plist fails loudly. Under KeepAlive that is a respawn
    loop, so a blank pref must never reach the command line.
    """
    bridge = _bridge(tmp_path, mock_logger,
                     {bridge_protocol.PREF_WS_PORT: bad, bridge_agent.PREF_MATTER_PORT: bad})
    args = bridge.program_arguments()
    assert args[args.index("--ws-port") + 1] == bridge_protocol.DEFAULT_WS_PORT
    assert args[args.index("--matter-port") + 1] == bridge_agent.DEFAULT_MATTER_PORT
    assert "" not in args


# ---------------------------------------------------------------------------
# #222: a non-default Matter port is known to break Alexa — warn, don't refuse
# ---------------------------------------------------------------------------

def test_ensure_installed_warns_on_a_non_default_matter_port(tmp_path, mock_logger):
    bridge = _bridge(tmp_path, mock_logger, {bridge_agent.PREF_MATTER_PORT: "5541"})
    bridge.ensure_installed()
    warned = [c for c in mock_logger.warning.call_args_list if "5541" in str(c)]
    assert warned and "Alexa" in str(warned[0]) and "ONE Matter node per host" in str(warned[0])


def test_ensure_installed_is_quiet_on_the_default_matter_port(tmp_path, mock_logger):
    bridge = _bridge(tmp_path, mock_logger)
    bridge.ensure_installed()
    assert not any("Matter node per host" in str(c) for c in mock_logger.warning.call_args_list)


def test_the_port_warning_fires_on_every_startup_not_only_on_change(tmp_path, mock_logger):
    # Same discipline as the controller's attestation warning: the hazard is
    # forgetting a non-default port was ever set, so every ensure_installed()
    # must repeat it, not just the first.
    bridge = _bridge(tmp_path, mock_logger, {bridge_agent.PREF_MATTER_PORT: "5541"})
    bridge.ensure_installed()
    bridge.ensure_installed()
    warned = [c for c in mock_logger.warning.call_args_list if "5541" in str(c)]
    assert len(warned) == 2


def test_the_mdns_flag_is_omitted_when_no_interface_is_pinned(tmp_path, mock_logger):
    """The node refuses to start on an interface this host does not have.

    Right when the user asked for one; wrong when we invented an empty string —
    so the flag is absent rather than blank.
    """
    assert "--mdns-interface" not in _bridge(tmp_path, mock_logger).program_arguments()
    assert "--mdns-interface" not in _bridge(tmp_path, mock_logger,
                                             {"primaryInterface": "  "}).program_arguments()


def test_the_mdns_flag_reuses_the_controllers_interface_pref(tmp_path, mock_logger):
    args = _bridge(tmp_path, mock_logger, {"primaryInterface": "en1"}).program_arguments()
    assert args[args.index("--mdns-interface") + 1] == "en1"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_constructing_one_writes_nothing(tmp_path, mock_logger):
    """⊗ XAC1: a fresh install must be completely inert.

    This is what lets the plugin build a BridgeProcess lazily from a menu
    callback or a device-delete without installing a bridge nobody asked for.
    """
    bridge = _bridge(tmp_path, mock_logger, installed=False)
    assert not os.path.exists(bridge.plist_path)
    assert not os.path.exists(bridge.storage_path)
    assert not os.path.exists(bridge.log_dir)


def test_ensure_installed_writes_the_plist_and_creates_the_storage(tmp_path, mock_logger):
    bridge = _bridge(tmp_path, mock_logger)
    assert bridge.ensure_installed() is True
    assert os.path.exists(bridge.plist_path)
    assert os.path.isdir(bridge.storage_path)
    import plistlib
    with open(bridge.plist_path, "rb") as handle:
        plist = plistlib.load(handle)
    assert plist["Label"] == bridge_agent.LABEL
    assert plist["ProgramArguments"] == bridge.program_arguments()
    assert plist["StandardErrorPath"].endswith(bridge_agent.BRIDGE_ERR_LOG)


def test_preflight_names_the_bridge_install_action_when_the_package_is_missing(tmp_path,
                                                                               mock_logger):
    """A user reading this must be sent at the BRIDGE's install menu, not the
    controller's — the two packages are separately versioned and separately
    installed."""
    bridge = _bridge(tmp_path, mock_logger, installed=False)
    problem = bridge.preflight()
    assert problem is not None
    assert bridge_agent.BRIDGE_PACKAGE in problem
    assert bridge_agent.DEFAULT_INSTALL_SPEC in problem


def _menu_names() -> set:
    """Every ``<Name>`` in MenuItems.xml, as the Plugins ▸ Matter menu shows it."""
    import xml.etree.ElementTree as ET
    from pathlib import Path

    root = ET.parse(Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
                    / "Contents" / "Server Plugin" / "MenuItems.xml").getroot()
    return {item.findtext("Name") for item in root.findall("MenuItem")}


def test_the_messages_name_a_menu_item_that_actually_EXISTS(tmp_path, mock_logger):
    """⊗ They named the npm package instead.

    ``preflight`` and ``abi_warning`` both interpolated ``spec.package``,
    producing "Plugins ▸ Matter ▸ Install/update indigo-matter-bridge" — there
    is no such menu item. This fires on the first-run path, where the user is
    already stuck, so a name they cannot find in the menu is the difference
    between a fixable state and giving up.
    """
    bridge = _bridge(tmp_path, mock_logger, installed=False)
    problem = bridge.preflight()
    assert bridge.spec.install_menu_name in problem
    assert bridge.spec.install_menu_name in _menu_names(), \
        "the message sends the user at a menu item that does not exist"
    assert f"Install/update {bridge_agent.BRIDGE_PACKAGE}" not in problem


def test_the_controllers_message_names_ITS_menu_item(tmp_path, mock_logger):
    from server_process import INSTALL_MENU

    controller = ServerProcess({}, mock_logger, home=str(tmp_path / "nowhere"),
                               npx_path=str(tmp_path / "nowhere" / "bin" / "npx"),
                               runner=FakeRunner())
    assert controller.spec.install_menu_name == INSTALL_MENU
    assert INSTALL_MENU in _menu_names()


def test_an_agent_with_no_menu_still_gets_a_usable_sentence():
    """The fallback is the old wording, which is right for an agent that has no
    Install/update menu at all."""
    from launch_agent import AgentSpec

    spec = AgentSpec(label="l", package="some-pkg", install_spec="some-pkg@1",
                     default_entry="m.js", storage_path="/s", out_log="o", err_log="e",
                     argv=lambda _agent: [])
    assert spec.install_menu_name == "Install/update some-pkg"


def test_uninstall_never_touches_the_storage(tmp_path, mock_logger):
    """The pairings of every ecosystem live in there. Sacred both ways."""
    bridge = _bridge(tmp_path, mock_logger)
    bridge.ensure_installed()
    bridge.uninstall()
    assert not os.path.exists(bridge.plist_path)
    assert os.path.isdir(bridge.storage_path)
