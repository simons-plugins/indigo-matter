"""The Matter **bridge node** LaunchAgent (PRD-indigo-matter-export §4.2).

The second of the plugin's two launchd-managed node processes, and the reason
:mod:`launch_agent` exists at all: the controller's hard-won recovery machinery
(applied-plist digest, loaded-but-dead revival, orphan/EADDRINUSE reaping) is
shared verbatim, and everything that differs is carried by a frozen
:class:`~launch_agent.AgentSpec`. :class:`BridgeProcess` is to the bridge node
what :class:`server_process.ServerProcess` is to the controller.

Three differences from the controller agent, all of them deliberate:

* **It is not installed and not started while the allow-list is empty (XG5 /
  XAC1).** A fresh install must be completely inert — no plist, no process, no
  log noise — so nothing here is called from ``startup``. The empty→non-empty
  transition in :class:`export_bridge.ExportBridge` is what brings it up, and
  non-empty→empty (after the un-export has actually landed) is what takes it
  down. Constructing a ``BridgeProcess`` is side-effect free; ``ensure_installed``
  is the first thing that writes anything.
* **Its storage dir is derived, not configured.** It is the sibling of the
  controller's (PRD §4.3), which is also ``bridge-node/src/config.ts``'s
  ``DEFAULT_STORAGE_PATH``. One derivation, used by the agent, by the fabric
  backup, and by the plugin — see :func:`bridge_storage_path`.
* **A failure here degrades export only.** The inbound controller is a separate
  agent with a separate label, package, storage path and port; nothing in this
  module can reach it. Callers wrap agent operations so that a launchd fault
  never propagates into an Indigo callback.

**Distribution.** ``indigo-matter-bridge`` is an npm package, exact-pinned by
:data:`DEFAULT_INSTALL_SPEC` exactly as ``matter-server@1.2.2`` is — which is
precisely the parameterisation the ``AgentSpec`` extraction was for:
installing it is ``npm install --prefix ~/indigo-matter <spec>`` with a
different string. The plugin bundle ships **no JavaScript**; ``bridge-node/`` is
a top-level source directory in the repo and is published from there.

**It is published**: 0.5.0 onward is on the registry, so
:meth:`BridgeProcess.install` resolves :data:`DEFAULT_INSTALL_SPEC` off npm like
any other dependency. The pin is deliberate and moves by hand — see
``bridge-node/README.md`` § Releasing for the release steps, and for the
local-install recipe that is still the fastest way to test an unreleased
node.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Callable, Optional

from launch_agent import AgentSpec, LaunchAgent, expand_home
from server_process import LABEL as CONTROLLER_LABEL

#: launchd job label. A SUFFIX of the controller's, never the same string:
#: the label is the plist filename stem and launchd's job identity, so sharing
#: one would have each agent bootout the other on every reload.
LABEL = "com.simons-plugins.indigo-matter.bridge"

#: The npm package name. Matched in ``ps`` output when reaping orphans and used
#: as the directory under ``node_modules`` holding the entry point.
BRIDGE_PACKAGE = "indigo-matter-bridge"

#: Version installed by :meth:`BridgeProcess.install`. Exact-pinned (no caret)
#: for the same reason ``matter-server`` is: this package pins matter.js exactly,
#: and matter.js patch releases have changed what Apple Home renders with no code
#: change on the bridge side. **This is the one line a release bumps**, in step
#: with ``bridge-node/package.json``'s ``version``.
DEFAULT_INSTALL_SPEC = f"{BRIDGE_PACKAGE}@0.16.0"

#: Fallback entry point when the installed package's ``package.json`` is missing
#: or unreadable. Must match ``bridge-node/package.json``'s ``main``;
#: ``LaunchAgent._server_entry()`` reads the real value and only falls back here.
DEFAULT_BRIDGE_ENTRY = "dist/main.js"

BRIDGE_OUT_LOG = "bridge-node.log"
BRIDGE_ERR_LOG = "bridge-node.err.log"

#: The EXACT wording of ``MenuItems.xml``'s ``installBridgeNode`` item. Every
#: message that sends a user there used to interpolate the package name instead,
#: producing "Plugins ▸ Matter ▸ Install/update indigo-matter-bridge" — a menu
#: that does not exist. It fires on the first-run path (no package, so no plist,
#: so the preflight error), where a name the user cannot find in the menu is the
#: difference between a fixable state and giving up.
BRIDGE_INSTALL_MENU = "Install/update the Matter bridge"

#: Matter UDP port the node binds (PRD §4.4). 5540 is Matter's default and the
#: one matter.js's ECOSYSTEMS.md records as Alexa's hard requirement; the pref is
#: the escape hatch when another Matter stack on the same Mac already holds it.
PREF_MATTER_PORT = "bridgeMatterPort"
DEFAULT_MATTER_PORT = "5540"

#: Reused from the controller rather than given its own pref: matter.js's mDNS
#: stack defaults to every interface and breaks on Macs with VPN/utun interfaces,
#: which is the same fault, on the same host, that the controller's pref exists
#: for. A blank value means "let matter.js decide" and the flag is omitted.
PREF_PRIMARY_INTERFACE = "primaryInterface"


def bridge_storage_path(controller_storage_path: str) -> str:
    """The bridge node's storage dir, given the controller's (PRD §4.3).

    Derived rather than configured, and derived in exactly ONE place, because
    three callers have to agree on it or the disagreement is silent: this agent
    (which passes it as ``--storage-path``), the fabric backup (which archives
    it), and ``bridge-node/src/config.ts``'s ``DEFAULT_STORAGE_PATH`` (which is
    what the node uses if nobody passes the flag). A mismatch means a node
    serving from one directory while the plugin backs up another — a backup that
    silently contains nothing, discovered at restore time.

    SACRED, for two independent reasons: it holds the operational credentials of
    every ecosystem fabric the bridge has joined (losing them un-pairs the lot)
    and the endpoint-number witness (losing that duplicates every accessory in
    every ecosystem). Created here, never deleted here.
    """
    controller = os.path.normpath(controller_storage_path)
    return os.path.join(os.path.dirname(controller), "bridge-node")


def default_controller_storage(home: str) -> str:
    """The controller's default storage dir — the root :func:`bridge_storage_path` hangs off."""
    return expand_home(f"~/Library/Application Support/{CONTROLLER_LABEL}/matter-server", home)


def _port_str(value: Any, fallback: str) -> str:
    """A port as a non-empty numeric string, or ``fallback``.

    ``bridge-node/src/config.ts`` throws on a value it cannot parse *and* on a
    flag whose value is missing — deliberately, so a bad plist fails loudly
    rather than silently running on the wrong port. Under ``KeepAlive`` that is a
    respawn loop, which is the precise failure the controller's blank ``--port``
    caused before 2026.7.1. So a blank or non-numeric pref never reaches the CLI:
    it becomes the documented default here, where it is one log line instead.
    """
    text = str(value or "").strip()
    if text.isdigit() and 1 <= int(text) <= 65535:
        return text
    return fallback


def bridge_arguments(agent: "BridgeProcess") -> list[str]:
    """Build the bridge node's command line. The argv hook of its AgentSpec.

    ``node <package main> …`` rather than ``npx indigo-matter-bridge``: the same
    reasoning as the controller (see :mod:`server_process`) plus one of its own —
    ``npx`` would resolve a *different* copy of the package if one is on PATH,
    and launchd would keep respawning it long after the plugin was upgraded.
    """
    args = [
        agent.node_path,
        agent._server_entry(),  # pylint: disable=protected-access
        "--storage-path", agent.storage_path,
        "--ws-port", agent.ws_port,
        "--matter-port", agent.matter_port,
    ]
    if agent.mdns_interface:
        # Omitted entirely when blank. The node validates the name against the
        # host's real interfaces and refuses to start on a miss, which is right
        # — but only when the user actually asked for one.
        args += ["--mdns-interface", agent.mdns_interface]
    return args


class BridgeProcess(LaunchAgent):
    """Install / control the Matter bridge node's LaunchAgent.

    The bridge specialisation of :class:`~launch_agent.LaunchAgent`: it reads the
    export prefs, builds its :class:`~launch_agent.AgentSpec`, and adds nothing
    else. It deliberately takes the DEFAULT per-label applied-plist marker
    (``.launchagent-com.simons-plugins.indigo-matter.bridge.sha256``) — the
    controller keeps the original un-suffixed filename because every existing
    install already has that file, and this agent has no such history.
    """

    def __init__(
        self,
        prefs: dict,
        logger: Any,
        *,
        home: Optional[str] = None,
        npx_path: Optional[str] = None,
        runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
        exists: Callable[[str], bool] = os.path.exists,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Resolved before super() because the storage path the spec carries
        # expands against it — the same ordering ServerProcess uses.
        resolved_home = home or os.path.expanduser("~")
        raw_controller = str(prefs.get("storagePath") or "").strip()
        controller_storage = (expand_home(raw_controller, resolved_home) if raw_controller
                              else default_controller_storage(resolved_home))
        storage_path = bridge_storage_path(controller_storage)
        # Imported here rather than at module scope: bridge_protocol is the WIRE
        # contract and this is process management, so the dependency runs one way
        # only. The port itself must agree with what BridgeClient dials, which is
        # why it is read from the same constant rather than duplicated.
        import bridge_protocol  # pylint: disable=import-outside-toplevel

        ws_port = _port_str(prefs.get(bridge_protocol.PREF_WS_PORT),
                            bridge_protocol.DEFAULT_WS_PORT)
        super().__init__(
            AgentSpec(
                label=LABEL,
                package=BRIDGE_PACKAGE,
                install_spec=DEFAULT_INSTALL_SPEC,
                default_entry=DEFAULT_BRIDGE_ENTRY,
                storage_path=storage_path,
                out_log=BRIDGE_OUT_LOG,
                err_log=BRIDGE_ERR_LOG,
                argv=bridge_arguments,
                # The loopback protocol port, NOT the Matter UDP port: the
                # orphan reaper's port signal is an `lsof -iTCP` match, and the
                # thing a second bridge node actually contends for on TCP is
                # 5581. 5540 is UDP and a clash there is the node's own
                # first-class §7 failure, reported by the node at startup.
                port=int(ws_port),
                install_menu=BRIDGE_INSTALL_MENU,
            ),
            prefs, logger,
            home=resolved_home, npx_path=npx_path,
            runner=runner, exists=exists, sleep=sleep,
        )
        self._ws_port = ws_port
        self._matter_port = _port_str(prefs.get(PREF_MATTER_PORT), DEFAULT_MATTER_PORT)
        self.mdns_interface = str(prefs.get(PREF_PRIMARY_INTERFACE) or "").strip()

    @property
    def ws_port(self) -> str:
        """The loopback protocol port as the CLI takes it. Read-only.

        ``spec.port`` (the reaper's EADDRINUSE signal) is frozen from the same
        pref at construction, so a post-construction assignment here would change
        the command line without changing the port the reaper polices. Both come
        from prefs — rebuild the ``BridgeProcess`` to change them.
        """
        return self._ws_port

    @property
    def matter_port(self) -> str:
        """The Matter UDP port as the CLI takes it. Read-only, same reasoning."""
        return self._matter_port

    def install(self, install_spec: str = DEFAULT_INSTALL_SPEC) -> bool:
        """npm-install the bridge node. Signature pins the default for callers/docs."""
        return super().install(install_spec)

    def _warn_on_settings(self) -> None:
        """Warn, on every ensure_installed() (that passes preflight), while the Matter
        port is non-default (#222).

        5540 is Matter's own default AND — per matter.js's ECOSYSTEMS.md — the port
        Alexa hard-requires; the ``bridgeMatterPort`` pref exists as an escape hatch
        for a Mac where something else already holds it (PRD §4.4), not as a tuning
        knob. Advisory only: a non-default port is sometimes the only way to get the
        bridge running at all, so this warns rather than refuses.
        """
        # int(), not a string compare: _port_str() returns digits as-is (a pref of
        # "05540" survives with its leading zero), so a plain == would warn about a
        # port that is numerically the default. Both sides are already digit-only
        # per _port_str()'s own contract, so int() here never raises.
        if int(self._matter_port) == int(DEFAULT_MATTER_PORT):
            return
        self.logger.warning(
            "the Matter bridge is starting on port %s, not the default %s: non-standard "
            "Matter ports are known to break Alexa discovery, and Amazon documents "
            "support for only ONE Matter node per host. Use the default unless another "
            "Matter stack on this Mac already holds it.",
            self._matter_port, DEFAULT_MATTER_PORT,
        )
