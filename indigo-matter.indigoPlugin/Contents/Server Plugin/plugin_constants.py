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
