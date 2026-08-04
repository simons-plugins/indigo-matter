"""matter-server process management — the controller LaunchAgent (PM-B).

Manages a launchd LaunchAgent that runs matter-server, per IMPLEMENTATION.md §1.4.
The ``matter-server`` npm package ships ``"bin": null`` (true through at least 1.2.2)
— there is NO ``matter-server`` executable — so ``npx matter-server`` fails with
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

**Structure.** The launchd/npm/orphan-reaping machinery lives in
:mod:`launch_agent` and is driven by a frozen :class:`~launch_agent.AgentSpec`,
because the plugin will manage a SECOND agent (the Matter bridge node, PRD §4.2 /
XOQ3) and that machinery must not be duplicated. What stays here is everything
matter-server-specific: its identity (label, package, pinned version, entry point,
log names), the prefs that configure it, and its command line. ``ServerProcess``
is that specialisation — same public API as before the split.

Paths and the subprocess runner are injectable so the whole module is unit-testable
without touching the real launchd or filesystem.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Callable, Optional

from launch_agent import AgentSpec, LaunchAgent, expand_home

LABEL = "com.simons-plugins.indigo-matter"
MATTER_SERVER_PACKAGE = "matter-server"
# Version installed by install(). Pinned (exact, not caret) for reproducibility — the
# package is fast-moving pre-1.0-style Alpha/Beta. Kept in one place so a version bump
# is a one-line change (matches docs/INSTALL.md). NOTE: 1.2.2 requires Node >= 22.13.0
# (enforced by launch_agent.MIN_NODE_VERSION).
DEFAULT_INSTALL_SPEC = "matter-server@1.2.2"
# Fallback entry point if the package's package.json is missing/unreadable. Matches the
# "main" of both 0.6.x and 1.2.x ("dist/esm/MatterServer.js"); _server_entry() reads the
# real value from the installed package.json and only falls back to this.
DEFAULT_SERVER_ENTRY = "dist/esm/MatterServer.js"
SERVER_OUT_LOG = "matter-server.log"
SERVER_ERR_LOG = "matter-server.err.log"
# Records the sha256 of the plist launchd was last told to load (bootstrap). launchd
# caches a job's ProgramArguments at bootstrap time — rewriting the plist FILE does not
# touch an already-loaded job — so we compare against this to tell "current definition
# already running" (leave the healthy server alone) from "stale job loaded" (reload). See
# LaunchAgent._apply_plist(). Lives beside INSTALL_NODE_STAMP in project_dir (the npm
# install dir), NOT log_dir: a marker in a logs folder is easily lost to log cleanup, and
# losing it would force a needless restart of a healthy server — the very cost this avoids.
#
# The marker is per-agent (AgentSpec.applied_marker_name) so a second agent sharing
# project_dir can't clobber this one's digest. The CONTROLLER deliberately keeps the
# original, un-suffixed filename rather than migrating to `.launchagent-<label>.sha256`:
# every existing install already has this file, and a rename — even one that copies the
# old value across — is a change that can only go wrong (a failed/partial migration reads
# as "no marker", which forces a bootout+bootstrap of a healthy server and drops every
# device's CASE session). Keeping the name is the strictly behaviour-preserving choice;
# only agents added later take the per-label default.
APPLIED_PLIST_MARKER = ".launchagent.sha256"


_PREF_TRUE = ("true", "yes", "on", "1")
_PREF_FALSE = ("false", "no", "off", "0", "")


def _pref_flag(value: Any) -> bool:
    """Parse a checkbox pref; strings need an explicit affirmative, else it's off.

    Saved Indigo checkbox prefs arrive as real bools, but a pref can also reach us
    as a string (``"true"``/``"false"``), and ``bool("false")`` is True. For a flag
    that relaxes device attestation, failing open is the wrong direction, so strings
    are parsed strictly and anything unrecognised is off. Non-strings fall back to
    ``bool()``.
    """
    if isinstance(value, str):
        return value.strip().lower() in _PREF_TRUE
    return bool(value)


def _pref_unrecognised(value: Any) -> bool:
    """True for a string pref that is neither a recognised yes nor a recognised no.

    Such a value reads as OFF, which for this flag is the safe direction — but it is
    the one remaining way the user's choice can be dropped with nothing in the log, so
    the caller warns. Non-strings are never unrecognised (``bool()`` is total).
    """
    return isinstance(value, str) and value.strip().lower() not in _PREF_TRUE + _PREF_FALSE


def _port_number(port: str) -> Optional[int]:
    """The port as an int for the EADDRINUSE/orphan logic, or None if not numeric.

    ``self.port`` stays a string because that is what the CLI takes; the spec wants a
    number because that is what ``lsof -iTCP:`` takes. A non-numeric pref would have
    made lsof fail and report nothing, so None (skip the port signal) is the same
    outcome, reached without shelling out.
    """
    try:
        return int(port)
    except (TypeError, ValueError):
        return None


def matter_server_arguments(agent: "ServerProcess") -> list[str]:
    """Build matter-server's command line. The argv hook of the controller AgentSpec.

    Deliberately a per-agent function rather than a generic flag builder: the
    ``--enable-test-net-dcl`` ordering hazard below cannot survive generalisation.
    """
    # node <package main> … — NOT `npx matter-server`: the matter-server npm
    # package ships "bin": null, so npx cannot resolve an executable and the
    # LaunchAgent respawn-loops with "could not determine executable to run".
    args = [
        agent.node_path,
        agent._server_entry(),  # pylint: disable=protected-access
        "--port", agent.port,
        "--listen-address", agent.listen_address,
        "--storage-path", agent.storage_path,
        "--primary-interface", agent.primary_interface,
    ]
    if agent.enable_test_net_dcl:
        # MUST stay last. matter-server declares this as "--enable-test-net-dcl
        # [value]" (optional value), so commander consumes any following token that
        # does not start with "-" as its value. A boolean-ish one ("false", "0", …)
        # is swallowed SILENTLY and can turn the flag off; anything else fails
        # parsing and aborts startup, which under KeepAlive is a respawn loop — the
        # same class of failure the npx note above exists to prevent. Append new
        # flags BEFORE this one.
        args.append("--enable-test-net-dcl")
    return args


class ServerProcess(LaunchAgent):
    """Install / control the matter-server LaunchAgent.

    The controller specialisation of :class:`~launch_agent.LaunchAgent`: it reads
    matter-server's prefs, builds its :class:`~launch_agent.AgentSpec`, and adds the
    attestation warning. Everything else is inherited.
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
        # Resolved here (not left to the base) because the storage path — which the
        # spec carries — expands against it.
        resolved_home = home or os.path.expanduser("~")
        # Port. In LOCAL mode the WS client hardcodes ws://localhost:5580/ws, so the
        # server must listen on 5580 too — the matterServerPort field is hidden in
        # local mode (its defaultValue never applies) and would otherwise reach the
        # CLI as "" → matter-server "Invalid integer:" crash-loop, or as a stale value
        # that diverges from what the client dials. Force 5580 in local mode; in remote
        # mode honour the pref but still fall back on blank (prefs.get only defaults on
        # an ABSENT key, not a present-but-empty one).
        if str(prefs.get("serverLocation") or "").strip().lower() == "local":
            port = "5580"
        else:
            port = str(prefs.get("matterServerPort") or "").strip() or "5580"
        default_storage = f"~/Library/Application Support/{LABEL}/matter-server"
        raw_storage = str(prefs.get("storagePath") or "").strip() or default_storage
        storage_path = expand_home(raw_storage, resolved_home)
        super().__init__(
            AgentSpec(
                label=LABEL,
                package=MATTER_SERVER_PACKAGE,
                install_spec=DEFAULT_INSTALL_SPEC,
                default_entry=DEFAULT_SERVER_ENTRY,
                storage_path=storage_path,
                out_log=SERVER_OUT_LOG,
                err_log=SERVER_ERR_LOG,
                argv=matter_server_arguments,
                port=_port_number(port),
                applied_marker=APPLIED_PLIST_MARKER,
            ),
            prefs, logger,
            home=resolved_home, npx_path=npx_path,
            runner=runner, exists=exists, sleep=sleep,
        )
        self._port = port
        self.primary_interface = str(prefs.get("primaryInterface") or "").strip() or "en0"
        # Address the matter-server WebSocket control API binds to. It binds to ALL
        # interfaces when no --listen-address is given, and the
        # control WS is UNAUTHENTICATED — so the safe default is loopback only. An
        # empty/whitespace pref must never leak through (that would re-expose all
        # interfaces), hence the explicit fall back to 127.0.0.1. This is distinct
        # from matterServerHost ("localhost"), which is the CLIENT connect target.
        self.listen_address = str(prefs.get("matterServerListenAddress", "127.0.0.1")).strip() or "127.0.0.1"
        # Also trust the test-net DCL (device attestation roots for uncertified
        # devices) on top of the production DCL, and allow test-net OTA images.
        # Off by default: a device that fails attestation is one whose certificate
        # chain we can't verify against production Matter roots. Needed to commission
        # dev/test bridges (Homebridge's Matter accessory server currently presents a
        # test PAA). Verified against matter-server 1.2.2 (DEFAULT_INSTALL_SPEC), which
        # declares it as an optional-value option, so the bare flag means "on".
        raw_test_net_dcl = prefs.get("enableTestNetDcl", False)
        self.enable_test_net_dcl = _pref_flag(raw_test_net_dcl)
        if _pref_unrecognised(raw_test_net_dcl):
            self.logger.warning(
                "the 'Allow test/development device certificates' pref has the "
                "unrecognised value %r — treating it as OFF. Set it by ticking or "
                "unticking the checkbox in Configure….", raw_test_net_dcl,
            )

    @property
    def port(self) -> str:
        """The CLI port string, read-only. ``spec.port`` (the lsof/EADDRINUSE signal)
        is frozen from the same pref at construction; a post-construction ``sp.port =``
        would change the command line but not the port the orphan reaper polices.
        Both derive from prefs — rebuild the ServerProcess to change them."""
        return self._port

    def install(self, install_spec: str = DEFAULT_INSTALL_SPEC) -> bool:
        """npm-install matter-server. Signature pins the default for callers/docs."""
        return super().install(install_spec)

    def _warn_on_settings(self) -> None:
        """Warn, on every ensure_installed(), while device attestation is relaxed."""
        if not self.enable_test_net_dcl:
            return
        # Every call, not just on change: the hazard of this setting is that it is
        # ticked once to pair one device and then forgotten, and nothing else in the
        # log says the fabric accepts unverified certificate chains. Warning level so
        # it survives verboseLogging being off.
        #
        # Deliberately phrased as what we are STARTING matter-server with, not as the
        # state of whatever is currently listening: this is derived from a prefs
        # snapshot, and a stray server that outlived its LaunchAgent can still be
        # serving with different arguments (see docs/INSTALL.md troubleshooting).
        self.logger.warning(
            "starting matter-server with RELAXED device attestation "
            "(--enable-test-net-dcl): it additionally trusts the Matter test-net "
            "DCL, so devices with test/development certificates are accepted and "
            "test-net OTA firmware may be offered to them. Untick 'Allow "
            "test/development device certificates' in Configure… when you no "
            "longer need it."
        )
