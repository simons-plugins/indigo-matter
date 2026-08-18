"""Module-level constants and pure prefs helpers shared across ``plugin.py`` and
the four mixin modules it composes. See issue #146.
"""
from __future__ import annotations

PLUGIN_NAME = "indigo-matter"
COMMAND_TIMEOUT = 5.0
DECOMMISSION_TIMEOUT = 15.0

#: Seconds between periodic "does our matter-server still own its port?" checks
#: (issue #182). Deliberately far coarser than the ~15s health tick: the check
#: shells out to `lsof` and `ps`, and the fault it looks for takes days to matter,
#: not seconds. Five minutes bounds the blind spot to minutes instead of "until
#: somebody reloads the plugin", which is what let the original incident run for
#: four days, while costing 12 subprocess pairs an hour.
PORT_CONFLICT_CHECK_INTERVAL = 300.0

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
#: Menu id of the re-adopt dialog (issue #219). Seeded for the same reason —
#: it has two pickers whose Execute button hands an accessory identity to a
#: different Indigo device.
MENU_READOPT_EXPORT = "readoptExport"
#: Menu id of the migrate dialog (issue #246). Same reasoning as the re-adopt
#: dialog above — two pickers whose Execute button moves a LIVE accessory
#: identity to a different Indigo device.
MENU_MIGRATE_EXPORT = "migrateExport"
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

#: pluginPrefs key holding the settable-attribute report's "already told you
#: about this one" log (issue #191) — one JSON string keyed by node id, the same
#: shape ``matterExports`` uses. Named here because ``plugin.startup`` loads it
#: and ``DiagnosticsMenuMixin`` saves it.
SURVEY_LOG_PREF = "matterSettingsSurveyed"
#: pluginPrefs flag: the one-shot notice that the ``onTime`` state was withdrawn
#: in 2026.12.0 has been logged (issue #197). Removing a state silently breaks
#: any trigger bound to it — the asymmetry ADR-0003 records — so the removal
#: announces itself once rather than leaving a user to find a dead trigger. A
#: flag rather than a version comparison: the plugin has no record of the
#: version it upgraded FROM, and "have I said this?" is the actual question.
ON_TIME_RETIRED_PREF = "matterOnTimeStateRetired"
#: pluginPrefs key holding the node-device tombstone set (issue #204, ADR-0008)
#: — one JSON array of node ids whose synthetic matterNode device the user
#: deliberately deleted, so create_devices does not recreate it on the next
#: reconcile. Same prefs discipline as SURVEY_LOG_PREF/matterExports: plugin.
#: startup loads it, ServerMenuMixin saves it (the "Recreate Matter node
#: devices…" menu item is where a tombstone is cleared).
NODE_TOMBSTONES_PREF = "matterNodeDeviceTombstones"
#: How long the "Recreate Matter node devices…" menu action may block the
#: Indigo UI thread on a full reconcile (get_nodes + get_node per node, not
#: just one round trip like SURVEY_READ_TIMEOUT) — generous for the same
#: reason WINDOW_OPEN_TIMEOUT/UNPAIR_TIMEOUT are: real work happens behind it.
RECONCILE_TIMEOUT = 30.0
#: How long a diagnostic menu action may block the Indigo UI thread on
#: ``get_node``. That call is answered from matter-server's own cache, so it is
#: a socket round trip rather than a device round trip — generous, but nothing
#: like the device timeouts, because no device is involved.
SURVEY_READ_TIMEOUT = 20.0
#: How long "Share a Matter device with another ecosystem…" (issue #210) may
#: block the Indigo UI thread on ``open_commissioning_window`` — the OUTER of
#: two nested deadlines, the inner being ``matter_client.SHARE_WINDOW_RPC_TIMEOUT``
#: (45.0). PairingMenuMixin's WINDOW_OPEN_TIMEOUT (45.0) covers the SAME
#: operation against the export bridge's own, LOCAL matter.js node; this one
#: crosses the socket to matter-server AND a Thread radio round trip on top,
#: so it gets its own, longer number rather than reusing that one.
#: INVARIANT: this must stay strictly ABOVE SHARE_WINDOW_RPC_TIMEOUT. On
#: Python 3.11+ ``concurrent.futures.TimeoutError`` IS ``asyncio.TimeoutError``
#: IS the builtin ``TimeoutError`` (requires-python >= 3.11), so both
#: deadlines raise the identical exception type either way — the ordering
#: does not change WHAT is raised. It changes what happens FIRST: with the
#: inner deadline strictly below, the RPC layer's own ``asyncio.wait_for``
#: fires first and gets to complete its own cleanup of the pending frame
#: normally — including, since issue #210's late-response wiring, handing
#: any answer that arrives after this to the request's ``ShareWindowRequest``
#: context — before this outer ``.result(timeout=...)`` would ever have
#: given up on its own. Tied or reversed, the outer timeout could fire while
#: the inner wait is still the one genuinely pending, which is not a
#: different symptom (a mismatched deadline is not visible on the exception
#: it raises) but does lose the ordering guarantee this book-keeping relies
#: on. Tested in tests/test_share_node_menu.py.
SHARE_WINDOW_TIMEOUT = 60.0
#: The "every device" row in the diagnostic node pickers, and the "no filter"
#: rows in the explorer's endpoint/cluster pickers. Shares NO_SELECTION_ID's
#: reasoning: never "", which Indigo rejects outright.
ALL_OPTION_ID = "all"
#: How long "Re-adopt a Matter accessory…" (issue #219) may block the Indigo
#: UI thread on `list_orphans` — for both its pickers and its own step-3
#: re-verification (PR5 design §4.4). A plain, local read like PAIRING_READ_TIMEOUT's
#: `get_pairing`, not one of the "real work happens behind it" deadlines
#: above, so it gets the same number rather than a longer one.
READOPT_ORPHANS_TIMEOUT = 15.0


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
