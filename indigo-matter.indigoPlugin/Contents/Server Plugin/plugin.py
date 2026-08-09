"""indigo-matter — Matter device support for the Indigo home automation server.

Lifecycle glue only. All I/O lives on the asyncio loop owned by
:class:`AsyncRuntime`; this class wires the async services in ``startup``, runs a
non-I/O watchdog in ``runConcurrentThread``, tears everything down in
``shutdown``, bridges Indigo device actions onto the loop, and exposes the Domio
HTTP API as Indigo Web Server hidden-action handlers.

See ``docs/PRD-indigo-matter-plugin.md``, ``docs/IMPLEMENTATION.md`` (protocol +
scaffold) and ``docs/API.md`` (the Domio contract). matter-server protocol field
names are isolated in ``protocol.py``.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import CancelledError as FuturesCancelledError
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import indigo  # provided by the Indigo runtime

import bridge_agent
import bridge_client
import bridge_protocol
import fabric_backup
from async_runtime import AsyncRuntime
from commission_jobs import CommissionJobs, node_id_to_str
from device_sync import DeviceSync
import export_bridge
from export_bridge import ExportBridge
import export_catalog
import export_handlers
from export_store import ExportEntry, ExportStore, OPTION_INVERT
from http_handlers import HttpApi, MatterUnavailable
from matter_client import MatterClient
from matter_handlers.boolean_state_config import (
    ATTR_CURRENT_SENSITIVITY,
    CLUSTER_BOOLEAN_STATE_CONFIG,
)
from matter_handlers.registry import HandlerRegistry
import protocol
from protocol import MatterWrite, Protocol
from server_process import ServerProcess

PLUGIN_NAME = "indigo-matter"
COMMAND_TIMEOUT = 5.0
DECOMMISSION_TIMEOUT = 15.0

#: Deadlines for the export/pairing menu actions, which block the **Indigo UI
#: thread** on a WS round trip: without one, a bridge node that accepts the
#: socket and then stops answering hangs the dialog — and Indigo's client — with
#: no way out but force-quitting it. Named rather than inline because a `.result()`
#: with no timeout looks like an ordinary call at a glance, so nothing about the
#: absence of one is visible at the call site.
#:
#: PAIRING_READ_TIMEOUT covers a plain read (`get_pairing`). The other two are
#: long because the node does real Matter work behind them: opening an enhanced
#: window derives a fresh passcode and re-advertises, and removing a fabric
#: flushes subscriptions and — on the last one — factory-resets the whole stack.
PAIRING_READ_TIMEOUT = 15.0
WINDOW_OPEN_TIMEOUT = 45.0
UNPAIR_TIMEOUT = 45.0
FACTORY_RESET_TIMEOUT = 45.0

#: Watchdog ticks (~15s each) of an active export with no ``deviceUpdated`` at
#: all before ``subscribeToChanges`` is re-issued — see
#: ``Plugin._resubscribe_tick``. ~1 minute, the same shape as every other streak
#: counter here.
RESUBSCRIBE_TICKS = 4
#: How many times, at most. Bounded because a house where nothing changes looks
#: identical to a subscription that never registered.
MAX_RESUBSCRIBE_ATTEMPTS = 3

#: Menu id of the export dialog (MenuItems.xml) — matched in
#: ``get_menu_action_config_ui_values`` so other menus are never seeded.
MENU_MANAGE_EXPORTS = "manageMatterExports"
#: Menu id of the unpair dialog. Seeded for the same reason the export dialog is
#: — Indigo pre-selects the first row of a picker, and this picker's rows are
#: real ecosystems whose Execute button removes them.
MENU_UNPAIR_ECOSYSTEM = "unpairEcosystem"
#: Option-id prefix marking a picker row the user may look at but not choose
#: (PRD §5.2: excluded devices are shown *with a reason*, never hidden — XAC9).
EXCLUDED_OPTION_PREFIX = "x-"
#: The "nothing selected" sentinel. Never "": Indigo rejects an empty list id
#: with "UI dynamic list function returned illegal ID string" and silently
#: drops the option. The picker always emits a REAL row carrying this id
#: (:data:`NO_SELECTION_LABEL`), because the dialog is seeded with it — a
#: seeded value with no matching row renders as a blank first item.
NO_SELECTION_ID = "0"
NO_SELECTION_LABEL = "— select a device —"
#: Informational rows. They get their own ids so :data:`NO_SELECTION_ID` stays
#: unique, and the ``x-`` prefix keeps them unpickable through the same door
#: excluded devices use.
TRUNCATED_OPTION = (f"{EXCLUDED_OPTION_PREFIX}truncated",
                    "…too many matches — narrow the filter")
NO_MATCH_OPTION = (f"{EXCLUDED_OPTION_PREFIX}nomatch", "(no devices match the filter)")
#: What a list callback returns when it fails outright. An empty list would
#: render as an empty popup the user cannot tell from "nothing to choose".
LIST_ERROR_OPTION = (NO_SELECTION_ID, "(error building list — see Event Log)")
#: One unreadable device inside an otherwise fine list (D3): the row is kept so
#: the count is honest, but it is not selectable.
ROW_ERROR_LABEL = "(error reading device — see Event Log)"
#: Picker cap. Past this the tail row asks the user to narrow the filter — a
#: 2000-device database would otherwise build an unusable popup menu.
EXPORT_PICKER_LIMIT = 300


def server_location(prefs: dict) -> str:
    """Resolve the one user-facing choice: is matter-server on this Mac?

    Returns ``"local"`` (the plugin runs and manages matter-server here on
    loopback) or ``"remote"`` (connect to a matter-server elsewhere).

    Migrates pre-2026.6 prefs that predate the ``serverLocation`` menu:
      * a managed LaunchAgent meant the plugin already ran the server here → local;
      * a host pointed at another machine → remote (keep its host/port);
      * anything else — a fresh install or a loopback self-run server → local,
        the turnkey default.
    """
    loc = str(prefs.get("serverLocation") or "").strip().lower()
    if loc in ("local", "remote"):
        return loc
    if prefs.get("manageLaunchAgent", False):
        return "local"
    host = str(prefs.get("matterServerHost") or "").strip().lower()
    if host and host not in ("localhost", "127.0.0.1", "::1"):
        return "remote"
    return "local"


def sanitize_host(raw: str) -> str:
    """Reduce a user-entered host to a bare hostname / IP.

    Users paste full URLs into the host field (e.g. ``http://jobs2.local:8176``);
    a scheme, an embedded port, and any path all corrupt ``ws://{host}:{port}{path}``.
    Strip them so the separate port field stays authoritative. IPv6 literals
    (multiple colons) are left untouched.
    """
    host = str(raw or "").strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]  # drop any /path
    # strip an embedded :PORT (host:1234) but preserve IPv6 literals (many colons)
    if host.count(":") == 1 and host.rsplit(":", 1)[1].isdigit():
        host = host.rsplit(":", 1)[0]
    return host


#: Where the raw ``MT:`` payload can be rendered as a scannable QR code. The
#: CHIP project's own tool, which is the reference implementation of the payload
#: format — so a code it cannot render is a code no commissioner would accept
#: either. Linked rather than embedded: see :meth:`Plugin._pairing_page` for why
#: no QR is generated locally.
QR_VIEWER_URL = "https://project-chip.github.io/connectedhomeip/qrcode.html"


def _escape(text: Any) -> str:
    """Minimal HTML escaping for the pairing page.

    Hand-rolled rather than ``html.escape`` only in that it also handles a
    ``None`` — every value on that page comes from the bridge node or from an
    exception string, and one of them being absent must not render the word
    "None" into a field a user is about to type into their phone.
    """
    if text is None:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _pairing_html(pairing, message: str) -> str:
    """The pairing page (PRD §6). Self-contained: no scripts, no assets.

    ``pairing`` may be ``None`` when there is nothing to report — the page still
    renders, carrying ``message``, because a blank page over a bridge that is
    merely not running is indistinguishable from a broken handler.
    """
    manual = _escape(getattr(pairing, "manual_pairing_code", None))
    qr_payload = _escape(getattr(pairing, "qr_pairing_code", None))
    expires = _escape(getattr(pairing, "window_expires_at", None))
    fabrics = list(getattr(pairing, "fabrics", ()) or [])
    paired = ", ".join(_escape(export_bridge.describe_fabric(f)) for f in fabrics) or "none yet"
    banner = f'<p class="msg">{_escape(message)}</p>' if message else ""
    codes = ""
    if manual:
        # The payload is URL-encoded into the viewer link because an `MT:` string
        # is base-38 and can legitimately contain characters that would otherwise
        # end the query (`+`, `/`, `%`), producing a link that opens the tool with
        # a silently truncated payload — a QR that scans and means the wrong thing.
        viewer = f"{QR_VIEWER_URL}?data={quote(str(getattr(pairing, 'qr_pairing_code', '') or ''), safe='')}"
        codes = f"""
    <p class="warn"><strong>This page shows a live commissioning passcode.</strong>
       Anyone who can reach this URL can add the bridge — and every Indigo device you
       export — to <em>their</em> Apple Home, Alexa or Google account, for as long as the
       window is open. The Indigo Web Server only asks for a password if you have turned
       authentication on, so if you have not, treat this URL as the code itself: do not
       put it in a chat or an email, and close the window when you are done (it also
       expires on its own).</p>
    <h2>Manual pairing code</h2>
    <p class="code">{manual}</p>
    <h2>QR payload</h2>
    <p class="payload">{qr_payload}</p>
    <p><a href="{_escape(viewer)}" rel="noreferrer noopener" target="_blank">
       Render this payload as a scannable QR code</a> (opens the Matter project's own
       viewer — it needs internet access, and the payload is sent to it).</p>
    {f'<p class="expiry">This code stops working at {expires}.</p>' if expires else ''}
    <h2>What to expect</h2>
    <p>Add the bridge in your ecosystem's app as you would any Matter accessory. Every
       ecosystem will warn that it is an <strong>uncertified accessory</strong> — that is
       normal for a bridge like this one, and the same warning Homebridge and Home Assistant
       produce. Choose "Add Anyway".</p>"""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Indigo Matter bridge — pairing</title>
<style>
 body {{ font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        margin: 0 auto; max-width: 34rem; padding: 1.5rem; color: #222; }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1rem; margin-bottom: .2rem; color: #555; }}
 .code {{ font: 700 2.1rem/1.2 ui-monospace, Menlo, monospace; letter-spacing: .08em;
          margin: .2rem 0 1rem; word-break: break-all; }}
 .payload {{ font: .85rem ui-monospace, Menlo, monospace; word-break: break-all;
             background: #f4f4f6; padding: .6rem; border-radius: .4rem; }}
 .msg {{ background: #fff6d6; border: 1px solid #e8d48a; padding: .7rem; border-radius: .4rem; }}
 .warn {{ background: #fdeaea; border: 1px solid #d99; padding: .7rem; border-radius: .4rem;
          font-size: .92rem; }}
 .expiry {{ color: #a33; }}
 footer {{ margin-top: 2rem; font-size: .85rem; color: #777; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #16171a; color: #e6e6e6; }} h2 {{ color: #aaa; }}
   .payload {{ background: #26272b; }} .msg {{ background: #3a3320; border-color: #6b5c2e; }}
   .warn {{ background: #3a2222; border-color: #7a4444; }}
 }}
</style></head><body>
<h1>Indigo Matter bridge</h1>
{banner}{codes}
<footer>Paired ecosystems: {paired}.<br>
This page is served by the Indigo Web Server from the Matter plugin.</footer>
</body></html>"""


class Plugin(indigo.PluginBase):
    """Matter plugin entry point."""

    def __init__(self, plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs):
        super().__init__(plugin_id, plugin_display_name, plugin_version, plugin_prefs, **kwargs)
        self.debug = bool(plugin_prefs.get("verboseLogging", False))
        self._version = plugin_version
        self._start_ts = time.monotonic()

        self.runtime: AsyncRuntime | None = None
        self.proto = Protocol()
        self.registry = HandlerRegistry()
        self.device_sync = DeviceSync(self.registry, self.logger)
        self.matter: MatterClient | None = None
        self.jobs: CommissionJobs | None = None
        self.http: HttpApi | None = None
        self.server_process: ServerProcess | None = None
        # The EXPORT bridge node's LaunchAgent (E7). Built lazily and only ever
        # by a path that means to run it: a fresh install must be inert (XAC1),
        # and constructing this in startup would be one `ensure_installed` away
        # from a plist for a bridge nobody has asked for.
        self.bridge_process = None
        # The export allow-list (PRD §5.1). Built in startup, before anything
        # can consult it; None means "the plugin has not started yet", which
        # every export callback checks rather than assuming.
        self.exports: ExportStore | None = None
        # The outbound export engine (PRD-indigo-matter-export §5.4). Built in
        # startup; it owns the bridge client and starts one only when something
        # is actually exported (XG5).
        self.export_bridge: ExportBridge | None = None
        #: The allow-listed device ids, cached as a plain frozenset attribute.
        #: ``deviceUpdated`` fires for EVERY device on the server, so its guard
        #: has to be one attribute load and one hash lookup — no lock, no
        #: rebuild, no allocation. Refreshed only by :meth:`_exports_changed`.
        self._exported_ids: frozenset[int] = frozenset()
        #: Whether ``indigo.devices.subscribeToChanges()`` has been issued.
        self._subscribed_to_devices = False
        #: Whether a ``deviceUpdated`` has arrived since the subscription was
        #: issued — the only observable evidence that it actually took.
        self._device_updates_seen = False
        #: How many watchdog ticks have passed with a subscription and no
        #: ``deviceUpdated``, and how many times we have re-issued it.
        self._no_update_ticks = 0
        self._resubscribe_attempts = 0
        #: Set once the watchdog has said out loud that it is giving up, so the
        #: one line naming the consequence is said once and not every 15s.
        self._resubscribe_gave_up = False
        #: Device ids whose export callback is currently failing. This callback
        #: fires on every change of an exported device, so a stuck failure would
        #: otherwise write one traceback per dimmer-ramp step; cleared on the
        #: first success so a second, later outage is still heard.
        self._export_callback_failed: set[int] = set()
        self._install_thread: threading.Thread | None = None
        self._stopping = False
        # When WE restart matter-server (menu / post-install), the client sees a brief
        # outage — suppress the "appears to be crashing" diagnostic until this deadline
        # (cleared early on a successful reconnect). _restart_notice_shown dedups the
        # "restarting…" info line to once per window.
        self._restart_expected_until = 0.0
        self._restart_notice_shown = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _server_prefs(self) -> dict:
        """Current prefs with local-mode pinning applied — build every ServerProcess here.

        Local mode is turnkey: the plugin runs matter-server on loopback and dials it
        there, so a stale or blank host/port can never be used. That pinning lands in a
        COPY (only ``serverLocation``/``manageLaunchAgent`` are persisted), so anything
        constructing a ServerProcess from raw ``pluginPrefs`` skips it and can write a
        different plist than startup does — which, because ``_apply_plist`` reloads on a
        digest change, would flip-flop the server on every reload. One helper keeps every
        call site honest.
        """
        prefs = dict(self.pluginPrefs)
        location = server_location(prefs)
        if location == "local":
            prefs["matterServerHost"] = "localhost"
            prefs["matterServerPort"] = "5580"
            prefs["matterServerPath"] = "/ws"
            prefs["matterServerListenAddress"] = "127.0.0.1"
        prefs["serverLocation"] = location
        return prefs

    def startup(self) -> None:
        self.debug = bool(self.pluginPrefs.get("verboseLogging", False))
        # One user-facing choice — is matter-server on this Mac? — drives both
        # the connection target and whether the plugin runs the server.
        prefs = self._server_prefs()
        location = prefs["serverLocation"]
        managed = location == "local"
        # Persist the resolved choice so the config UI and later reads agree
        # (also migrates pre-2026.6 prefs that had no serverLocation key).
        self.pluginPrefs["serverLocation"] = location
        self.pluginPrefs["manageLaunchAgent"] = managed

        # Load the export allow-list first: it is pure prefs I/O, and E3's
        # bridge wiring will need it already populated when it starts. An
        # unreadable list must not stop the (inbound) plugin starting, so
        # ExportStore degrades to empty and preserves the blob rather than
        # raising — see export_store._corrupt.
        # prefs are read through a getter, not captured: Indigo can rebind
        # self.pluginPrefs when the user saves the PluginConfig dialog, and a
        # store holding the old mapping would write to an orphan. savePluginPrefs
        # is the flush the store commits through before it trusts a write.
        self.exports = ExportStore(
            lambda: self.pluginPrefs, self.logger,
            save_prefs=self._save_plugin_prefs,
            entry_validator=self._reject_unexportable_entry,
        )
        if len(self.exports):
            self.logger.info("Matter export allow-list: %d device(s) exported", len(self.exports))
        self._reconcile_exports()

        self.runtime = AsyncRuntime(self.logger)
        self.runtime.start()

        if managed:
            try:
                self.server_process = ServerProcess(prefs, self.logger)
                self.server_process.ensure_installed()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception(exc)

        # Only in managed/local mode can we read the server's own error log; in remote
        # mode there is no local log to surface.
        on_repeated_failure = self._on_server_unreachable if self.server_process else None
        self.matter = MatterClient(
            self.proto, self.logger, prefs,
            on_event=self._on_matter_event,
            on_connect=self._resync,
            on_disconnect=self._on_disconnected,
            on_repeated_failure=on_repeated_failure,
            on_late_response=self._on_late_matter_response,
        )
        self.jobs = CommissionJobs(
            self.matter, self.device_sync.create_from_raw, self.logger,
            schedule=self.runtime.submit,
            knows_node=self.device_sync.knows_node,
        )
        self.http = HttpApi(
            self.jobs, self.logger,
            status_provider=self._status_body,
            decommission_provider=self._decommission_sync,
            diagnostics_provider=self._diagnostics_sync,
        )

        # Export (outbound) — built unconditionally, started only if the
        # allow-list is non-empty. Both decisions live in _exports_changed.
        self.export_bridge = ExportBridge(
            self.exports, self.runtime, self.logger, lambda: self.pluginPrefs,
            plugin_version=self._version, plugin_id=self._export_plugin_id(),
            # The un-export debt (XAC7) is written to prefs and has to reach
            # disk to be worth writing: the failure it covers is the plugin
            # never getting another chance to say it.
            save_prefs=self._save_plugin_prefs,
            # E7's LaunchAgent seams. Passed unconditionally — the bridge is
            # gated by the ALLOW-LIST, not by startup, so handing them over here
            # installs nothing (XAC1). ExportBridge calls them on the
            # empty↔non-empty transitions and nowhere else.
            agent_start=self._start_bridge_agent,
            agent_stop=self._stop_bridge_agent,
            agent_diagnose=self._bridge_agent_diagnosis,
        )
        self._exports_changed()

        run_future = self.runtime.submit(self.matter.run())
        # if the run-loop coroutine ever dies, surface it rather than parking the
        # exception on an unretrieved future.
        run_future.add_done_callback(self._log_run_future)
        # reconcile is driven by on_connect (_resync), so it runs on first connect
        # and again on every reconnect — no separate initial-sync task needed.
        self.logger.info("%s started", PLUGIN_NAME)

    def _log_run_future(self, fut) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            self.logger.exception(exc)

    # ------------------------------------------------------------------
    # Export wiring (PRD-indigo-matter-export §5.4)
    # ------------------------------------------------------------------
    def _exports_changed(self) -> None:
        """THE seam for "the allow-list changed" — call it after every write.

        Refreshes the hot-path id set, subscribes to device changes if this is
        the first export, and lets the bridge start or stop itself (XG5).
        """
        self._exported_ids = self.exports.ids() if self.exports is not None else frozenset()
        if self._exported_ids:
            self._subscribe_to_device_changes()
        if self.export_bridge is not None:
            self.export_bridge.exports_changed()

    def _subscribe_to_device_changes(self) -> None:
        """Ask the server for every device change — once, and only if we need it.

        Three findings settle the shape of this, all from the Indigo docs rather
        than from what was convenient:

        * ``indigo.devices.subscribeToChanges()`` subscribes to **every device
          on the server**, not ours, and the IOM reference is explicit that it
          "causes a significant amount of traffic between IndigoServer and your
          plugin". The default posture of this plugin is an empty allow-list
          (XG5), so subscribing unconditionally would tax every existing user
          who never exports anything — for callbacks that would return on their
          first line every single time.
        * It is a plain request to the server, not a startup-only registration.
          Issuing it from a menu callback the first time a user exports a device
          works exactly as it does from ``startup``, which is what makes the
          conditional subscription safe: the first export in a session turns it
          on, and every later ``deviceUpdated`` arrives.
        * There is **no unsubscribe** in the canonical scripting reference (only
          ``subscribeToChanges``), so this is a one-way door. We therefore never
          try to turn it off when the allow-list empties again; the hot-path
          guard below already makes a stale subscription free, and a
          "clever" unsubscribe against an undocumented API is exactly the sort
          of thing that fails silently on an Indigo upgrade.

        Note there is deliberately **no ``pluginId`` self-loop guard** here (the
        usual companion to this subscription). It would be dead code: our own
        devices are excluded by the catalog's loop guard, so they can never
        reach the allow-list, and the id-set check below already refuses them.
        The dispatch→state→push path does not loop either — the node
        echo-guards its own writes (§6.4) and a push produces no Indigo change.
        """
        if self._subscribed_to_devices:
            return
        self._issue_device_subscription()

    def _issue_device_subscription(self) -> bool:
        """The bare call, without the once-only guard. True if it did not raise."""
        try:
            indigo.devices.subscribeToChanges()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Matter bridge: could not subscribe to Indigo device changes — "
                              "exported accessories will not follow Indigo state. %s", exc)
            self.logger.exception(exc)
            return False
        self._subscribed_to_devices = True
        self.logger.debug("subscribed to Indigo device changes (export is active)")
        return True

    def _resubscribe_tick(self) -> None:
        """Re-issue ``subscribeToChanges`` if it looks like it never took.

        **Why this exists.** The whole outbound push path rests on one
        undocumented assumption: that ``subscribeToChanges`` works the same
        issued from a menu callback as from ``startup``. The canonical reference
        describes it as a request to the server, which is why it is safe to make
        conditional (see :meth:`_subscribe_to_device_changes`) — but it does not
        *say* so, there is no acknowledgement to check, and no unsubscribe to
        compare against. If the assumption is ever wrong on some Indigo build,
        the symptom is silence: exported accessories simply stop following
        Indigo, with no error anywhere.

        **Giving up is itself news.** Exhausting the attempts used to be a bare
        ``return``, at no log level at all — so the one feature whose entire
        purpose is to break a silence ended by going silent, in exactly the
        house where it had failed to help. It now says so once, naming the
        consequence and the remedy rather than the counter.

        **``_device_updates_seen`` deliberately never re-arms**, and that is a
        known limit rather than an oversight: a subscription that dies *later*
        in a session is not noticed here, because the flag only records that an
        update was seen at some point. Re-arming it on a window would make every
        genuinely quiet house — nobody home, nothing switching for a few minutes
        — re-issue the subscription and eventually warn that export is broken
        when it is working perfectly. The failure this watchdog exists for is a
        subscription that never registered *at all*, which is the one it can
        tell apart from a quiet house, and it stays scoped to that.
        """
        if not self._exported_ids or not self._subscribed_to_devices:
            return
        if self._device_updates_seen:
            return
        if self._resubscribe_attempts >= MAX_RESUBSCRIBE_ATTEMPTS:
            if not self._resubscribe_gave_up:
                self._resubscribe_gave_up = True
                self.logger.warning(
                    "Matter bridge: no Indigo device update has arrived since exporting, after "
                    "re-issuing subscribeToChanges %d times. Unless the house is simply idle, "
                    "exported accessories are NOT following Indigo state and nothing further will "
                    "retry on its own — reload the plugin.", MAX_RESUBSCRIBE_ATTEMPTS)
            return
        self._no_update_ticks += 1
        if self._no_update_ticks < RESUBSCRIBE_TICKS:
            return
        self._no_update_ticks = 0
        self._resubscribe_attempts += 1
        # **Bounded, because "no updates" is also what a quiet house looks
        # like.** A device that nobody touches genuinely produces no callback,
        # so this cannot be a permanent retry loop without being permanent
        # noise. A handful of re-issues covers the case it is for — a
        # subscription that never registered — and after that the evidence is
        # indistinguishable from nothing having happened.
        self.logger.debug(
            "Matter bridge: no device updates since subscribing; re-issuing "
            "subscribeToChanges (attempt %d of %d)",
            self._resubscribe_attempts, MAX_RESUBSCRIBE_ATTEMPTS)
        self._issue_device_subscription()

    def deviceUpdated(self, origDev, newDev):  # noqa: N802
        """Push an exported device's change outward.

        This runs for **every device on the server** (see
        :meth:`_subscribe_to_device_changes`), so the second statement is the
        whole performance story: a frozenset membership test on an int, against
        an attribute the plugin already holds. Nothing is classified, nothing is
        locked and nothing is allocated for a device nobody exported.
        """
        super().deviceUpdated(origDev, newDev)
        # Set for ANY device, before the allow-list gate: the flag is evidence
        # that the *subscription* is alive, and the subscription covers every
        # device on the server. Gating it on an exported device would leave the
        # watchdog re-issuing a perfectly good subscription in any house where
        # the exported devices happen to be idle.
        self._device_updates_seen = True
        if newDev.id not in self._exported_ids:
            return
        if self.export_bridge is None:
            return
        try:
            self.export_bridge.device_updated(origDev, newDev)
        except Exception as exc:  # noqa: BLE001 - never let export break Indigo's callback
            # Named and rate-limited: a bare traceback here says a device broke
            # but not which one, and this callback fires often enough that a
            # stuck device would bury the rest of the event log.
            if newDev.id not in self._export_callback_failed:
                self._export_callback_failed.add(newDev.id)
                self.logger.error(
                    "Matter bridge: the update of %s (id %s) could not be handed to the bridge "
                    "— %s. Its accessory will show stale state until this clears.",
                    getattr(newDev, "name", ""), newDev.id, exc)
                self.logger.exception(exc)
        else:
            self._export_callback_failed.discard(newDev.id)

    def deviceDeleted(self, dev):  # noqa: N802
        """A deleted device leaves the allow-list and the bridge (PRD §5.4)."""
        super().deviceDeleted(dev)
        if dev.id not in self._exported_ids or self.exports is None:
            return
        try:
            self.exports.remove(dev.id)
            self.logger.info("Removed Matter export for %s (id %s) — the Indigo device was deleted",
                             getattr(dev, "name", ""), dev.id)
        except Exception as exc:  # noqa: BLE001
            # The store rolled back, so the entry survives; the endpoint removal
            # below is still right (the device is gone either way) and the
            # startup sweep will report the orphan.
            self.logger.error("Matter bridge: removing the deleted device %s from the export "
                              "list FAILED — %s", dev.id, exc)
            self.logger.exception(exc)
        self._export_callback_failed.discard(dev.id)
        try:
            if self.export_bridge is not None:
                self.export_bridge.remove(dev.id)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
        finally:
            # In a finally because the endpoint removal above can raise (the
            # socket is the bridge's, not ours) and the id-set cache has no
            # other way back in sync: leaving a deleted device in it makes
            # deviceUpdated hand a ghost to the bridge on every later change,
            # and deviceDeleted will never fire for it again.
            try:
                self._exports_changed()
            except Exception as exc:  # noqa: BLE001
                self.logger.exception(exc)

    def shutdown(self) -> None:
        self.logger.debug("%s shutting down", PLUGIN_NAME)
        # Signal any in-flight background install to skip its post-npm plugin-state
        # mutation, then wait briefly for it so we don't rewrite the LaunchAgent or
        # prefs against a tearing-down plugin.
        self._stopping = True
        if self._install_thread is not None and self._install_thread.is_alive():
            self._install_thread.join(timeout=5)
        if self.runtime is not None and self.runtime.is_running and self.matter is not None:
            try:
                self.runtime.submit(self.matter.close()).result(timeout=4)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("matter close error: %s", exc)
        # Same ordering rule as the controller client: close the socket while the
        # loop still exists to close it on. The bridge *agent* is deliberately
        # left running (PRD §5.4 / PM-B) — a plugin reload must not un-pair
        # anyone's ecosystems.
        if self.runtime is not None and self.runtime.is_running and self.export_bridge is not None:
            self.export_bridge.stop()
        self.export_bridge = None
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

    async def _assert_fabric_label(self) -> None:
        """Name the Indigo fabric on devices (matter-server's default label is
        'HomeAssistant' — vendor apps would list us as Home Assistant).

        Sent only when the server's current label differs: set_default_fabric_label
        pushes UpdateFabricLabel to every connected node, so re-asserting an
        unchanged label on each reconnect would be pointless per-device writes.
        Cosmetic — failure (e.g. an older matter-server without the command) must
        never block reconcile.
        """
        desired = str(self.pluginPrefs.get("fabricLabel", "Indigo")).strip()[:32] or "Indigo"
        try:
            current = (self.matter.server_info or {}).get("fabric_label")
            if current == desired:
                return
            await self.matter.set_default_fabric_label(desired)
            self.logger.info('fabric label set to "%s" (was %s)', desired, current or "server default")
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("could not set fabric label (older matter-server?): %s", exc)

    async def _resync(self) -> None:
        """Reconcile matter nodes ↔ Indigo devices. Driven by the WS client's
        on_connect, so it runs on the first connect and again on every reconnect
        (recovers state + clears 'unreachable' after a drop / sleep-wake)."""
        # A successful (re)connect means any expected-restart window has served its
        # purpose — close it so a LATER crash isn't wrongly suppressed as "restarting".
        self._restart_expected_until = 0.0
        await self._assert_fabric_label()
        try:
            nodes = await self.matter.get_nodes() or []
            detailed = []
            for node in nodes:
                if isinstance(node, dict) and "endpoints" in node:
                    detailed.append(node)
                else:
                    node_id = node.get("node_id") if isinstance(node, dict) else node
                    detailed.append(await self.matter.get_node(node_id))
            self.device_sync.reconcile_all(detailed)
            self.logger.info("reconciled %d Matter node(s)", len(detailed))
        except Exception as exc:  # noqa: BLE001
            # _resync is the sole reconcile path (first connect + every reconnect);
            # keep the traceback so a first-connect failure (user sees no devices)
            # is debuggable. On failure nothing is cleared (reconcile_all never ran).
            self.logger.warning("resync incomplete: %s", exc, exc_info=True)

    def _on_disconnected(self) -> None:
        """matter-server connection dropped — devices are unreachable until reconnect."""
        self.device_sync.mark_all_unreachable()

    def _on_server_unreachable(self, attempts: int) -> None:
        """Managed mode: matter-server still unreachable after several attempts.

        The WS client only sees "connection refused"; the real reason is in the
        server's own stderr. Surface a tail of it (or a not-installed hint) into the
        Indigo log so users don't have to hunt for ~/Library/Logs/indigo-matter.
        Fired once per failure streak by the client, so this never spams.
        """
        sp = self.server_process
        if sp is None:
            return
        if time.time() < self._restart_expected_until:
            # We deliberately restarted it — this outage is expected, not a crash. But
            # DEFER, don't drop: re-arm the client so if the server never comes back the
            # real error still surfaces on a later cycle (past the window). _resync
            # clears the window on a successful reconnect, so this only persists on an
            # actually-failing restart.
            if not self._restart_notice_shown:
                self.logger.info("matter-server is restarting; reconnecting…")
                self._restart_notice_shown = True
            if self.matter is not None:
                self.matter.rearm_failure_diagnostic()
            return
        tail = sp.tail_error_log()
        if tail:
            hint = ""
            if "Storage is locked" in tail or "storage-lock" in tail:
                # A second matter-server (orphaned from an earlier LaunchAgent) holds the
                # storage lock. The plugin now reaps such strays on start/restart; point
                # the user at that in case a reap couldn't run (e.g. ps unavailable).
                hint = ("\nAnother matter-server appears to be holding the storage lock. "
                        "Use Plugins ▸ Matter ▸ Restart the Matter controller (it stops "
                        "stray servers), or reboot the Mac if it persists.")
            self.logger.error(
                "matter-server is not responding after %d attempts and appears to be "
                "crashing. Recent matter-server errors:\n%s%s", attempts, tail, hint,
            )
        else:
            self.logger.error(
                "matter-server is not responding after %d attempts and its error log is "
                "empty — it may not be installed (checked %s). Use Plugins ▸ Matter ▸ "
                "Install/update the Matter controller (matter-server), then restart the "
                "plugin.",
                attempts, sp.project_dir,
            )

    def _expect_restart(self) -> None:
        """Open the ~30s window during which a client outage is treated as an expected
        restart (see :meth:`_on_server_unreachable`), not a crash."""
        self._restart_expected_until = time.time() + 30
        self._restart_notice_shown = False

    def runConcurrentThread(self) -> None:
        """Watchdog only — no I/O. Surfaces connectivity; reconnect is owned by
        the WS client's own backoff loop."""
        try:
            while True:
                self._health_tick()
                self.sleep(15)
        except self.StopThread:
            pass

    def _health_tick(self) -> None:
        if self.runtime is not None and not self.runtime.is_running:
            self.logger.warning("async runtime is not running")
            return
        if self.matter is not None and not self.matter.connected:
            ticks = getattr(self, "_disconnect_ticks", 0) + 1
            self._disconnect_ticks = ticks
            # debug each ~15s tick, but warn once it's been down ~1 min so a
            # never-connecting matter-server is visible in the log, not buried.
            if ticks == 4:
                self.logger.warning("matter-server still not connected after ~1 min")
            else:
                self.logger.debug("matter-server not currently connected")
        else:
            self._disconnect_ticks = 0
        # The export side keeps its OWN counter (E1 audit note): the two clients
        # talk to different processes and fail independently, so a shared streak
        # counter would let a healthy bridge silence a dead matter-server, or
        # the reverse.
        if self.export_bridge is not None:
            self.export_bridge.health_tick()
        self._resubscribe_tick()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    def validatePrefsConfigUi(self, valuesDict):  # noqa: N802
        """Sanitise the remote host and require it when connecting off-Mac."""
        location = str(valuesDict.get("serverLocation", "local")).strip().lower()
        if location == "remote":
            host = sanitize_host(valuesDict.get("matterServerHost", ""))
            valuesDict["matterServerHost"] = host  # strip pasted scheme/port/path
            if not host:
                errors = indigo.Dict()
                errors["matterServerHost"] = (
                    "Enter the hostname or IP of the computer running matter-server."
                )
                return (False, valuesDict, errors)
        return (True, valuesDict)

    def getPrefsConfigUiValues(self):  # noqa: N802
        """Seed the plugin config dialog, computing the PRD §5.5 export readout.

        Indigo has no dynamic labels, so the readout is a **read-only textfield**
        written here — the same shape the export dialog's status line already
        uses. Every value comes from state the plugin already holds (the last
        attach or 15-second status poll, and the §5 fabric events); nothing here
        does I/O, because this runs while the dialog is opening.
        """
        values = indigo.Dict(self.pluginPrefs)
        values["exportReadout"] = self._export_readout()
        return (values, indigo.Dict())

    # Indigo 2025.2's PluginBase carries snake_case aliases for the ConfigUI
    # pre-population callbacks alongside the camelCase names, and which one it
    # dispatches on is not documented. Both are defined so the readout cannot be
    # silently empty on a build that prefers the other spelling.
    def get_prefs_config_ui_values(self):
        return self.getPrefsConfigUiValues()

    def _export_readout(self) -> str:
        """One line describing the Matter bridge for the config dialog (PRD §5.5).

        What the PRD asks for is *which* ecosystems hold a fabric and whether a
        window is open — not slot arithmetic. matter.js allows 254 fabrics, so
        the count is never the interesting number; the identity of the peers is,
        because "why has Alexa stopped working" is answered by seeing that Alexa
        is not in this list.
        """
        bridge = self.export_bridge
        if bridge is None:
            return "Plugin still starting."
        exported = len(self.exports) if self.exports is not None else 0
        if not bridge.enabled:
            return f"Export is switched off. {exported} device(s) would be exported."
        if not bridge.active:
            return (f"{exported} device(s) exported; the bridge node is not running."
                    if exported else "Nothing is exported yet, so the bridge node is not running.")
        # ⊗ `active` means "a client OBJECT exists" — it is set the moment the
        # engine builds one and stays set through every reconnect attempt. Read as
        # "the bridge node is running", it told a user whose node had been
        # crash-looping for a day "3 device(s) exported. Paired with: Apple",
        # which is the readout PRD §5.5 exists to make impossible. The client's
        # own socket state is the fact; `attached` narrows it further, because a
        # connected-but-refusing node (§1.1) serves no accessories at all.
        client = bridge.client
        if client is None or not getattr(client, "connected", False):
            return (f"{exported} device(s) exported, but the plugin is NOT connected to the bridge "
                    f"node — exported accessories are unavailable right now. See the Event Log.")
        fabrics = bridge.fabrics
        if fabrics is None:
            return f"{exported} device(s) exported; not yet connected to the bridge node."
        paired = ", ".join(export_bridge.describe_fabric(f) for f in fabrics) or "nothing yet"
        # Said as "last reported" because it is: this list is the one the last
        # attach or §5 event left behind, deliberately not a WS round trip (this
        # runs while the dialog is opening). An ecosystem that dropped us a
        # second ago is still in it.
        window = self._window_readout(bridge)
        serving = ("" if getattr(client, "attached", False) else
                   " The node is connected but not serving accessories — see the Event Log.")
        return (f"{exported} device(s) exported. Paired with (last reported): {paired}."
                f"{serving}{window}")

    @staticmethod
    def _window_readout(bridge) -> str:
        """The pairing-window sentence, or "" — and never a window that has passed.

        ⊗ ``window_expires_at`` is set by the pairing menu and cleared only by
        the §5 ``window_closed`` event, which the node does not send on shutdown.
        So a node that was restarted (or a plugin that was) left the timestamp
        standing and the readout claimed an open window indefinitely — including
        for a time hours in the past. Comparing it against now costs one parse
        and turns a permanent false claim into an expiry the user can read.
        """
        raw = getattr(bridge, "window_expires_at", None)
        if not raw:
            return ""
        try:
            # The node sends RFC 3339 with a `Z`; `fromisoformat` learned `Z` in
            # 3.11 but the substitution costs nothing and works on 3.10 too.
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            # Unparseable: report it as-is rather than dropping it. A timestamp
            # we cannot compare is still the only thing we know.
            return f" A pairing window was opened, expiring {raw}."
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= datetime.now(timezone.utc):
            return (f" The last pairing window expired at {raw} — open a new one with "
                    "Plugins ▸ Matter ▸ Pair Matter Bridge….")
        return f" A pairing window is open until {raw}."

    def closedPrefsConfigUi(self, valuesDict, userCancelled):  # noqa: N802
        if userCancelled:
            return
        self.debug = bool(valuesDict.get("verboseLogging", False))
        # The connection + managed server are wired once in startup from a prefs
        # snapshot, so a changed location/host only takes effect on reload.
        self.logger.info(
            "matter-server settings saved — reload the plugin (or Plugins ▸ Matter ▸ "
            "Restart the Matter controller) to apply them"
        )
        # Export is the exception: its switch and its ports are read on every
        # connect, and the ONE change a user expects to act immediately is
        # ticking or unticking "Enable Matter export". Re-running the transition
        # applies it without a reload — and, because the transition is the same
        # code the allow-list uses, it also brings the agent up or down.
        #
        # This call is the ONLY thing that makes that switch act without a plugin
        # reload, which is what PluginConfig.xml promises the user in prose. It
        # is three lines with no local symptom if they go, so it has its own test
        # (⊗ `test_saving_config_applies_the_export_switch_immediately`).
        if self.export_bridge is None:
            self.logger.debug(
                "Matter bridge: the export engine is not running, so the export switch will take "
                "effect when the plugin next starts.")
            return
        try:
            self.export_bridge.exports_changed()
        except Exception as exc:  # noqa: BLE001
            # A bare traceback here reads as a crash in "save settings". Say what
            # did not happen and what to do instead — the prefs ARE saved.
            self.logger.error(
                "Matter bridge: your settings were saved, but applying the export switch "
                "immediately FAILED (%s). Reload the plugin to apply it.", exc)
            self.logger.exception(exc)

    # ------------------------------------------------------------------
    # Device lifecycle
    # ------------------------------------------------------------------
    def validateDeviceConfigUi(self, valuesDict, typeId, devId):  # noqa: N802
        """Reject changing a Matter device's Indigo type (issue #58).

        Indigo's Edit Device dialog always offers the plugin's full Type menu
        and a plugin cannot remove it — but Matter device types are derived
        from the node's clusters at creation. A manual change desyncs the
        device: the next reconcile creates a duplicate of the correct type,
        and the re-typed device becomes a zombie whose actions target clusters
        the node does not implement. ``createdTypeId`` is stamped into props at
        creation (and healed onto older devices at reconcile); absence of the
        stamp (e.g. a manually created device) allows the save.
        """
        created = ""
        try:
            created = indigo.devices[devId].pluginProps.get("createdTypeId", "")
        except KeyError:
            pass  # brand-new device — nothing to protect yet
        if created and typeId != created:
            errors = indigo.Dict()
            errors["showAlertText"] = (
                "Matter device types are derived from the device itself and "
                f"cannot be changed (this device was created as '{created}'). "
                "If the device is wrong, delete it and reload the plugin — it "
                "will be recreated from the node's clusters."
            )
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def deviceStartComm(self, dev):  # noqa: N802
        # Indigo builds a device's state list at creation and does NOT re-read
        # Devices.xml on plugin upgrade — without this refresh, states added in
        # a new release (e.g. colorCapabilities, issue #60) never exist on
        # fielded devices and every update logs an Indigo error.
        try:
            dev.stateListOrDisplayStateIdChanged()
        except Exception as exc:  # noqa: BLE001 - refresh failure must not block startComm
            self.logger.debug("state list refresh failed for %s: %s", dev.id, exc)
        # Backstop for type edits made while the plugin was not running (the
        # validateDeviceConfigUi guard can't fire then) — issue #58.
        created = dev.pluginProps.get("createdTypeId", "")
        if created and dev.deviceTypeId != created:
            self.logger.warning(
                "device %s (%s) was created as type %s but is now %s — Matter "
                "device types cannot be changed; delete the device and reload "
                "this plugin to recreate it correctly",
                dev.id, dev.name, created, dev.deviceTypeId,
            )
        self.device_sync.note_device(dev)
        self.device_sync.set_active(dev.id, True)

    def deviceStopComm(self, dev):  # noqa: N802
        self.device_sync.set_active(dev.id, False)

    # ------------------------------------------------------------------
    # Device actions → Matter commands (bridged onto the loop, 5s ack)
    # ------------------------------------------------------------------
    def actionControlDevice(self, action, dev):  # noqa: N802
        self._send_built_commands(self.device_sync.build_command(dev, action), dev)

    def actionControlThermostat(self, action, dev):  # noqa: N802
        self._send_built_commands(self.device_sync.build_command(dev, action), dev)

    def _send_built_commands(self, commands, dev) -> None:
        """Send a handler's command(s). Handlers may return one MatterAction or
        a list for composite operations (the colour W slider is CT mode + level
        — two Matter commands); each is sent and acked individually so a
        failure surfaces on the device exactly as a single command's would."""
        if commands is None:
            return
        if not isinstance(commands, list):
            commands = [commands]
        for command in commands:
            self._send_matter_command(command, dev)

    def actionControlSensor(self, action, dev):  # noqa: N802
        self.logger.info('ignored "%s" — Matter sensor is read-only', dev.name)

    def actionControlUniversal(self, action, dev):  # noqa: N802
        """Indigo's universal buttons: Request Status / Update / Reset / Beep.

        Without this method Indigo logs "plugin does not define method
        actionControlUniversal" whenever a user presses the energy Update/Reset
        (or status) buttons. Request Status and Energy Update re-interview the
        node (see _refresh_node) to re-read its attributes incl. power/energy.
        Energy Reset is surfaced as unsupported: Matter's accumulated energy is
        cumulative on the device and there is no Matter command to zero it (and
        silently bouncing the Indigo state to 0 would be undone by the device's
        next report). Beep / any other action are ignored."""
        universal = indigo.kUniversalAction
        cmd = action.deviceAction
        if cmd in (universal.RequestStatus, universal.EnergyUpdate):
            self._refresh_node(dev)
        elif cmd == universal.EnergyReset:
            self.logger.info(
                '"%s": Matter accumulated energy is cumulative on the device and '
                "cannot be reset from Indigo (no Matter command exists for it).", dev.name)
        else:
            self.logger.debug('ignored universal action %r for "%s"', cmd, dev.name)

    def getSensitivityLevels(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the Set Sensitivity Level action's picker.

        Reads SupportedSensitivityLevels (0x0080/0x0001), cached by device_sync
        at the device's last create/reconcile pass — get_node's snapshot isn't
        otherwise retained (issue #85). A CONFIRMED count of 3 gets the Aqara
        FP300's real labels (Low/Standard/High); any other confirmed count gets
        generic "Level N" options. Unknown (device not yet reconciled, or
        offline) never presumes the FP300 labelling — it degrades to a generic
        0..2 scale so the dialog is never empty but also never lies about what
        the device actually supports.
        """
        known = None
        try:
            dev = indigo.devices[targetId]
            node_id = dev.pluginProps.get("nodeId")
            endpoint_id = dev.pluginProps.get("endpointId")
            if node_id and endpoint_id not in (None, ""):
                known = self.device_sync.sensitivity_levels_supported(int(node_id), int(endpoint_id))
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to the generic fallback
            self.logger.debug("getSensitivityLevels: could not resolve device %r: %s", targetId, exc)
        if known and known > 0:
            labels = ["Low (0)", "Standard (1)", "High (2)"] if known == 3 else [f"Level {i}" for i in range(known)]
        else:
            labels = [f"Level {i}" for i in range(3)]  # unconfirmed — generic 3-level fallback
        return [(str(i), label) for i, label in enumerate(labels)]

    def actionSetSensitivityLevel(self, action, dev):  # noqa: N802
        """Custom device action (issue #85): write BooleanStateConfiguration's
        writable CurrentSensitivityLevel (0x0080/0x0000) — the Aqara FP300's
        motion sensitivity (co-located with OccupancySensing), or a contact
        sensor's equivalent per the Matter spec's own BooleanState pairing."""
        try:
            level = int(action.props.get("level", ""))
        except (TypeError, ValueError):
            self.logger.error('"%s": invalid sensitivity level %r', dev.name, action.props.get("level"))
            return
        if "sensitivityLevel" not in dev.states:
            # Defense-in-depth: the two Actions.xml entries are already scoped
            # to motion/contact types, but a stale saved action (or a future
            # filter change) could still hand us a device without the state.
            self.logger.error('"%s": device does not support sensitivity (no Boolean State '
                              'Configuration cluster)', dev.name)
            return
        node_id = dev.pluginProps.get("nodeId")
        endpoint_id = dev.pluginProps.get("endpointId")
        if not node_id or endpoint_id in (None, ""):
            self.logger.error('"%s": cannot set sensitivity — device has no Matter node/endpoint yet', dev.name)
            return
        supported = self.device_sync.sensitivity_levels_supported(int(node_id), int(endpoint_id))
        if supported and not 0 <= level < supported:
            self.logger.error(
                '"%s": sensitivity level %d out of range (device supports 0-%d)',
                dev.name, level, supported - 1,
            )
            return
        write = MatterWrite(int(node_id), int(endpoint_id), CLUSTER_BOOLEAN_STATE_CONFIG,
                             ATTR_CURRENT_SENSITIVITY, level)
        if self._send_matter_command(write, dev):
            # Optimistic echo (precedent: color_control's whiteLevel echo) — the
            # firehose attribute_updated report will confirm/correct this once
            # matter-server processes the write.
            try:
                dev.updateStateOnServer("sensitivityLevel", level)
            except Exception as exc:  # noqa: BLE001 - cosmetic echo only, must not fail the action
                self.logger.debug('optimistic sensitivityLevel echo failed for "%s": %s', dev.name, exc)

    def _refresh_node(self, dev) -> None:
        """Re-interview the device's Matter node so matter-server re-reads its
        attributes; matter-server then emits a node_updated event which
        _refresh_live_node turns into refreshed Indigo states (incl. power/
        energy via the electrical handlers).

        NOTE: the interview ⇒ node_updated emission is matter-server behaviour,
        not guaranteed here — if it stops firing, a refresh becomes a no-op.
        Mirrors _send_matter_command: visible error state on timeout/failure,
        cleared on success, and a debug line on every silent skip so a button
        that does nothing still leaves a trail."""
        if self.runtime is None or self.matter is None:
            self.logger.debug('refresh skipped for "%s" — plugin not fully started', dev.name)
            return
        node_id = dev.pluginProps.get("nodeId")
        if not node_id:
            self.logger.debug('refresh skipped for "%s" — no nodeId (device not yet reconciled)', dev.name)
            return
        try:
            self.runtime.submit(self.matter.interview_node(int(node_id))).result(timeout=COMMAND_TIMEOUT)
            self.logger.info('refreshed "%s" (node %s)', dev.name, node_id)
            if getattr(dev, "errorState", ""):
                dev.setErrorStateOnServer("")  # refresh succeeded — clear a stale error
        except FuturesTimeoutError:
            self.logger.error('refresh of "%s" timed out', dev.name)
            dev.setErrorStateOnServer("timeout")
        except Exception as exc:  # noqa: BLE001
            self.logger.error('refresh of "%s" failed: %s', dev.name, exc)
            dev.setErrorStateOnServer("cmd failed")

    def _send_matter_command(self, action, dev) -> bool:
        if self.runtime is None or self.matter is None:
            return False
        # An action is either a cluster-command invoke or an attribute write.
        coro = self.matter.write(action) if isinstance(action, MatterWrite) else self.matter.send_command(action)
        try:
            self.runtime.submit(coro).result(timeout=COMMAND_TIMEOUT)
            if getattr(dev, "errorState", ""):
                dev.setErrorStateOnServer("")  # command succeeded — clear a stale error
            return True
        except FuturesTimeoutError:
            self.logger.error("Matter command to %s timed out", dev.name)
            dev.setErrorStateOnServer("timeout")
            return False
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Matter command to %s failed: %s", dev.name, exc)
            dev.setErrorStateOnServer("cmd failed")
            return False

    # ------------------------------------------------------------------
    # asyncio → Indigo event bridge (called on the loop thread)
    # ------------------------------------------------------------------
    def _on_matter_event(self, evt) -> None:
        # Stage isolation: a device_sync failure must not starve the job
        # reconcile below — reconcile_node_added can still recover the job
        # (it runs its own device-create pass), so it always gets its shot.
        try:
            self.device_sync.handle_event(evt)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
        # A node that arrives AFTER its commission job timed out is matter-server
        # finishing the join in the background (issue #16): device_sync has just
        # created the devices (bare product name); now let the job table claim it,
        # apply suggestedName/suggestedRoom, and flip the job back to success so a
        # still-polling client gets the real outcome.
        if self.jobs is not None and evt.kind == protocol.EVT_NODE_ADDED:
            data = evt.raw.get("data") if evt.raw else None
            if isinstance(data, dict):
                self.jobs.reconcile_node_added(data)

    def _on_late_matter_response(self, late) -> None:
        # self.jobs is None only in the startup gap between MatterClient's
        # construction (which wires this hook) and CommissionJobs' own — that
        # gap never reaches the loop today (matter.run() is not scheduled
        # until both exist), but this guard costs nothing and keeps the
        # invariant honest rather than assumed, same as _on_matter_event above.
        if self.jobs is not None:
            self.jobs.note_late_response(late)

    # ------------------------------------------------------------------
    # HTTP API (IWS hidden-action handlers — API.md v1.1)
    # ------------------------------------------------------------------
    def http_status(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        status, body = self.http.status()
        return self._reply(status, body)

    def http_commission(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        method, path_args, query = self._parse_request(action)
        status, body = self.http.commission(method, path_args, query)
        return self._reply(status, body)

    def http_decommission(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        method, path_args, query = self._parse_request(action)
        status, body = self.http.decommission(method, path_args, query)
        return self._reply(status, body)

    def http_diagnostics(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802
        _method, path_args, query = self._parse_request(action)
        status, body = self.http.diagnostics(path_args, query)
        return self._reply(status, body)

    @staticmethod
    def _parse_request(action):
        props = dict(action.props)
        method = props.get("incoming_request_method", "GET")
        path_args = list(props.get("file_path", []) or [])
        query = dict(props.get("url_query_args", {}) or {})
        body_params = props.get("body_params")
        if body_params:
            query = {**query, **dict(body_params)}
        return method, path_args, query

    @staticmethod
    def _reply(status, body):
        reply = indigo.Dict()
        reply["status"] = status
        reply["headers"] = indigo.Dict({"Content-Type": "application/json"})
        reply["content"] = json.dumps(body)
        return reply

    # ----- providers used by HttpApi (bridge into the loop where needed) -----
    def _status_body(self) -> dict:
        # server_info fields per ws-controller v0.6.2: sdk_version, fabric_id,
        # bluetooth_enabled (there is no plain "version" key).
        connected = bool(self.matter is not None and self.matter.connected)
        info = (self.matter.server_info if self.matter else None) or {}
        body = {
            "ready": connected,
            "controllerVersion": self._version,
            "matterServerReachable": connected,
            "matterServerVersion": str(info.get("sdk_version", "unknown")),
            "fabricId": str(info.get("fabric_id", "")),
            "nodeCount": self.device_sync.node_count(),
            "bleAvailable": bool(info.get("bluetooth_enabled", False)),
            "uptime": int(time.monotonic() - self._start_ts),
        }
        if not connected:
            # API.md §3.1: the 503 body carries the standard error envelope so
            # Domio can surface an actionable message, not a generic failure.
            uri = getattr(self.matter, "uri", None) if self.matter else None
            body["error"] = "matter_server_unreachable"
            body["message"] = f"Cannot reach matter-server at {uri}" if uri else "Cannot reach matter-server"
        return body

    def _decommission_sync(self, node_id):
        # None → genuine unknown node (404). MatterUnavailable → 503. Other → 500.
        if self.runtime is None:
            raise MatterUnavailable("plugin not ready")
        if self.matter is None:
            # Without this guard the AttributeError inside _decommission would be
            # misread as "device offline" and the Indigo devices deleted anyway.
            raise MatterUnavailable("matter-server not connected")
        try:
            return self.runtime.submit(self._decommission(node_id)).result(timeout=DECOMMISSION_TIMEOUT)
        except FuturesTimeoutError as exc:
            self.logger.error("decommission %s timed out", node_id)
            raise MatterUnavailable("matter-server timed out") from exc
        except MatterUnavailable:
            raise
        except RuntimeError as exc:  # asyncio runtime not running
            raise MatterUnavailable(str(exc)) from exc

    async def _decommission(self, node_id):
        # Captured BEFORE the delete: it is the only way to tell "node we have
        # never heard of" (a genuine 404) from "node we know, whose removal
        # failed". Conflating them told the user "Unknown node" about a node that
        # was still commissioned, and — for a node with no Indigo devices, where
        # removed_ids is always empty — dropped it from the picker so the
        # decommission could not be retried (issue #111 review).
        known = self.device_sync.knows_node(node_id)
        fabric_removed = True
        try:
            await self.matter.remove_node(node_id)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("remove_node failed (device may be offline): %s", exc)
            fabric_removed = False
        # Only forget the node if it actually left the fabric; otherwise it must
        # stay listed so the user can retry.
        removed_ids = self.device_sync.delete_node(node_id, forget=fabric_removed)
        if not removed_ids and not fabric_removed and not known:
            return None  # genuinely unknown and unreachable → 404
        return {
            "nodeId": node_id_to_str(node_id),
            "removedIndigoDeviceIds": removed_ids,
            "fabricRemoved": fabric_removed,
        }

    def _diagnostics_sync(self, node_id):
        if self.runtime is None:
            raise MatterUnavailable("plugin not ready")
        try:
            return self.runtime.submit(self._diagnostics(node_id)).result(timeout=DECOMMISSION_TIMEOUT)
        except FuturesTimeoutError as exc:
            self.logger.error("diagnostics %s timed out", node_id)
            raise MatterUnavailable("matter-server timed out") from exc
        except MatterUnavailable:
            raise
        except RuntimeError as exc:
            raise MatterUnavailable(str(exc)) from exc

    async def _diagnostics(self, node_id):
        from matter_model import parse_node
        try:
            raw = await self.matter.get_node(node_id)
        except ConnectionError as exc:
            raise MatterUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - protocol/timeout error reading the node
            self.logger.warning("diagnostics get_node(%s) failed: %s", node_id, exc)
            raise MatterUnavailable(str(exc)) from exc
        if not raw:
            return None  # genuine unknown node → 404
        node = parse_node(raw)
        return {
            "nodeId": node_id_to_str(node_id),
            "reachable": True,
            "vendorId": node.vendor_id,
            "productId": node.product_id,
            "vendorName": node.vendor_name,
            "productName": node.product_name,
            "softwareVersion": node.sw_version,
            "endpoints": [
                {
                    "endpointId": ep.endpoint_id,
                    "clusters": sorted(ep.cluster_ids),
                    "indigoDeviceId": self.device_sync.lookup(node.node_id, ep.endpoint_id),
                }
                for ep in node.endpoints
            ],
        }

    # ------------------------------------------------------------------
    # Menu items
    # ------------------------------------------------------------------
    def menuInstallMatterServer(self):  # noqa: N802
        """Install/update the matter-server npm package, then pin the node used.

        Only meaningful in local (managed) mode — self-managers keep their own
        server untouched. Runs off the Indigo main thread so the UI never blocks on
        npm; progress and outcome go to the log.
        """
        if server_location(self.pluginPrefs) != "local":
            self.logger.error(
                "Install is only for local mode. Set 'is matter-server on this Mac?' "
                "to local (managed) first, or install/manage the server yourself."
            )
            return
        if self._install_thread is not None and self._install_thread.is_alive():
            self.logger.warning("matter-server install already in progress.")
            return
        self.logger.info("Starting matter-server install in the background — watch the "
                         "log for progress; this can take a minute.")
        self._install_thread = threading.Thread(
            target=self._install_matter_server, name="matter-install", daemon=True)
        self._install_thread.start()

    def _install_matter_server(self, clean: bool = False) -> None:
        try:
            sp = self.server_process or ServerProcess(self._server_prefs(), self.logger)
            if clean:
                # "Start fresh": delete node_modules and reinstall. Stops/reaps the server
                # first and leaves the storage (fabric/pairings) intact. Used to recover a
                # matter-server that won't start after an upgrade.
                self.logger.info("Removing the installed matter-server for a clean "
                                 "reinstall (your devices/pairings are kept)…")
                if not sp.remove_package():
                    # remove_package has already said what is still there. Do NOT
                    # install over it: a clean reinstall that quietly became a
                    # plain reinstall leaves the wedge the user came here for.
                    self.logger.error(
                        "Clean reinstall ABANDONED — the old package could not be removed, so "
                        "nothing was reinstalled over it. Nothing was changed.")
                    return
            if not sp.install():
                self.logger.error(
                    "Install/update of the Matter controller (matter-server) did not "
                    "complete — see the error above. The server was not (re)installed; "
                    "retry when resolved."
                )
                return
            if self._stopping:  # plugin is tearing down — don't mutate its state
                return
            # Pin the exact node used so the LaunchAgent runs the same one forever —
            # this is what keeps install-node == run-node and avoids ABI crash-loops.
            self.pluginPrefs["nodeBinDir"] = sp.resolved_bin_dir
            indigo.server.savePluginPrefs()
            self.server_process = ServerProcess(self._server_prefs(), self.logger)
            self.server_process.ensure_installed()
            # Restart matter-server onto the just-installed version — otherwise the
            # newly-installed package sits on disk while the OLD process keeps running
            # (a running LaunchAgent doesn't pick up new files). This is what makes the
            # menu action a one-click, no-CLI update.
            self._expect_restart()
            if not self.server_process.restart():
                # Don't claim success: the new version may not be running.
                self._restart_expected_until = 0.0  # let the crash diagnostic work
                self.logger.error(
                    "matter-server was installed and pinned to node at %s, but the "
                    "restart onto the new version FAILED — the old version may still be "
                    "running. Use Plugins ▸ Matter ▸ Restart the Matter controller, or "
                    "reload the plugin.", sp.resolved_bin_dir,
                )
                return
            self.logger.info(
                "matter-server installed, pinned to node at %s, and restarting onto the "
                "new version — it reconnects automatically.", sp.resolved_bin_dir,
            )
        except Exception as exc:  # noqa: BLE001
            # npm may have succeeded and only the pin/activate step failed — say so, so
            # the user doesn't reinstall in circles chasing a downstream problem.
            self.logger.exception(exc)
            self.logger.error(
                "matter-server install did not complete after the npm step — the "
                "package may be installed but the node was not pinned and the server "
                "was not (re)started. See the trace above, then retry Plugins ▸ Matter "
                "▸ Install/update the Matter controller (matter-server)."
            )

    def menuReinstallMatterServerClean(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Menu callback: delete matter-server and reinstall it fresh (keeps devices).

        The "blow it all away and start over" recovery when matter-server won't start
        after an upgrade (e.g. a wedged install or a stray process). Removes
        ~/indigo-matter/node_modules and reinstalls; the fabric/storage is left intact so
        commissioned devices survive. Runs in the background like the plain install.
        """
        errors = indigo.Dict()
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm the reinstall."
            return (False, valuesDict, errors)
        if server_location(self.pluginPrefs) != "local":
            errors["confirm"] = ("Reinstall is only for local (managed) mode. Set 'is "
                                 "matter-server on this Mac?' to local first.")
            return (False, valuesDict, errors)
        if self._install_thread is not None and self._install_thread.is_alive():
            errors["confirm"] = "An install is already in progress — wait for it to finish."
            return (False, valuesDict, errors)
        self.logger.info("Starting a clean matter-server reinstall in the background — "
                         "watch the log for progress; this can take a minute.")
        self._install_thread = threading.Thread(
            target=self._install_matter_server, kwargs={"clean": True},
            name="matter-reinstall", daemon=True)
        self._install_thread.start()
        return (True, valuesDict)

    def menuRestartMatterServer(self):  # noqa: N802
        if self.server_process is None:
            self.logger.warning("LaunchAgent management is off; start matter-server manually")
            return
        # Rebuild from CURRENT prefs first. ServerProcess snapshots prefs at construction
        # and restart() bootstraps the plist *as it is on disk*, which only
        # ensure_installed() regenerates — so without this, a setting changed since
        # startup (notably the attestation flag) is silently NOT applied and this menu
        # still logs success.
        self.server_process = ServerProcess(self._server_prefs(), self.logger)
        self._expect_restart()  # expected outage, not a crash
        try:
            # None = preflight failed (plist torn down, nothing to restart);
            # True = it already reloaded launchd, so a restart() here would stop and
            # start the server a SECOND time for nothing — two outages, every device's
            # session dropped twice; False = job left running, so we do the restart.
            reloaded = self.server_process.ensure_installed()
            restarted = True if reloaded else (
                False if reloaded is None else self.server_process.restart()
            )
        except Exception as exc:  # noqa: BLE001
            # Unguarded, this would escape with the expected-restart window still armed,
            # suppressing the crash diagnostic for 30s while the server is down.
            self._restart_expected_until = 0.0
            self.logger.exception(exc)
            return
        if restarted:
            self.logger.info("matter-server restart requested")
        elif reloaded is None:
            self._restart_expected_until = 0.0
            self.logger.error(
                "matter-server cannot be restarted — see the error above. Its LaunchAgent "
                "was removed to stop a crash-loop; fix the cause, then reload the plugin."
            )
        else:
            self._restart_expected_until = 0.0  # restart failed — don't suppress the diagnostic
            self.logger.error(
                "matter-server restart failed; check ~/Library/Logs/indigo-matter/matter-server.err.log"
            )

    def menuShowMatterServerLogs(self):  # noqa: N802
        self.logger.info("matter-server log: ~/Library/Logs/indigo-matter/matter-server.log")

    def menuCommissionDeviceManually(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        if self.jobs is None:
            # Surface WHY OK did nothing — a bare (False, valuesDict) leaves the
            # dialog open with no explanation at all.
            self.logger.warning("manual commission requested before the plugin finished starting")
            errors = indigo.Dict()
            errors["setupCode"] = "Plugin still starting — try again in a moment."
            return (False, valuesDict, errors)
        status, body = self.jobs.create_job({
            "setupCode": valuesDict.get("setupCode", ""),
            "suggestedName": valuesDict.get("suggestedName", "Matter Device"),
            # The picker's value is a folder id; map it back to the folder NAME and
            # pass it as suggestedRoom, which device_sync resolves to that folder
            # (the same path Domio's room uses). "0"/unknown → no folder (root).
            "suggestedRoom": self._folder_name_for(valuesDict.get("folder")),
        })
        self.logger.info("manual commission → %s %s", status, body)
        return (status in (202, 409), valuesDict)

    def getDeviceFolders(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the folder picker on the manual-commission menu.

        Options are (folderId, folderName) with a leading "0" → no folder (the
        device-list root). The id MUST be a non-empty string — Indigo rejects an
        empty list id with "UI dynamic list function returned illegal ID string",
        which silently drops the option — so the no-folder sentinel is "0" (folder
        id 0 == no folder), never "". menuCommissionDeviceManually maps the chosen
        id back to the folder NAME for suggestedRoom."""
        options = [("0", "(no folder)")]
        try:
            for folder in indigo.devices.folders:
                options.append((str(folder.id), folder.name))
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to no-folder only
            self.logger.exception(exc)
        return options

    def _folder_name_for(self, folder_id):
        """Resolve the folder picker's selected id (string) to the folder NAME.

        "0", empty, or an unknown/stale id → None (commission to the device-list
        root). Never raises — an unresolvable folder must not fail the commission."""
        if not folder_id or folder_id == "0":
            return None
        try:
            fid = int(folder_id)
            for folder in indigo.devices.folders:
                if folder.id == fid:
                    return folder.name
            # Parses fine but matches nothing — e.g. folder deleted between the
            # picker rendering and submit. Benign (device lands at root), but leave
            # a trail rather than silently dropping the selection.
            self.logger.debug("folder id %r not found, commissioning at root", folder_id)
        except Exception as exc:  # noqa: BLE001 - degrade to no folder, never fail the commission
            self.logger.warning("folder id %r not resolvable, commissioning without a folder: %s", folder_id, exc)
        return None

    def getMatterNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """List-callback populating the decommission picker (one entry per node)."""
        if self.device_sync is None:
            return []
        try:
            options = []
            for node_id, names in self.device_sync.list_nodes():
                label = ", ".join(names) if names else "(no Indigo devices)"
                options.append((str(node_id), f"{label} — node {node_id_to_str(node_id)}"))
            return options
        except Exception as exc:  # noqa: BLE001 - never break the dialog; degrade to an empty picker
            self.logger.exception(exc)
            return []

    def menuDecommissionDevice(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Menu callback: decommission the selected node.

        Returns ``(False, valuesDict, errors)`` to keep the dialog open with a
        field error, ``(True, valuesDict)`` only when the node was fully
        removed (fabric AND Indigo devices) — partial outcomes are dialog
        errors so they can't masquerade as success.
        """
        errors = indigo.Dict()
        selected = valuesDict.get("node", "")
        if not selected:
            errors["node"] = "Select a device to decommission."
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm removal from Indigo."
            return (False, valuesDict, errors)
        try:
            node_id = int(selected)
        except (TypeError, ValueError):
            errors["node"] = "Invalid selection."
            return (False, valuesDict, errors)
        try:
            result = self._decommission_sync(node_id)
        except MatterUnavailable as exc:
            self.logger.error("decommission %s failed — matter-server unavailable: %s",
                              node_id_to_str(node_id), exc)
            # A timeout does NOT cancel the in-flight coroutine — the removal may
            # still complete in the background, so don't claim nothing happened.
            errors["node"] = ("matter-server did not respond — see the log. The removal may "
                              "still complete in the background; check the device before retrying.")
            return (False, valuesDict, errors)
        except (Exception, FuturesCancelledError) as exc:  # CancelledError is BaseException on 3.10+
            self.logger.error("decommission %s failed: %s", node_id_to_str(node_id), exc)
            self.logger.exception(exc)
            errors["node"] = "Decommission failed — see the Indigo event log."
            return (False, valuesDict, errors)
        if result is None:
            errors["node"] = "Unknown node — nothing was removed."
            return (False, valuesDict, errors)
        if result["fabricRemoved"]:
            self.logger.info(
                "Decommissioned Matter node %s: fabric removed, Indigo device(s) deleted: %s",
                result["nodeId"], result["removedIndigoDeviceIds"] or "none",
            )
            return (True, valuesDict)
        # remove_node failed (usually: device offline) — matter-server most likely
        # still has the node (any remove_node failure is treated as not-removed),
        # so the next reconcile (plugin restart or matter-server reconnect) will
        # recreate the Indigo devices we just deleted. Surface that in the dialog —
        # never report success when the underlying op only half-happened.
        self.logger.warning(
            "Decommission of node %s incomplete: Indigo device(s) %s deleted but the "
            "fabric removal failed (device offline?). The node is still commissioned in "
            "matter-server and its devices will reappear at the next reconcile — retry "
            "once the device is reachable.",
            result["nodeId"], result["removedIndigoDeviceIds"] or "none",
        )
        errors["node"] = ("Device unreachable — removed from Indigo, but it is still commissioned "
                          "in matter-server and will reappear at the next reconcile (plugin restart "
                          "or reconnect). Retry once the device is powered and reachable.")
        return (False, valuesDict, errors)

    # ------------------------------------------------------------------
    # Matter export allow-list — the "Manage Matter Exports…" dialog
    # (PRD-indigo-matter-export §5.1 UI-D; roles per BRIDGE_PROTOCOL §4.2)
    # ------------------------------------------------------------------
    def _export_plugin_id(self) -> str:
        """This plugin's id, for the loop guard (XNG3/XAC6).

        Read from the running plugin rather than hardcoded, so the guard can
        never drift from the bundle it is protecting; the catalog constant is
        only the fallback for a plugin object built without one (tests).
        """
        return getattr(self, "pluginId", "") or export_catalog.DEFAULT_PLUGIN_ID

    @staticmethod
    def _truthy(value) -> bool:
        """Indigo checkboxes arrive as bools or as "true"/"false" strings."""
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)

    @staticmethod
    def _indigo_device(device_id):
        """``indigo.devices[device_id]`` or None — a stale id is never fatal."""
        try:
            return indigo.devices[int(device_id)]
        except Exception:  # pylint: disable=broad-except  # KeyError/ValueError/Indigo's own
            return None

    def _export_selection(self, values_dict) -> tuple[str, int]:
        """Decode the picker value into ``(kind, device_id)``.

        ``kind`` is ``"none"`` (nothing chosen, or one of the informational
        rows — the "select a device" seed, the truncation tail, the no-match
        note), ``"excluded"`` (an ``x-`` row the user may see but not pick), or
        ``"device"``.
        """
        raw = str((values_dict or {}).get("exportDevice", "") or "")
        if not raw or raw == NO_SELECTION_ID or raw in (TRUNCATED_OPTION[0], NO_MATCH_OPTION[0]):
            return ("none", 0)
        excluded = raw.startswith(EXCLUDED_OPTION_PREFIX)
        if excluded:
            raw = raw[len(EXCLUDED_OPTION_PREFIX):]
        try:
            device_id = int(raw)
        except (TypeError, ValueError):
            return ("none", 0)
        return ("excluded" if excluded else "device", device_id)

    def _save_plugin_prefs(self) -> None:
        """Flush pluginPrefs to Indigo's database (the store's commit step)."""
        indigo.server.savePluginPrefs()

    def _reject_unexportable_entry(self, entry) -> str | None:
        """Validator for entries restored from prefs — the loop guard, re-run.

        Load is the one write path the dialog's guards never see: a blob
        restored from a backup, or hand-edited in the ``.indiPref``, can name a
        device this plugin created. Only the loop guard is enforced here.
        Ordinary ineligibility is *reported* by the startup reconcile and left
        alone, because a device can be temporarily odd (a plugin still
        starting) and silently deleting the user's export would be worse than
        an accessory that fails to build.
        """
        dev = self._indigo_device(entry.indigo_device_id)
        if dev is None:
            return None
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if isinstance(verdict, export_catalog.Excluded) \
                and verdict.reason == export_catalog.REASON_LOOP_GUARD:
            return export_catalog.REASON_LOOP_GUARD
        return None

    def _reconcile_exports(self) -> None:
        """Report-only startup sweep of the allow-list (never edits it).

        An export whose device has been deleted, or which no longer classifies
        as exportable, is a real problem the user should hear about at startup
        rather than discovering as a missing accessory. It is NOT auto-removed:
        the allow-list is the user's declaration, and E3 re-classifies at
        endpoint-build time anyway.
        """
        if self.exports is None:
            return
        try:
            plugin_id = self._export_plugin_id()
            for entry in self.exports.all():
                dev = self._indigo_device(entry.indigo_device_id)
                if dev is None:
                    self.logger.warning(
                        "Matter export allow-list: device %s is exported as %s but no longer "
                        "exists in Indigo — it will not be bridged. Remove it in "
                        "'Manage Matter Exports…'.",
                        entry.indigo_device_id, entry.role)
                    continue
                verdict = export_catalog.classify(dev, plugin_id)
                if isinstance(verdict, export_catalog.Excluded):
                    self.logger.warning(
                        "Matter export allow-list: %s (id %s) is exported as %s but is no longer "
                        "exportable: %s. It will not be bridged.",
                        getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                        verdict.reason)
                elif entry.role not in verdict.eligible_roles:
                    self.logger.warning(
                        "Matter export allow-list: %s (id %s) is exported as %s, which this "
                        "device no longer offers (%s). Re-pick its role in "
                        "'Manage Matter Exports…'.",
                        getattr(dev, "name", ""), entry.indigo_device_id, entry.role,
                        ", ".join(verdict.eligible_roles))
        except Exception as exc:  # pylint: disable=broad-except
            # A diagnostic sweep must never be the thing that fails startup.
            self.logger.exception(exc)

    def _export_summary(self) -> str:
        if self.exports is None:
            return "Plugin still starting — reopen this dialog in a moment."
        count = len(self.exports)
        # A load failure has to lead. Reporting "Nothing is exported yet." over
        # a blob we could not read invites the user to rebuild the list from
        # scratch, and the rebuild's first save overwrites the rescue copy.
        error = self.exports.load_error
        if error:
            return error if not count else \
                f"{error} {count} device(s) exported.{self._export_bridge_note()}"
        if not count:
            return "Nothing is exported yet."
        summary = f"{count} device(s) exported."
        # An export whose role this version cannot bridge is silently absent
        # from every ecosystem otherwise — the dialog is the only place the user
        # would ever look for the reason.
        pending = sum(1 for entry in self.exports.all()
                      if not export_handlers.is_bridgeable(entry.role))
        if pending:
            # E4 completed the v1 role table, so this can now only mean an
            # allow-list written by a NEWER plugin than the one running — the
            # export blob lives in plugin prefs and survives a downgrade.
            summary += (f" {pending} of them use a role this version cannot bridge "
                        "and will not appear in any ecosystem — they were most likely "
                        "added by a newer version of this plugin.")
        return summary + self._export_bridge_note()

    def _export_bridge_note(self) -> str:
        """One sentence when the exports exist but are not actually live.

        "3 device(s) exported." is true and useless while the bridge client is
        halted on a version skew: the user is looking at this dialog precisely
        because a light is missing from the Home app, and every state below
        answers that question. Reported as a suffix so a load error — which is
        about rescuing the user's list, and outranks everything — still leads.
        """
        bridge = self.export_bridge
        if bridge is None or not bridge.active:
            # No client is the CORRECT state for an empty allow-list (XG5), and
            # the count above already says the list is not empty — so this is a
            # plugin still starting, which its own log line covers.
            return ""
        client = bridge.client
        if client.halted:
            return (f" Bridge client halted ({client.halted_reason or 'no reason recorded'}) "
                    "— restart the bridge node.")
        if client.recovery:
            return (" The bridge node is waiting for an endpoint-map rebuild — exports are not "
                    "live until it is done. Use 'Rebuild Matter Endpoint Map…' in the plugin "
                    "menu.")
        if not client.attached:
            return " Not connected to the bridge node — exports are not live."
        return self._export_health_note(client.status)

    @staticmethod
    def _export_health_note(status) -> str:
        """The §4.3 facts the dialog is the only place a user would look for.

        `drift` and `warnings` were parsed and then read by nobody: an endpoint
        number that had moved, or a map the node could not write, showed up in
        the log at the moment it happened and nowhere at all afterwards. This
        dialog is where somebody goes when an accessory is behaving oddly.
        """
        if status is None:
            return ""
        if status.warnings:
            return (f" The bridge node reports {len(status.warnings)} persistence problem(s): "
                    f"{'; '.join(status.warnings)}")
        if status.drift:
            return (f" WARNING: {len(status.drift)} exported accessory number(s) have DRIFTED — "
                    "they may have swapped identities in paired ecosystems. See the log; this is "
                    "never repaired automatically.")
        if not status.drift_checked:
            return (" Endpoint numbers have not been checked against a saved map yet — that "
                    "happens on the first reconcile.")
        return ""

    def get_menu_action_config_ui_values(self, menu_id):
        """Seed the export and unpair dialogs (menu dialogs never remember values).

        Only those two are seeded — this callback fires for EVERY menu item that
        has a ConfigUI, and returning values for another one would overwrite its
        defaults.
        """
        values = indigo.Dict()
        if menu_id == MENU_UNPAIR_ECOSYSTEM:
            # The picker leads with a no-selection row; seed the field to match
            # it, or Indigo renders the seeded-but-unmatched value as a blank
            # first item and the user is one click from unpairing whatever
            # happens to be second.
            values["fabric"] = NO_SELECTION_ID
            return values
        if menu_id != MENU_MANAGE_EXPORTS:
            return values
        values["exportFilter"] = ""
        values["exportDevice"] = NO_SELECTION_ID
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportStatus"] = self._export_summary()
        return values

    def _log_row_failure(self, exc, first: bool) -> None:
        """Log one unreadable picker row — stack for the first, one line after.

        A database with fifty broken proxies must not write fifty tracebacks
        into the event log, but the first one has to carry enough to debug.
        """
        if first:
            self.logger.exception(exc)
        else:
            self.logger.error("Matter bridge: another device could not be read — %s", exc)

    @staticmethod
    def _candidate_row(dev, name: str, plugin_id: str, exported) -> Optional[tuple[str, str]]:
        """One picker row for ``dev``, or None to omit it. May raise — the caller contains it.

        Loop-guard devices (created by this plugin) return None: XAC6 requires
        them ABSENT from the picker, not merely unpickable — every one of them
        shadows a device the user already sees, so listing them as excluded
        would only add noise. Every OTHER exclusion is listed with its reason
        (XAC9); hiding those would leave a user hunting for a device that never
        appears.
        """
        device_id = dev.id
        # An excluded device that IS exported keeps its marker: the pair
        # "excluded" + "exported" is exactly the state the user has to know
        # about, and hiding half of it reads as a picker bug rather than the
        # stale export it actually is.
        mark = "● " if device_id in exported else ""
        verdict = export_catalog.classify(dev, plugin_id)
        if isinstance(verdict, export_catalog.Excluded):
            if verdict.reason == export_catalog.REASON_LOOP_GUARD:
                return None
            return (f"{EXCLUDED_OPTION_PREFIX}{device_id}",
                    f"{mark}{name} — not exportable: {verdict.reason}")
        return (str(device_id), f"{mark}{name}")

    def getExportCandidates(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument, too-many-locals
        """Picker rows: every Indigo device, exportable or not (XAC9).

        Excluded devices are listed **with the reason in the label** and an
        ``x-``-prefixed id so the callbacks can reject the pick cleanly —
        hiding them would leave a user hunting for a device that will never
        appear. ``filter`` here is the XML's static filter attribute, NOT the
        user's text: textfields have no callbacks, so the typed filter arrives
        in ``valuesDict`` and the Apply-filter button drives the reload.

        One device that cannot be read costs one row, not the whole list: the
        try/except is INSIDE the loop, because the alternative is a dialog that
        renders empty the moment any device in the database misbehaves. That
        promise holds below :data:`EXPORT_PICKER_LIMIT`; past it, unreadable
        devices are counted in the truncation tail like any other row, and the
        log still carries every one.

        Ordering: the seeded ``(select a device)`` row is always first, then
        every already-exported device (database order), then everything else
        (also database order) — never alphabetised. This dialog is the only
        place a user can remove an export, so an exported device buried past
        :data:`EXPORT_PICKER_LIMIT` would be effectively stuck there; exported
        rows are therefore classified and kept unconditionally, and the cap is
        applied only to the rest, at the end, once the exported count is known.
        """
        try:
            text = str((valuesDict or {}).get("exportFilter", "") or "").strip().lower()
            exported = self.exports.ids() if self.exports is not None else frozenset()
            plugin_id = self._export_plugin_id()
            # Always a real row for the seeded value, and always first.
            options: list[tuple[str, str]] = [(NO_SELECTION_ID, NO_SELECTION_LABEL)]
            # One pass, two row lists: exported rows are never truncated (see
            # docstring), so the cap is applied only to `other_rows`, after the
            # loop, once `len(exported_rows)` is known.
            exported_rows: list[tuple[str, str]] = []
            other_rows: list[tuple[str, str]] = []
            truncated = 0
            failures = 0
            for dev in indigo.devices:
                try:
                    name = str(getattr(dev, "name", "") or "")
                    if text and text not in name.lower():
                        continue
                    is_exported = dev.id in exported
                    if not is_exported and len(other_rows) >= EXPORT_PICKER_LIMIT:
                        # Upper bound on what the cap below could ever keep —
                        # exported rows only shrink that allowance, never
                        # raise it — so it's safe to stop building rows here.
                        # Still counted, so the tail stays honest.
                        truncated += 1
                        continue
                    row = self._candidate_row(dev, name, plugin_id, exported)
                    if row is None:               # loop guard: absent, not excluded (XAC6)
                        continue
                    (exported_rows if is_exported else other_rows).append(row)
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)
                    failures += 1
                    # `dev.id` may be exactly what failed to read, so an
                    # unreadable device can't be safely tested for membership
                    # in `exported` — it always lands with the others.
                    other_rows.append((f"{EXCLUDED_OPTION_PREFIX}err{failures}",
                                       f"— {ROW_ERROR_LABEL}"))
            allowance = max(EXPORT_PICKER_LIMIT - len(exported_rows), 0)
            truncated += max(len(other_rows) - allowance, 0)
            options.extend(exported_rows)
            options.extend(other_rows[:allowance])
            if truncated:
                options.append(TRUNCATED_OPTION)
            if len(options) == 1:
                options.append(NO_MATCH_OPTION)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def getExportRoles(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Roles the picked device may legitimately be exported as (§5.2).

        Empty for no selection or an excluded pick — an empty role menu is the
        honest rendering of "there is nothing you may choose here". An outright
        failure is NOT empty: it says so, so the user does not read a broken
        callback as "this device offers no roles".
        """
        try:
            kind, device_id = self._export_selection(valuesDict)
            if kind != "device":
                return []
            dev = self._indigo_device(device_id)
            if dev is None:
                return []
            verdict = export_catalog.classify(dev, self._export_plugin_id())
            if isinstance(verdict, export_catalog.Excluded):
                return []
            options = []
            for role in verdict.eligible_roles:
                try:
                    options.append((role, export_catalog.role_label(role)))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not options)
            return options
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def getCurrentExports(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Read-only summary of the allow-list (one row per export)."""
        try:
            if self.exports is None:
                return [(NO_SELECTION_ID, "(plugin still starting)")]
            options = []
            failures = 0
            for entry in self.exports.all():
                try:
                    dev = self._indigo_device(entry.indigo_device_id)
                    name = str(getattr(dev, "name", "") or "") if dev is not None else ""
                    if not name:
                        name = f"(deleted device {entry.indigo_device_id})"
                    label = f"{name} → {export_catalog.role_label(entry.role)}"
                    if entry.name_override:
                        label += f' · shown as "{entry.name_override}"'
                    if entry.options.get(OPTION_INVERT):
                        label += " · inverted"
                    options.append((str(entry.indigo_device_id), label))
                except Exception as exc:  # pylint: disable=broad-except
                    self._log_row_failure(exc, first=not failures)
                    failures += 1
                    options.append((f"{EXCLUDED_OPTION_PREFIX}err{len(options)}",
                                    f"— {ROW_ERROR_LABEL}"))
            return options or [(NO_SELECTION_ID, "(nothing exported yet)")]
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def _exported_warning(self, device_id: int) -> str:
        """Suffix warning shown when an EXCLUDED device is nonetheless exported.

        This is the incoherent state worth naming out loud: the allow-list says
        export it, the catalog says it cannot be. Left alone it becomes an
        accessory that never appears, with no visible cause.
        """
        if self.exports is not None and device_id in self.exports:
            return " — but this device IS currently exported — remove it or it will fail to bridge"
        return ""

    def exportReloadPicker(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Apply-filter button: the return trip is what reloads the lists."""
        values = valuesDict
        text = str(values.get("exportFilter", "") or "").strip()
        values["exportStatus"] = (f'Filtered on "{text}". {self._export_summary()}' if text
                                  else self._export_summary())
        return values

    def exportDeviceChanged(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Picker selection changed: load that device's saved export, or defaults.

        Menu callbacks return a valuesDict, not an error dict (the SDK's menu
        contract), so an excluded pick is reported in the read-only status
        field here — and refused again by the Add/update button below. Both
        paths are covered by tests.
        """
        values = valuesDict
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = self._export_summary()
            return values
        dev = self._indigo_device(device_id)
        if kind == "excluded" or dev is None:
            reason = "that device no longer exists"
            if dev is not None:
                verdict = export_catalog.classify(dev, self._export_plugin_id())
                reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                    else "that device is not exportable"
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = (f"Not exportable: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if isinstance(verdict, export_catalog.Excluded):
            values["exportRole"] = ""
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = (f"Not exportable: {verdict.reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        entry = self.exports.get(device_id) if self.exports is not None else None
        if entry is not None:
            values["exportRole"] = entry.role
            values["exportName"] = entry.name_override or ""
            values["exportInvert"] = bool(entry.options.get(OPTION_INVERT, False))
            values["exportStatus"] = f"{dev.name} is exported as {export_catalog.role_label(entry.role)}."
        else:
            values["exportRole"] = verdict.default_role
            values["exportName"] = ""
            values["exportInvert"] = False
            values["exportStatus"] = f"{dev.name} is not exported yet."
        return values

    def exportAddOrUpdate(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Add or update one export. Validates the role against the catalog.

        A role the catalog does not offer for this device is refused here
        rather than by the bridge node, which would only reject it with
        ``unknown_role``/``role_change`` long after the user could connect the
        failure to what they did (BRIDGE_PROTOCOL §1.1).

        Returns **the values dict only**. A ``(valuesDict, errorsDict)`` tuple
        is the documented contract for *validation* methods, not for button
        ``CallbackMethod``s — the SDK's button reference says a button callback
        returns a dictionary of field changes, and the field carrying a button's
        outcome is read-only, so it cannot hold an error message anyway. Every
        refusal therefore lands in ``exportStatus``, which is what the dialog
        actually shows.
        """
        values = valuesDict
        if self.exports is None:
            values["exportStatus"] = "Plugin still starting — try again in a moment."
            return values
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportStatus"] = "Select a device to export."
            return values
        dev = self._indigo_device(device_id)
        if dev is None:
            values["exportStatus"] = "That device no longer exists — refresh the list."
            return values
        verdict = export_catalog.classify(dev, self._export_plugin_id())
        if kind == "excluded" or isinstance(verdict, export_catalog.Excluded):
            reason = verdict.reason if isinstance(verdict, export_catalog.Excluded) \
                else "not exportable"
            values["exportStatus"] = (f"{dev.name} cannot be exported: {reason}"
                                      f"{self._exported_warning(device_id)}")
            return values
        role = str(values.get("exportRole", "") or "")
        if role not in verdict.eligible_roles:
            values["exportStatus"] = ("Choose how this device should appear "
                                      f"({', '.join(verdict.eligible_roles)}).")
            return values
        name_override = str(values.get("exportName", "") or "").strip() or None
        options = {}
        if role == export_catalog.ROLE_WINDOW_COVERING and self._truthy(values.get("exportInvert")):
            options[OPTION_INVERT] = True
        previous = self.exports.get(device_id)
        existed = previous is not None
        role_changed = existed and previous.role != role
        try:
            self.exports.upsert(ExportEntry(
                indigo_device_id=device_id, role=role,
                name_override=name_override, options=options,
            ))
        except Exception as exc:  # pylint: disable=broad-except
            # The store rolled back, so nothing was saved — say so rather than
            # reporting the success the old code reported unconditionally.
            self.logger.error("Matter bridge: saving the export list FAILED — %s", exc)
            self.logger.exception(exc)
            values["exportStatus"] = "FAILED to save the export list — see Event Log"
            return values
        verb = "Updated" if existed else "Added"
        self.logger.info("%s Matter export for %s (id %s) as %s%s",
                         verb, dev.name, device_id, role,
                         f' named "{name_override}"' if name_override else "")
        self._nudge_export(device_id, role_changed=role_changed)
        values["exportStatus"] = f"{verb} {dev.name} as {export_catalog.role_label(role)}. " \
                                 f"{self._role_change_warning(role_changed)}{self._export_summary()}"
        return values

    @staticmethod
    def _role_change_warning(role_changed: bool) -> str:
        """What a role change actually costs the user, said before they find out.

        BRIDGE_PROTOCOL §4.1 rejects changing an existing endpoint's role, so the
        plugin removes and re-adds it. Ecosystems treat that as a brand-new
        accessory: the name and room it was given in Apple Home are gone.
        """
        if not role_changed:
            return ""
        return ("Changing the role RE-CREATES the accessory, so it loses the name and room "
                "you gave it in Apple Home and any other paired ecosystem. ")

    def _nudge_export(self, device_id: int, *, role_changed: bool = False) -> None:
        """Tell the bridge about one changed export, without a full reconnect.

        A role change is the one case that cannot be an ``upsert``: §4.1 refuses
        it with ``role_change``, so it becomes remove-then-add.
        """
        self._exports_changed()
        bridge = self.export_bridge
        if bridge is None:
            return
        try:
            if role_changed:
                bridge.replace(device_id)
            else:
                bridge.upsert(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)

    def exportRemove(self, valuesDict, typeId="", devId=0):
        # pylint: disable=unused-argument
        """Drop the picked device from the allow-list. Returns values only (see above)."""
        values = valuesDict
        if self.exports is None:
            values["exportStatus"] = "Plugin still starting — try again in a moment."
            return values
        kind, device_id = self._export_selection(values)
        if kind == "none":
            values["exportStatus"] = "Select a device to remove from the export list."
            return values
        try:
            removed = self.exports.remove(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.error("Matter bridge: saving the export list FAILED — %s", exc)
            self.logger.exception(exc)
            values["exportStatus"] = "FAILED to save the export list — see Event Log"
            return values
        if not removed:
            values["exportStatus"] = "That device is not exported."
            return values
        dev = self._indigo_device(device_id)
        name = str(getattr(dev, "name", "") or "") if dev is not None else f"device {device_id}"
        self.logger.info("Removed Matter export for %s (id %s)", name, device_id)
        # XAC7: the accessory has to leave every paired ecosystem, not just the
        # allow-list. Order matters — remove the endpoint BEFORE the empty
        # allow-list stops the client out from under it.
        if self.export_bridge is not None:
            try:
                self.export_bridge.remove(device_id)
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception(exc)
        self._exports_changed()
        values["exportRole"] = ""
        values["exportName"] = ""
        values["exportInvert"] = False
        values["exportStatus"] = f"Removed {name}. {self._export_summary()}"
        return values

    # ------------------------------------------------------------------
    # Export-bridge recovery menus (BRIDGE_PROTOCOL §3.10/§3.11)
    # ------------------------------------------------------------------
    def _recovery_client(self, errors, field: str):
        """The bridge client, or ``None`` with ``errors`` filled in.

        Both recovery commands need a live socket, and the state they exist to
        fix is exactly the one where the plugin holds the connection open
        UN-attached (§1.1 recovery). So `connected`, not `attached`, is the
        right gate — requiring an attach would make the rebuild unreachable in
        the only situation that needs it.
        """
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        if client is None or not client.connected:
            msg = ("Not connected to the Matter bridge node. Start it (it is launched by "
                   "hand in this build), export at least one device so the plugin connects, then "
                   "try again.")
            self.logger.warning(msg)
            errors[field] = "Not connected to the bridge node — see the log."
            return None
        return client

    def menuRebuildEndpointMap(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """§3.11 — the way out of the endpoint_map_invalid refuse-to-start state.

        Two outcomes are reported separately, because they can differ and the
        expensive one is the first: the rebuild is irreversible (it discards
        the persisted baseline and adopts the live numbers) while the re-attach
        that follows it is an ordinary connection step that retries on its own.
        It renumbers nothing, so it cannot itself duplicate accessories — any
        duplication belongs to the storage loss that caused the refusal (#132).
        Reporting the pair as one used to tell users their node was "unchanged
        and still refusing" over a map that had already been rewritten — and
        invite them to do it again.
        """
        errors = indigo.Dict()
        if not self._truthy(valuesDict.get("confirm")):
            errors["confirm"] = ("Tick the box — a rebuild replaces the endpoint-number record "
                                 "and cannot be undone.")
            return (False, valuesDict, errors)
        client = self._recovery_client(errors, "confirm")
        if client is None:
            return (False, valuesDict, errors)
        if not client.recovery:
            # M11: `connected` is the right gate for REACHING the node, but it
            # is not a reason to rebuild. Run against a healthy node this
            # silently discards the retained endpoint-number allocations of
            # every export that is not currently live — §3.3 keeps them exactly
            # so that re-adding a device restores its accessory — and there is
            # nothing to recover from in the first place.
            msg = ("Matter bridge: the bridge node is NOT refusing to serve endpoints, so there "
                   "is nothing to rebuild. Rebuilding anyway would discard the retained endpoint "
                   "numbers of every device that is not currently exported, and those are what "
                   "make re-adding one restore the same accessory.")
            self.logger.warning(msg)
            errors["confirm"] = ("The bridge node is not refusing to export — there is nothing "
                                 "to rebuild. See the log.")
            return (False, valuesDict, errors)
        # Derived from the same two deadlines the call itself is built from: a
        # flat number here is the one that expires first on a large export list,
        # turning a rebuild that worked into a reported failure.
        deadline = bridge_client.rebuild_timeout_for(
            len(self.exports) if self.exports is not None else 0)
        try:
            status = self.runtime.submit(client.rebuild_endpoint_map()).result(timeout=deadline)
        except Exception as exc:  # noqa: BLE001
            # Never report success over a rebuild that did not persist: the node
            # answers with an error rather than a StatusReport when the new map
            # could not be written, and the refusal is still in force.
            self.logger.error("Matter bridge: rebuilding the endpoint map FAILED — %s. The bridge "
                              "node is unchanged and still refusing to export.", exc)
            self.logger.exception(exc)
            errors["confirm"] = "Rebuild failed — see the log. Nothing was changed."
            return (False, valuesDict, errors)
        self.logger.warning(
            "Matter bridge: endpoint map REBUILT — the bridge node has stopped refusing and is "
            "serving %d endpoint(s). Nothing was renumbered. If only the map file was damaged, "
            "no paired ecosystem will see any change; if the bridge's Matter storage was lost, "
            "your ecosystems already hold dead accessories under the old numbers — delete those "
            "by hand. Do NOT run this again for the same fault.",
            status.endpoint_count)
        for warning in status.warnings:
            self.logger.warning("Matter bridge: the bridge node reports — %s", warning)
        if not client.attached:
            # The rebuild stands; only the connection step after it did not. The
            # client's own triage has already named the reason at error level.
            self.logger.warning(
                "Matter bridge: the rebuild succeeded but re-attaching to the bridge node did "
                "not. The rebuild does NOT need repeating — exports resume when the connection "
                "does, and the reason is logged above.")
        return (True, valuesDict)

    def menuResetBridgePairings(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """§3.10 — wipe the Matter bridge's commissioning and re-advertise."""
        errors = indigo.Dict()
        # Two boxes, deliberately. This is the only plugin action that destroys
        # every ecosystem pairing at once, and it is irreversible without
        # re-pairing each ecosystem by hand.
        if not self._truthy(valuesDict.get("confirm")) \
                or not self._truthy(valuesDict.get("confirmAgain")):
            field = "confirm" if not self._truthy(valuesDict.get("confirm")) else "confirmAgain"
            errors[field] = "Tick BOTH boxes — this removes every ecosystem pairing."
            return (False, valuesDict, errors)
        client = self._recovery_client(errors, "confirmAgain")
        if client is None:
            return (False, valuesDict, errors)
        try:
            # preserve_endpoint_numbers=True: a user resetting to re-pair the
            # same ecosystems should not also lose accessory identity. The
            # "the map itself is corrupt" path is the rebuild above.
            self.runtime.submit(client.factory_reset(True)).result(timeout=FACTORY_RESET_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Matter bridge: resetting the bridge pairings FAILED — %s. "
                              "Pairings are unchanged.", exc)
            self.logger.exception(exc)
            errors["confirmAgain"] = "Reset failed — see the log. Pairings were not changed."
            return (False, valuesDict, errors)
        self.logger.warning(
            "Matter bridge: the bridge node's pairings have been RESET. It is advertising for "
            "commissioning again — pair it from each ecosystem, and remove the now-dead Indigo "
            "bridge from any ecosystem that still lists it.")
        return (True, valuesDict)

    # ------------------------------------------------------------------
    # The Matter bridge node's LaunchAgent (E7 — PRD §4.2, XG5, XAC1)
    # ------------------------------------------------------------------
    def _start_bridge_agent(self) -> None:
        """Install (if needed) and start the bridge node's LaunchAgent.

        The ``agent_start`` seam. Called by :class:`export_bridge.ExportBridge`
        on the empty→non-empty allow-list transition, on whichever Indigo thread
        made that change — never on the loop. Blocking, but only by a couple of
        ``launchctl`` calls; ``install`` is deliberately NOT attempted here (npm
        takes a minute and this can run from ``deviceDeleted``), so a missing
        package surfaces as ``ensure_installed``'s actionable preflight error
        naming the install menu.

        Rebuilt from current prefs on every call rather than cached: the ports
        and the mDNS interface are prefs, ``ensure_installed`` only reloads
        launchd when the resulting plist actually differs, and a stale
        ``BridgeProcess`` would write yesterday's ports while reporting success.
        """
        self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        agent = self.bridge_process
        if agent.ensure_installed() is None:
            # Preflight failed; the plist has been torn down and the reason
            # logged. Nothing to start, and starting would only crash-loop.
            return
        # ``ensure_installed() is not None`` is NOT the process being up. It is
        # False for "the current definition was already loaded and healthy" AND
        # for "bootout succeeded but neither bootstrap nor load did" AND for "the
        # job is loaded with a pid line we could not parse" — and the middle one
        # was reported here as "bridge node LaunchAgent is running". Ask launchd.
        state = agent.run_state()
        if state == agent.RUNNING:
            self.logger.info("Matter bridge: bridge node LaunchAgent is running (protocol port %s, "
                             "Matter port %s)", agent.ws_port, agent.matter_port)
        elif state == agent.UNKNOWN:
            # A pid line we could not parse. It may well be serving; what we must
            # not do is assert either way.
            self.logger.info(
                "Matter bridge: the bridge node's LaunchAgent is loaded (protocol port %s, Matter "
                "port %s); launchd did not report a readable pid, so whether the process is up "
                "will show as the plugin connects — or fails to.", agent.ws_port, agent.matter_port)
        else:
            self.logger.error(
                "Matter bridge: the bridge node's LaunchAgent %s. Exported accessories will be "
                "unavailable until it does. %s",
                "did not start" if state == agent.LOADED_NOT_RUNNING
                else "could not be loaded by launchd",
                self._bridge_agent_diagnosis())

    def _stop_bridge_agent(self) -> None:
        """Stop the bridge agent and REMOVE its plist. The storage is untouched.

        The ``agent_stop`` seam, called only once there is genuinely nothing to
        serve.

        **``uninstall()`` rather than ``stop()``, and that is a correction.**
        ``stop()`` boots the job out and keeps the plist, which reads as a
        thrifty choice until you notice the plist carries ``RunAtLoad: True``:
        at the next login launchd started an *unpaired* bridge node with an
        EMPTY allow-list, advertising on the Matter port, that this plugin never
        started and — because ``_agent_started`` is false in a session that
        never brought it up (XAC1) — would never stop. XG5's guarantee is that a
        fresh or emptied install runs no bridge process, and a guarantee that
        does not survive a reboot is not one. Re-deriving the plist costs one
        ``ensure_installed`` on the next export.

        The storage dir — every ecosystem pairing plus the endpoint-number
        witness — is never touched by either (PRD §5.4).
        """
        agent = self.bridge_process
        if agent is None:
            return
        was_loaded = agent.is_running()      # "is there a job on the books"
        agent.uninstall()                    # bootout + remove the plist
        if agent.is_running() or os.path.exists(agent.plist_path):
            # ⊗ The silent branch. stop() returning False used to say nothing at
            # all, and `_agent_started` had already been cleared, so nothing
            # retried: the node kept serving every paired ecosystem with the log
            # asserting the opposite by omission.
            self.logger.warning(
                "Matter bridge: nothing is exported, but the bridge node's LaunchAgent could not "
                "be %s (%s). It keeps running and serving every paired ecosystem, and NOTHING "
                "retries this on its own — reload the plugin, or run 'launchctl bootout "
                "gui/$(id -u)/%s' and delete that file by hand.",
                "stopped" if agent.is_running() else "removed", agent.plist_path,
                bridge_agent.LABEL)
            return
        if was_loaded:
            self.logger.info(
                "Matter bridge: nothing is exported — the bridge node has been stopped and its "
                "LaunchAgent removed, so a restart of this Mac cannot bring it back. Its pairings "
                "are kept.")
        else:
            # The two Falses `stop()` conflated: this one is "there was no job",
            # which is not a failure and must not be reported as one.
            self.logger.debug(
                "Matter bridge: nothing is exported and no bridge node LaunchAgent was loaded; "
                "any plist has been removed. Pairings are kept.")

    def _bridge_agent_diagnosis(self) -> Optional[str]:
        """Why is the bridge node not answering? The ``agent_diagnose`` seam.

        Reads the agent's own error log, which is the only place the real cause
        appears: a Matter port already bound by another stack (PRD §7), a package
        that was never installed, an ABI mismatch. The socket sees "connection
        refused" for all of them.

        **Read-only.** It deliberately does not restart anything. launchd already
        owns respawn via ``KeepAlive``, the loaded-but-dead revival lives in
        ``ensure_installed``, and a diagnostic that quietly bounced the agent on
        every failure streak would turn a crash-loop into a crash-loop nobody can
        read the log of.
        """
        agent = self.bridge_process
        if agent is None:
            return ("The bridge node's LaunchAgent has not been started by this plugin session — "
                    "export at least one device, or reload the plugin.")
        # ⊗ Asked FIRST, and it was not asked at all. preflight() holds the
        # actual fact — is the node interpreter there, is the package installed —
        # while the old code guessed at it from an empty error log and then said
        # "checked {project_dir}", which it had not looked at. A missing package
        # is also the case where the error log is empty *for the right reason*:
        # launchd never got far enough to write one.
        problem = agent.preflight()
        if problem:
            return f"The bridge node cannot start: {problem}"
        tail = agent.tail_error_log()
        if tail:
            # NOT "recent". The file is appended to and never truncated, so the
            # last 20 lines can be from a crash-loop days ago that has since been
            # fixed — naming the file is what lets the user check the timestamps.
            return (f"The last lines of {os.path.join(agent.log_dir, bridge_agent.BRIDGE_ERR_LOG)} "
                    f"(appended to since the bridge was first started, so these may be old):\n"
                    f"{tail}")
        return (f"The {bridge_agent.BRIDGE_PACKAGE} package is installed and its error log "
                f"({os.path.join(agent.log_dir, bridge_agent.BRIDGE_ERR_LOG)}) is empty, so the "
                f"node is failing without saying why — check that nothing else on this Mac holds "
                f"Matter port {agent.matter_port} or protocol port {agent.ws_port}.")

    def menuInstallBridgeNode(self):  # noqa: N802
        """Install/update the ``indigo-matter-bridge`` npm package.

        The export-side twin of ``menuInstallMatterServer``, and a sibling rather
        than an extension of it: the two agents are separately versioned, and a
        user recovering a wedged bridge must not also be made to reinstall a
        controller that is working (or the reverse). They share the install
        thread because ``~/indigo-matter`` is one npm root and two concurrent
        ``npm install``s into it corrupt each other.
        """
        self._run_bridge_install(clean=False)

    def menuReinstallBridgeNodeClean(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Remove the bridge package and install it fresh (the controller's twin).

        The controller has had this exit since 2026.7; the bridge shipped without
        it, so a user whose bridge install was wedged had a menu that reinstalled
        *over* the wedge and no way to clear it. ``remove_package`` is per-package
        since E7, which is what makes this safe to offer at all — it used to
        rmtree the shared ``node_modules`` and take the controller with it.

        Pairings are untouched: they live in the storage dir, which nothing in
        the install path goes near.
        """
        errors = indigo.Dict()
        if not self._truthy(valuesDict.get("confirm")):
            errors["confirm"] = "Tick the box to confirm."
            return (False, valuesDict, errors)
        if not self._run_bridge_install(clean=True):
            errors["confirm"] = "An npm install is already running — wait for it to finish."
            return (False, valuesDict, errors)
        return (True, valuesDict)

    def _run_bridge_install(self, *, clean: bool) -> bool:
        """Start the background bridge install. False if one is already running."""
        if self._install_thread is not None and self._install_thread.is_alive():
            self.logger.warning("An npm install is already in progress — wait for it to finish.")
            return False
        self.logger.info(
            "%s the Matter bridge node in the background — watch the log for progress; "
            "this can take a minute.",
            "Removing and reinstalling" if clean else "Installing")
        self._install_thread = threading.Thread(
            target=self._install_bridge_node, args=(clean,),
            name="matter-bridge-install", daemon=True)
        self._install_thread.start()
        return True

    def menuStopBridgeNode(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Stop the bridge node and remove its LaunchAgent, by hand.

        The controller has "Restart the Matter controller"; the bridge had nothing at
        all, because it is started and stopped by the allow-list. That leaves one
        state with no UI: a user who disables the plugin (or whose plugin dies
        mid-session) has a node still running and still serving every paired
        ecosystem, and the only lever is ``launchctl``. Exporting nothing is not
        that lever — it needs the plugin to be running to notice.

        Exports are NOT changed. The next export starts the node again, which is
        exactly XG5 and is why this is safe to hand a user: the worst outcome is
        a bridge that comes back.
        """
        errors = indigo.Dict()
        if not self._truthy(valuesDict.get("confirm")):
            errors["confirm"] = "Tick the box to confirm."
            return (False, valuesDict, errors)
        # Built from CURRENT prefs rather than reused: this must work in a
        # session that never started the agent, which is the whole point of it.
        self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        self._stop_bridge_agent()
        if self.export_bridge is not None:
            # The plugin no longer has an agent it started, so the XAC1 latch
            # must not go on claiming it does.
            self.export_bridge.note_agent_stopped()
        return (True, valuesDict)

    def _install_bridge_node(self, clean: bool = False) -> None:
        """npm-install the bridge package, then restart it if anything is exported.

        The restart is conditional on there being something to export, which is
        the difference from the controller's install: bringing the agent up
        because a package was updated would violate XG5 on an install with an
        empty allow-list, and leave a bridge process running for nothing.

        **``ensure_installed()`` before ``restart()``, and its absence was the
        first-run dead end.** The bridge's plist is written by exactly one place
        — ``_start_bridge_agent`` — and on a machine where the package has never
        been installed that place cannot get past its own preflight, so it writes
        no plist and (correctly) tears any stale one down. The user's route out
        of that is this menu; it then went install() → restart(), restart found
        no plist, and printed "nothing to restart. Fix the problem reported
        above" (there was no problem above — the install had just SUCCEEDED)
        followed by "the restart FAILED — the old version may still be running"
        (nothing was running). Two wrong messages, no bridge, and the only real
        remedy — write the plist now that the package exists — never attempted.
        """
        try:
            agent = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
            if clean and not agent.remove_package():
                # remove_package has said what is still there. Installing over a
                # wedged install is what the clean variant exists to avoid.
                self.logger.error(
                    "Clean reinstall of the Matter bridge ABANDONED — the old package "
                    "could not be removed, so nothing was reinstalled over it.")
                return
            if not agent.install():
                self.logger.error(
                    "Install/update of the Matter bridge did not complete — see the error "
                    "above. Nothing was changed; retry when resolved.")
                return
            if self._stopping:  # plugin is tearing down — don't mutate its state
                return
            self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
            if self.exports is None or not len(self.exports):
                self.logger.info(
                    "Matter bridge installed. It is NOT being started: nothing is exported "
                    "yet, and the bridge only runs while the export list is non-empty. Add a "
                    "device in 'Manage Matter Exports…' and it will start itself.")
                return
            # Write (or refresh) the plist first. On the first-run path there is
            # none — this is where it comes from — and `ensure_installed` returning
            # True means launchd has already bootstrapped the NEW files, so there
            # is nothing left for restart() to do and bouncing again would be a
            # second gratuitous outage.
            applied = self.bridge_process.ensure_installed()
            if applied is None:
                self.logger.error(
                    "The Matter bridge was installed, but its LaunchAgent could not be "
                    "written — see the reason above. The package is on disk; fix that and reload "
                    "the plugin.")
                return
            # A running LaunchAgent does not pick up new files on disk, so a job
            # left alone by ensure_installed is still executing the OLD version.
            if applied is False and not self.bridge_process.restart():
                self.logger.error(
                    "The Matter bridge was installed but the restart onto the new version "
                    "FAILED — the old version may still be running. Check %s.",
                    os.path.join(self.bridge_process.log_dir, bridge_agent.BRIDGE_ERR_LOG))
                return
            if self._stopping:
                # Second check, deliberately: ensure_installed()/restart() are
                # subprocess work that can outlast shutdown()'s 5s thread join,
                # and revive_after_install is the one caller of start() that
                # can genuinely race teardown — it would bootstrap launchctl
                # and then log a scary "could not schedule bridge client run
                # loop" over a plugin that is simply exiting.
                return
            # #154: a client HALTED on version skew is not the retry_now() case
            # below — it declines the poke by design (a halt is fail-closed) and
            # nothing revives it on its own, so the reinstall that was SUPPOSED
            # to fix it left the user with no route back except a plugin reload.
            # Tried first, and only ever replaces a client actually halted for
            # that reason — see `revive_after_install`'s own reason gate.
            revived = self.export_bridge is not None and self.export_bridge.revive_after_install()
            if revived:
                self.logger.info("Matter bridge installed and restarted onto the new "
                                 "version — the halted connection has been replaced; "
                                 "reconnecting now.")
            else:
                # Cuts the reconnect backoff short (issue #135): it grew to its
                # 30s ceiling while the package was missing, and without this
                # the user watches out the rest of that delay right after a
                # success message. `poked` is truthful, not assumed: no bridge,
                # no client, or a declining client (halted/closing/#154) all
                # mean the poke never reached a run loop, so the log must not
                # claim it is reconnecting.
                poked = self.export_bridge is not None and self.export_bridge.retry_now()
                if poked:
                    self.logger.info("Matter bridge installed and restarted onto the new "
                                     "version — reconnecting now.")
                else:
                    self.logger.info("Matter bridge installed and restarted onto the new "
                                     "version — reload the plugin to reconnect.")
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
            self.logger.error(
                "Install of the Matter bridge did not complete after the npm step — the "
                "package may be installed but the agent was not restarted. See the trace above, "
                "then retry Plugins ▸ Matter ▸ Install/update the Matter bridge.")

    # ------------------------------------------------------------------
    # Pairing and fabric management (PRD §6, BRIDGE_PROTOCOL §3.7-§3.9)
    # ------------------------------------------------------------------
    def _pairing_client(self, errors, field: str):
        """The bridge client for a pairing action, or ``None`` with ``errors`` set.

        ``connected`` rather than ``attached``, for the same §1.1 reason the
        recovery menus use it: the node answers ``get_pairing`` while refusing to
        serve endpoints, and a user whose bridge is in that state still needs to
        be able to see and manage their pairings.

        The message names the real precondition, which is not obvious: the client
        exists only while something is exported (XG5), so "pair the bridge" is
        genuinely unreachable until the user has exported a device. That is XAC2's
        ordering, not an accident — a bridge with no accessories is nothing worth
        pairing, and Apple Home would show an empty one.
        """
        bridge = self.export_bridge
        client = bridge.client if bridge is not None else None
        if client is None or not client.connected:
            exported = len(self.exports) if self.exports is not None else 0
            why = ("Export at least one device in 'Manage Matter Exports…' first — the bridge only "
                   "runs while something is exported." if not exported else
                   "The bridge node is not answering; see the log for what its own error log says.")
            self.logger.warning("Matter bridge: cannot reach the bridge node for pairing. %s", why)
            errors[field] = "Not connected to the bridge node — see the log."
            return None
        return client

    def menuPairMatterBridge(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """Open a pairing window and put the codes where the user can read them.

        **Why this is "open a window" and not "show the code" (PRD §6).** A Matter
        commissioning passcode is not durable: the moment the first ecosystem
        commissions, the basic window closes and the original code stops working.
        Every ecosystem after that needs an *enhanced* window with a freshly
        derived code (§3.8), so there is no such thing as "the" pairing code to
        display.

        **Why the event log.** Indigo dialogs have no dynamic labels and no image
        fields, so a runtime string cannot be shown in the dialog that produced
        it. The log is this plugin's established channel for exactly that, and it
        is also the one place the codes survive being scrolled past — a window
        lasts up to 15 minutes and users do not type 11 digits first time.
        """
        errors = indigo.Dict()
        duration = self._window_duration(valuesDict, errors)
        if duration is None:
            return (False, valuesDict, errors)
        client = self._pairing_client(errors, "duration")
        if client is None:
            return (False, valuesDict, errors)
        try:
            pairing = self.runtime.submit(client.get_pairing()).result(timeout=PAIRING_READ_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Matter bridge: could not read the bridge node's pairing state — %s. "
                              "No pairing window was opened.", exc)
            self.logger.exception(exc)
            errors["duration"] = "Could not reach the bridge node — see the log."
            return (False, valuesDict, errors)
        # Two states already have a usable code, and opening a window in either
        # would be actively harmful: §3.8's `assertClosed` refuses a second one,
        # and on a never-commissioned node the basic window is ALREADY open with
        # the persisted originals (§3.7) — deriving a fresh enhanced code there
        # would invalidate a code the user may already be typing.
        if pairing.window_open and pairing.manual_pairing_code:
            self._log_pairing_codes(pairing.manual_pairing_code, pairing.qr_pairing_code,
                                    pairing.window_expires_at,
                                    already_open=not pairing.commissioned)
            return (True, valuesDict)
        try:
            window = self.runtime.submit(
                client.open_commissioning_window(duration)).result(timeout=WINDOW_OPEN_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                "Matter bridge: opening a pairing window FAILED — %s. Nothing was changed and "
                "existing pairings are untouched. If the bridge says a window is already open, "
                "wait for it to expire (up to 15 minutes) and try again.", exc)
            self.logger.exception(exc)
            errors["duration"] = "Could not open a pairing window — see the log."
            return (False, valuesDict, errors)
        if self.export_bridge is not None:
            self.export_bridge.note_window_opened(window.window_expires_at)
        self._log_pairing_codes(window.manual_pairing_code, window.qr_pairing_code,
                                window.window_expires_at, already_open=False)
        return (True, valuesDict)

    @staticmethod
    def _window_duration(values_dict, errors) -> Optional[int]:
        """Validate the duration field against §3.8's 180-900s band.

        Rejected in the dialog rather than clamped silently by the node: the
        number is how long the user has to walk to another room with a phone, and
        being given 900 when they asked for 60 is a difference they should be
        told about while the dialog is still open.
        """
        raw = str((values_dict or {}).get("duration", "") or "").strip()
        if not raw:
            return bridge_protocol.DEFAULT_WINDOW_SECONDS
        try:
            duration = int(raw)
        except (TypeError, ValueError):
            errors["duration"] = "Enter a whole number of seconds between 180 and 900."
            return None
        if not 180 <= duration <= 900:
            errors["duration"] = "Matter allows 180 to 900 seconds (3 to 15 minutes)."
            return None
        return duration

    def _log_pairing_codes(self, manual: Optional[str], qr: Optional[str],
                           expires_at: Optional[str], *, already_open: bool) -> None:
        """Write the codes, the expiry and the QR page URL to the event log."""
        when = f" It expires at {expires_at}." if expires_at else ""
        opening = ("The bridge has never been paired, so it is ALREADY advertising with its "
                   "original code — no new window was opened." if already_open else
                   "A pairing window is now open.")
        self.logger.info(
            "Matter export — %s%s\n"
            "    Manual pairing code: %s\n"
            "    QR payload: %s\n"
            "    QR code page: %s\n"
            "Add the bridge in your ecosystem's app as you would any Matter accessory, and type "
            "the manual code if it asks for one. Expect an 'uncertified accessory' warning — that "
            "is normal for a bridge like this one; choose Add Anyway.\n"
            "SECURITY: while this window is open, anyone who can reach that page (or read this "
            "code) can add your exported Indigo devices to THEIR Apple Home, Alexa or Google "
            "account. The page is served by the Indigo Web Server, which asks for a password only "
            "if you have switched authentication on — turn it on before using this over anything "
            "but a network you trust, and do not share the URL.",
            opening, when, manual or "(none)", qr or "(none)", self._pairing_page_url())

    def _pairing_page_url(self) -> str:
        """The IWS URL of the QR page (Actions.xml ``pairing``).

        ``getWebServerURL`` picks the reflector, then the Bonjour name, then
        localhost — so this is reachable from the phone the user is holding
        whenever a reflector or a ``.local`` name exists, which is the case the
        page is FOR. A failure falls back to the loopback default rather than
        omitting the line: a wrong-host URL a user can edit beats no URL.
        """
        base = "http://localhost:8176"
        try:
            base = str(indigo.server.getWebServerURL() or base)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("could not resolve the Indigo web server URL (%s)", exc)
        return f"{base}/message/{self._export_plugin_id()}/pairing/"

    def getBridgeFabrics(self, filter="", valuesDict=None, typeId="", targetId=0):
        # pylint: disable=redefined-builtin, unused-argument
        """Picker rows for the unpair menu: one per commissioned ecosystem.

        Built from the fabric set the bridge already reported (attach, then every
        §5 ``fabrics_changed``), never from a fresh WS round trip: a dynamic list
        callback runs on the Indigo UI's thread while the dialog is opening, and
        blocking it on a node that may be down would hang the dialog rather than
        render an empty one. That is a deliberate trade and the reason
        :meth:`menuUnpairEcosystem` re-reads the set *after* it acts, and the
        reason §3.9 now reports whether it removed anything: the list can be
        stale, so nothing downstream may assume it is not.

        **The first row is always "(select an ecosystem)".** Indigo pre-selects
        row one, so without it the dialog opened with a real ecosystem already
        chosen on a menu whose Execute button removes it — every other picker in
        this plugin (device, node, backup) leads with a no-selection row for
        exactly this reason, and the one destructive picker did not.
        """
        try:
            bridge = self.export_bridge
            fabrics = bridge.fabrics if bridge is not None else None
            if not fabrics:
                # None and [] are different facts, and both are un-pickable, but
                # only one of them should read as "you are not paired".
                return [(NO_SELECTION_ID,
                         "(no paired ecosystems)" if fabrics == []
                         else "(not connected to the bridge node)")]
            return [(NO_SELECTION_ID, "(select an ecosystem)")] + [
                (str(fabric.fabric_index), export_bridge.describe_fabric(fabric))
                for fabric in fabrics]
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.exception(exc)
            return [LIST_ERROR_OPTION]

    def menuUnpairEcosystem(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        """§3.9 — remove one ecosystem's fabric from the bridge.

        Two gates, like the reset menu, because the outcome is the same size for
        the ecosystem being removed: every accessory Indigo exports disappears
        from it, with the names, rooms and automations built on them.
        """
        errors = indigo.Dict()
        selected = str(valuesDict.get("fabric", "") or "")
        if not selected or selected == NO_SELECTION_ID:
            errors["fabric"] = "Select an ecosystem to unpair."
            return (False, valuesDict, errors)
        try:
            fabric_index = int(selected)
        except (TypeError, ValueError):
            errors["fabric"] = "Invalid selection."
            return (False, valuesDict, errors)
        if not self._truthy(valuesDict.get("confirm")) \
                or not self._truthy(valuesDict.get("confirmAgain")):
            field = "confirm" if not self._truthy(valuesDict.get("confirm")) else "confirmAgain"
            errors[field] = "Tick BOTH boxes — this removes every exported accessory from that "\
                            "ecosystem."
            return (False, valuesDict, errors)
        client = self._pairing_client(errors, "confirmAgain")
        if client is None:
            return (False, valuesDict, errors)
        cached_last = self._is_last_fabric(fabric_index)
        try:
            removal = self.runtime.submit(
                client.remove_fabric(fabric_index)).result(timeout=UNPAIR_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Matter bridge: unpairing ecosystem %s FAILED — %s. Pairings are "
                              "unchanged.", fabric_index, exc)
            self.logger.exception(exc)
            errors["confirmAgain"] = "Unpair failed — see the log. Nothing was changed."
            return (False, valuesDict, errors)
        # The picker is built from a CACHED fabric list, so the ecosystem may
        # have unpaired itself since — which the node reports as a successful
        # no-op. Re-read before saying anything, so the picker cannot keep
        # offering a ghost and the sentence below is about the real outcome.
        self._refresh_fabric_cache(client)
        if not removal.removed:
            # ⊗ This used to be indistinguishable from a real removal: the node
            # answered `{}` either way and the menu logged "has been unpaired.
            # Every accessory has been removed" over a node-side no-op.
            self.logger.warning(
                "Matter bridge: ecosystem %s was ALREADY gone from the bridge node — nothing was "
                "removed by this action, because there was nothing there to remove. It had most "
                "likely unpaired itself since this dialog was opened. The ecosystem list has been "
                "refreshed%s.", fabric_index,
                f"; {removal.remaining} pairing(s) remain" if removal.remaining is not None else "")
            return (True, valuesDict)
        # `remaining` is the node's own post-removal count and beats the cache;
        # the cache is only the fallback for a node that could not read it.
        last = cached_last if removal.remaining is None else removal.remaining == 0
        if last:
            # §3.9: matter.js factory-resets itself when the fabric set empties,
            # and the node clears its commissioning witness to match. Say what
            # that actually means rather than reporting a routine removal, because
            # the user has just reset the whole bridge without using the reset menu.
            self.logger.warning(
                "Matter bridge: ecosystem %s was the LAST one paired, so the bridge node has "
                "reset itself and is advertising for commissioning again — exactly as 'Reset "
                "Matter Bridge Pairings…' would have done. Nothing in Indigo changed. Use "
                "'Pair Matter Bridge…' to pair it again.", fabric_index)
        else:
            self.logger.warning(
                "Matter bridge: ecosystem %s has been unpaired. Every accessory Indigo exports "
                "has been removed from it; remove any leftover 'Indigo' bridge entry in that "
                "ecosystem's app by hand.", fabric_index)
        return (True, valuesDict)

    def _refresh_fabric_cache(self, client) -> None:
        """Re-read the fabric set from the node after an unpair. Never raises.

        The §5 ``fabrics_changed`` that follows a removal is asynchronous, and
        the picker is built from the cache it updates — so without this a user
        who unpairs and immediately re-opens the dialog is offered the ecosystem
        they just removed. Blocking is fine HERE (a menu Execute already blocked
        on the removal itself); it is not fine in the picker callback, which runs
        on the UI thread while the dialog opens.
        """
        bridge = self.export_bridge
        if bridge is None:
            return
        try:
            pairing = self.runtime.submit(client.get_pairing()).result(timeout=PAIRING_READ_TIMEOUT)
            bridge.note_fabrics(pairing.fabrics)
        except Exception as exc:  # noqa: BLE001
            # The removal itself already succeeded or was already true; failing
            # to re-read the list afterwards is not worth reporting as a failure.
            self.logger.debug("Matter bridge: could not refresh the ecosystem list (%s)", exc)

    def _is_last_fabric(self, fabric_index: int) -> bool:
        """Whether removing ``fabric_index`` empties the fabric set.

        Read BEFORE the removal, from the set the bridge last reported: the §5
        ``fabrics_changed`` that follows arrives asynchronously, so asking
        afterwards races it. Unknown (nothing reported yet) reads as False —
        the message it selects is only the difference between two warnings.
        """
        bridge = self.export_bridge
        fabrics = bridge.fabrics if bridge is not None else None
        if not fabrics:
            return False
        return [f.fabric_index for f in fabrics] == [fabric_index]

    # ------------------------------------------------------------------
    # The QR page (IWS hidden action — PRD §6 "display mechanism")
    # ------------------------------------------------------------------
    def http_pairing(self, action, dev=None, caller_waiting_for_result=None):  # noqa: N802, ARG002
        """Serve the pairing page. GET only; authenticated by IWS before we run.

        This is the *only* handler here that returns HTML rather than JSON, and
        it exists because the one thing the event log cannot carry is a QR code.
        """
        method, _path_args, _query = self._parse_request(action)
        if method.upper() != "GET":
            return self._reply(405, {"error": "method_not_allowed"})
        reply = indigo.Dict()
        reply["status"] = 200
        reply["headers"] = indigo.Dict({"Content-Type": "text/html; charset=utf-8"})
        reply["content"] = self._pairing_page()
        return reply

    def _pairing_page(self) -> str:
        """Build the pairing page's HTML from a live ``get_pairing``.

        **No QR is generated here, and that is a deliberate choice.** Rendering
        one needs either a Python dependency (Indigo's framework Python has no
        image stack and this plugin ships none) or a hand-written JS encoder —
        a few hundred lines of Reed-Solomon and bit-masking whose failure mode is
        a plausible-looking square that no phone can read. Neither is worth it
        for a code that Apple Home, Alexa and Google all accept *typed in*: the
        page therefore shows the manual code at a size you can read across a
        room, the raw ``MT:`` payload for copying, and a link to the CHIP
        project's own QR viewer for anyone who wants to scan. The tradeoff is
        recorded in ``docs/HANDOVER.md`` rather than only in this docstring.
        """
        client = self.export_bridge.client if self.export_bridge is not None else None
        if client is None or not client.connected:
            return _pairing_html(None, "The plugin is not connected to the Matter bridge node. "
                                       "Export at least one device, then reload this page.")
        try:
            pairing = self.runtime.submit(client.get_pairing()).result(timeout=PAIRING_READ_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
            return _pairing_html(None, f"Could not read the bridge node's pairing state: {exc}")
        if not pairing.manual_pairing_code:
            return _pairing_html(
                pairing,
                "No pairing window is open, so there is no code to show. Open one with "
                "Plugins ▸ Matter ▸ Pair Matter Bridge… in Indigo.")
        return _pairing_html(pairing, "")

    def _resolve_storage_path(self) -> str:
        """Storage dir path in BOTH managed and manual modes.

        In managed mode ``self.server_process`` already knows it. In manual mode
        we construct a throwaway ``ServerProcess`` purely to read ``storage_path``
        — its ``__init__`` writes no plist and runs no launchctl, so this is a
        side-effect-free path lookup.
        """
        if self.server_process is not None:
            return self.server_process.storage_path
        return ServerProcess(self._server_prefs(), self.logger).storage_path

    def _bridge_storage_path(self) -> str:
        """The **export** bridge node's storage dir — sibling of the controller's.

        Derived rather than read from a pref, and derived by the module that also
        hands it to the agent as ``--storage-path`` (E7), so the directory this
        backs up and the directory the node actually writes cannot disagree. The
        path is the PRD §4.3 default (``…/com.simons-plugins.indigo-matter/
        bridge-node``), which is also ``bridge-node/src/config.ts``'s
        ``DEFAULT_STORAGE_PATH``.
        """
        return bridge_agent.bridge_storage_path(self._resolve_storage_path())

    def _bridge_restore_control(self) -> Optional["bridge_agent.BridgeProcess"]:
        """The bridge's ``stop()``/``start()`` seam for :func:`fabric_backup.restore_backup`.

        ``stop()``/``start()``, NOT ``uninstall()``: restore wants the node
        back in exactly its prior lifecycle state, plist included, so that a
        reboot afterwards behaves exactly as it would have before the
        restore. ``uninstall()`` is ``menuStopBridgeNode``'s primitive
        (:meth:`_stop_bridge_agent`) and answers "make sure a reboot cannot
        bring this back" — the wrong question here, and why THIS path leaves
        the XAC1 latch alone (see the call site in ``menuRestoreFabricBackup``).

        Built from CURRENT prefs when no agent object exists yet, exactly as
        ``menuStopBridgeNode`` does — this must work in a session that never
        exported anything. Construction writes nothing and runs no launchctl.
        """
        if self.bridge_process is not None:
            return self.bridge_process
        try:
            self.bridge_process = bridge_agent.BridgeProcess(dict(self.pluginPrefs), self.logger)
        except Exception as exc:  # pylint: disable=broad-except
            self.logger.warning(
                "Matter bridge: could not build a control for the bridge node (%s), so any "
                "bridge files in this backup will be reported and skipped. The controller "
                "fabric restores normally.", exc)
            return None
        return self.bridge_process

    @staticmethod
    def _human_size(num_bytes: int) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    def menuExportFabricBackup(self):  # noqa: N802
        # menuItem has no ConfigUI/valuesDict, so outcome can only surface via the
        # log — make both success and failure unmistakable there. create_backup
        # already prunes (no duplicate prune here) and validates its own output.
        storage_path = None
        try:
            storage_path = self._resolve_storage_path()
            archive = fabric_backup.create_backup(
                storage_path, now=datetime.now(timezone.utc), logger=self.logger,
                # PRD-indigo-matter-export §4.3: the bridge node's storage is
                # backed up alongside the controller's. Losing it costs every
                # ecosystem pairing AND every exported accessory's identity.
                bridge_storage_path=self._bridge_storage_path(),
            )
            size = self._human_size(os.path.getsize(archive))
            self.logger.info(
                "Fabric backup complete: %s (%s). This is a best-effort live snapshot — "
                "matter-server was NOT stopped. Backups live in %s.",
                archive, size, fabric_backup.backups_dir_for(storage_path),
            )
        except FileNotFoundError as exc:
            # storage dir missing or empty — there is no fabric to back up.
            self.logger.error(
                "Fabric backup FAILED — no fabric to back up, nothing was written: %s", exc,
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Fabric backup FAILED — nothing was written: %s", exc)
            self.logger.exception(exc)

    def getFabricBackups(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002
        """List-callback populating the restore picker (newest first)."""
        try:
            storage_path = self._resolve_storage_path()
        except Exception as exc:  # noqa: BLE001
            self.logger.exception(exc)
            return []
        options = []
        for entry in fabric_backup.list_backups(storage_path):
            when = datetime.fromtimestamp(entry["mtime"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            label = f"{entry['filename']} — {self._human_size(entry['size_bytes'])} — {when}"
            options.append((entry["path"], label))
        return options

    def menuRestoreFabricBackup(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        errors = indigo.Dict()
        # Restore must stop/start matter-server, which the plugin can only do in
        # managed mode. Externally-managed (run.sh / manual) servers must be
        # stopped by the user by hand.
        if self.server_process is None:
            msg = ("LaunchAgent management is off — the plugin cannot stop matter-server. "
                   "Stop matter-server yourself, unzip the chosen backup over the storage dir, "
                   "then restart it. Refusing to restore automatically.")
            self.logger.warning(msg)
            errors["backup"] = "Turn on 'Manage LaunchAgent', or restore by hand (see log)."
            return (False, valuesDict, errors)

        selected = valuesDict.get("backup", "")
        if not selected:
            errors["backup"] = "Select a backup to restore."
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm — restore replaces the current fabric."
            return (False, valuesDict, errors)

        try:
            storage_path = self._resolve_storage_path()
            # The bridge control/path are passed in, but the XAC1 latch (which
            # session started the bridge agent) is NEVER touched here in either
            # direction: alive+latched -> stop/start -> unchanged, correct;
            # alive+unlatched (a prior session's agent) -> unchanged, because
            # setting it would arm a future bootout of an agent this session
            # never started; stopped-by-us + restart-failed -> the latch stays
            # SET, because clearing it would let the next empty-export
            # transition skip uninstalling a RunAtLoad plist that would
            # otherwise resurrect the bridge at the next login (XAC1/XG5).
            # restore_backup uses stop()/start(), never uninstall(), so the
            # plist survives and the latch's claim stays true throughout.
            result = fabric_backup.restore_backup(
                selected, storage_path, self.server_process,
                now=datetime.now(timezone.utc), logger=self.logger,
                bridge_storage_path=self._bridge_storage_path(),
                bridge_control=self._bridge_restore_control(),
            )
            # restore_backup only returns on success: the server was stopped, the
            # fabric was swapped, the restored dir is non-empty, and start()
            # returned True. Be honest — matter-server is RESTARTING, the node
            # count is not yet known; point the user at the real signal instead of
            # logging a likely-stale count and pretending it is confirmation.
            self.logger.info(
                "Fabric restored from %s; previous fabric preserved at %s. matter-server is "
                "restarting — watch the log for 'reconciled N node(s)' to confirm the devices "
                "came back.",
                result["restored_from"], result["moved_aside_to"],
            )
            if result["bridge_restored"]:
                if result["bridge_started"] is False:
                    self.logger.error(
                        "The controller fabric restored, but the Matter bridge node did not "
                        "come back up. %s", self._bridge_agent_diagnosis() or
                        "Check the bridge node's error log.")
                else:
                    # There may have been no pre-existing bridge dir to preserve —
                    # say nothing rather than "preserved at None".
                    preserved = (
                        f" (previous copy preserved at {result['bridge_moved_aside_to']})"
                        if result["bridge_moved_aside_to"] else "")
                    self.logger.info(
                        "The Matter bridge node's storage was restored too%s%s. It now holds "
                        "the accessory identities and endpoint numbers as of that backup — if "
                        "a paired ecosystem has changed since, the bridge REPORTS endpoint-map "
                        "drift in the log and renumbers nothing.", preserved,
                        " and the node has been restarted" if result["bridge_started"] else "")
            return (True, valuesDict)
        except Exception as exc:  # noqa: BLE001
            # restore_backup rolled back and preserved the original fabric (or
            # aborted before touching it). Surface the failure in the UI dialog —
            # never report success when the underlying op failed.
            self.logger.error("Fabric restore FAILED: %s", exc)
            self.logger.exception(exc)
            errors["backup"] = "Restore failed — see the log. Your existing fabric was preserved."
            return (False, valuesDict, errors)
