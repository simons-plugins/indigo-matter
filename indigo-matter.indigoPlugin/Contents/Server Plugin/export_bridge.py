"""The outbound export engine — Indigo device changes ⇄ the bridge node.

``plugin.py`` stays lifecycle glue: it owns the Indigo callbacks and nothing
else. Everything those callbacks *mean* for export lives here — when the
:class:`bridge_client.BridgeClient` exists at all, what the desired endpoint set
is, how a device change becomes a ``set_state``, and how an ecosystem command
becomes an ``indigo.*`` call. A bare ``§N`` below is ``docs/BRIDGE_PROTOCOL.md``.

Four disciplines worth knowing before editing:

* **The client exists only while something is exported (XG5).** A fresh install
  is inert: no allow-list, no socket, no log noise. The dialog transitions
  empty↔non-empty mid-session, so :meth:`ExportBridge.exports_changed` starts
  and stops the client rather than the plugin's ``startup`` deciding once. In E3
  the bridge *node* is started by hand — launchd is E7 — so "not running" is the
  normal case and is reported once per streak, not once per retry.

* **The store is not the guard; :func:`export_catalog.classify` is.** The
  endpoint provider re-classifies every entry on every attach (the E2 handover's
  standing requirement). An allow-list entry is a user *declaration*, made at
  some point in the past against a device that has since been deleted, disabled,
  reconfigured, or taken over by another plugin. Sending the node a spec built
  from a stale declaration is how an accessory ends up controlling the wrong
  thing.

* **A role the plugin cannot bridge is skipped, loudly, not sent.** E4 made
  ``export_handlers`` total over the v1 §4.2 enum, so this no longer fires for
  anything a user can select — but it is the guard for a role a *future*
  protocol version adds, and for an allow-list blob restored from a newer
  install. An unknown role fails the *whole* ``attach`` on the node side (E3a),
  so one such export would silently un-export every working one.
  Skip-with-warning keeps the blast radius at one device, and the count is
  surfaced in the dialog's status line.

* **Nothing here may block Indigo's thread.** ``deviceUpdated`` runs on Indigo's
  callback thread for *every* device on the server. State pushes are submitted
  to the loop and never awaited (§3.4); the result is logged by a done-callback
  so a failed push is never silent.

* **...and nothing here may block the loop either.** The reverse direction has
  the same rule and one fewer guarantee: whether ``indigo.*`` device *commands*
  are safe from a non-Indigo thread is unverified from the docs. Bulk Indigo IPC
  is kept off the loop (:meth:`ExportBridge.endpoint_specs` runs in an executor)
  and since E5 so is the inbound direction: :meth:`ExportBridge.on_command`
  hands the whole ``indigo.*`` call to a **single-threaded** executor. One
  worker, not a pool, because §4.2 commands are per-device state changes and a
  pool would let two of them for the same accessory land out of order — a
  `setLevel 20` overtaking a `setLevel 80` leaves the lamp permanently wrong.
  One worker gives global FIFO, which trivially contains per-device FIFO, and
  costs nothing: these are human-paced button presses, not a data feed.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import bridge_protocol
import export_catalog
import export_handlers
from bridge_client import TERMINAL_ATTACH_ERRORS, BridgeClient, attach_timeout_for
from bridge_protocol import EndpointSpec

#: How many consecutive watchdog ticks a disconnected bridge client tolerates
#: before the log escalates from debug to a single warning. Ticks are ~15s, so
#: this is ~1 minute — the same shape (and the same reasoning) as the
#: matter-server counter in ``plugin._health_tick``.
DISCONNECT_WARN_TICKS = 4

#: Deadline for ONE §5 command dispatch, in seconds. Generous: an Indigo device
#: command is a synchronous IPC round trip that may itself be waiting on a radio,
#: and a bridge that gave up early would report a failure the house then went and
#: performed anyway. Short enough that a genuinely wedged worker is named while
#: the user is still looking at the log.
COMMAND_TIMEOUT = 30.0

#: How many dispatches may be outstanding before the watchdog says so. The
#: worker is single-threaded, so anything above a couple means work is stacking
#: up behind something that is not returning.
COMMAND_QUEUE_WARN = 3

#: Pref key holding the size of an un-export whose ``attach`` never landed
#: (XAC7). Absent or 0 means there is nothing owed to the node. An int rather
#: than a bool because the §3.1 attach that discharges it has to size its own
#: deadline over the removals — see :func:`bridge_client.attach_timeout_for`.
PREF_PENDING_REPLACE_ALL = "matterExportPendingReplaceAll"

#: Halt reasons a bridge reinstall/restart genuinely fixes (issue #154). Both
#: name the SAME failure — the plugin and the node's protocol versions disagree
#: — reached through two different paths that end up latching two different
#: strings into :attr:`bridge_client.BridgeClient.halted_reason`:
#:
#: * the handshake ``hello`` (``bridge_client._handshake``) compares protocol
#:   versions before an attach is ever sent and raises ``ClientHalted`` with the
#:   literal ``"version_skew"`` (bridge_client.py:258-262);
#: * an attach the node refuses with ``bridge_protocol.ERR_VERSION_MISMATCH``
#:   (its own string, ``"version_mismatch"``) is one of
#:   :data:`bridge_client.HALTING_ATTACH_ERRORS`, so
#:   ``_handle_attach_refused`` (bridge_client.py:403) halts with
#:   ``reason=code`` — i.e. the reason string is ``"version_mismatch"`` itself.
#:
#: ``bridge_client.TERMINAL_ATTACH_ERRORS[ERR_VERSION_MISMATCH]``'s own remedy
#: text is "restart the bridge agent" — exactly what *Install/update the Matter
#: bridge* does — so both reason strings are revived here. The other member of
#: ``HALTING_ATTACH_ERRORS``, ``mass_removal_refused``, is deliberately NOT in
#: this set: its remedy is about the allow-list, not the node process, and
#: reviving it after an install would silently rebuild a client that attaches
#: into the exact same refusal a reinstall cannot touch.
REVIVABLE_HALT_REASONS = frozenset({"version_skew", bridge_protocol.ERR_VERSION_MISMATCH})

#: PRD §5.5's wholesale export switch. **Absent means ON**, because it arrived in
#: E6 and every install that predates it already has a working allow-list; a
#: missing key reading as "off" would silently un-run everyone's export on
#: upgrade. Explicitly off means: build no client, start no agent, and — the
#: part that is easy to get wrong — **un-export nothing**. Turning a switch off
#: is not the same statement as emptying the allow-list (PRD §7), and answering
#: it with the §3.1 mass removal would delete every accessory from every paired
#: ecosystem, taking their names, rooms and automations with them. Off means the
#: accessories stop being *updated* and go unreachable, which is recoverable by
#: ticking the box again.
PREF_EXPORT_ENABLED = "exportEnabled"


class ExportBridge:
    """Owns the bridge client and everything the Indigo callbacks mean for it.

    :param store: the :class:`export_store.ExportStore` allow-list.
    :param runtime: the :class:`async_runtime.AsyncRuntime` the client runs on.
    :param logger: the plugin logger.
    :param prefs_getter: callable returning the *current* prefs mapping — a
        callable, not the mapping, for the same reason ``ExportStore`` takes one
        (Indigo rebinds ``pluginPrefs`` when a config dialog is saved).
    :param plugin_version: reported to the node in ``attach`` (§3.1).
    :param plugin_id: this plugin's bundle id — the catalog's loop guard.
    :param device_getter: ``id → indigo device or None``. Injected so this
        module unit-tests without the Indigo runtime.
    :param client_factory: builds the :class:`BridgeClient`; injected for tests.
    :param save_prefs: flush callable for the prefs mapping, so the pending
        un-export flag survives a crash. Injected for the same reason
        ``ExportStore`` takes one.
    :param executor_factory: builds the command executor; injected so tests can
        run dispatches inline instead of on a thread.
    :param agent_start: called on the empty→non-empty transition, before the
        client is built — the E7 LaunchAgent seam (XG5/XAC1). Blocking (launchctl
        and file I/O), so it runs on whichever Indigo thread changed the
        allow-list, never on the loop.
    :param agent_stop: called after an un-export has actually landed and the
        socket is closed. Awaited off the loop by the un-export coroutine.
    :param agent_diagnose: called when the node stops answering; returns a
        sentence naming what the agent found (and may revive a dead job), or
        ``None`` when it has nothing to add. Purely advisory — export degrades,
        the inbound controller is untouched.
    """

    # The seams ARE the API, exactly as BridgeClient's callbacks are.
    # pylint: disable=too-many-arguments
    def __init__(self, store, runtime, logger, prefs_getter: Callable[[], dict], *,
                 plugin_version: str = "unknown",
                 plugin_id: str = export_catalog.DEFAULT_PLUGIN_ID,
                 device_getter: Optional[Callable[[int], Any]] = None,
                 client_factory: Optional[Callable[..., BridgeClient]] = None,
                 save_prefs: Optional[Callable[[], None]] = None,
                 executor_factory: Optional[Callable[[], Any]] = None,
                 agent_start: Optional[Callable[[], None]] = None,
                 agent_stop: Optional[Callable[[], None]] = None,
                 agent_diagnose: Optional[Callable[[], Optional[str]]] = None) -> None:
        self._store = store
        self._runtime = runtime
        self._logger = logger
        self._prefs_getter = prefs_getter
        self._plugin_version = plugin_version
        self._plugin_id = plugin_id
        self._device_getter = device_getter or (lambda dev_id: _indigo_device(dev_id, logger))
        self._client_factory = client_factory or BridgeClient
        self._save_prefs = save_prefs
        self._executor_factory = executor_factory or _command_executor
        self._agent_start = agent_start
        self._agent_stop = agent_stop
        self._agent_diagnose = agent_diagnose
        #: Whether THIS plugin session has brought the bridge agent up. Gates
        #: the stop (see :meth:`_stop_agent`) so a session that never started it
        #: never boots one out.
        self._agent_started = False

        #: The live client, or ``None`` while nothing is exported (XG5).
        self.client: Optional[BridgeClient] = None
        #: Last reason each device was skipped by the provider, so a permanent
        #: skip (an unbridgeable role) logs once, not on every reconnect.
        self._skipped: dict[int, str] = {}
        #: The reason set behind the last "NONE of them can be bridged" warning,
        #: so that state — which since #141 costs every accessory in every paired
        #: ecosystem — is announced once per cause rather than per reconnect.
        self._wholly_unbridgeable: str = ""
        #: Consecutive watchdog ticks seen disconnected.
        self._disconnect_ticks = 0
        #: Set once the "the node is not running" line has been said for this
        #: outage, so a manually-started-later node does not fill the log first.
        self._unreachable_reported = False
        #: Same latch shape, one per condition that persists until a human or a
        #: reconnect changes it. Each is cleared by :meth:`_on_attached`, which
        #: is the only event that means "whatever that was, it is over".
        #: Without them the watchdog says the same sentence every 15s forever,
        #: and a halted bridge says it again for every device change in the
        #: house — burying the one line that would explain the outage.
        self._halted_reported = False
        self._recovery_reported = False
        #: The last attach-refusal code reported, so a *transient* refusal —
        #: which reconnects on the normal backoff and refuses again — is said
        #: once per streak rather than once per cycle.
        self._refusal_reported: Optional[str] = None
        #: Device ids whose ``device_updated`` is currently failing, so a stuck
        #: device does not write a traceback per state change.
        self._update_failed: set[int] = set()
        #: ``device id → the §4.2 keys it has stopped reporting``. Same latch
        #: shape, because the condition persists: a dead battery does not
        #: un-die, and every subsequent change of that device would say it again.
        self._stopped_keys: dict[int, frozenset] = {}
        #: ``device id → the last no-op reason its role gave``, so a command an
        #: ecosystem repeats (a user pressing stop three times) says it once.
        self._no_op_reported: dict[int, str] = {}
        #: The allow-list size as of the last :meth:`exports_changed`. The
        #: un-export path needs it: by the time it runs the store is already
        #: empty, and its attach deadline has to cover REMOVING that many
        #: endpoints (see :func:`bridge_client.attach_timeout_for`).
        self._last_export_count = len(store)
        #: ``device id → the §4.2 states last actually PUSHED to the node``.
        #: What ``device_updated`` diffs against, instead of the previous Indigo
        #: reading — see :meth:`device_updated`. Bounded by the allow-list:
        #: entries are seeded when a spec is built and dropped when the export
        #: is removed or skipped.
        self._pushed: dict[int, dict] = {}
        #: True while :meth:`_replace_all_then_stop`'s coroutine is in flight.
        #: :meth:`start` must not build a second client underneath it.
        self._un_exporting = False
        #: Set when :meth:`start` was called during an un-export, so the client
        #: is created once that finishes rather than never.
        self._start_after_un_export = False
        #: The single worker §5 commands are dispatched on; built on first use.
        self._executor: Optional[Any] = None
        #: Set once :meth:`stop` has run, so a coroutine still queued on the
        #: loop cannot rebuild the executor we just shut down and leave a live
        #: worker thread behind a "stopped" bridge.
        self._stopped = False
        #: §5 dispatches submitted / completed. The gap is the queue depth, and
        #: it is the only observable of a wedged worker (see :meth:`health_tick`).
        self._submitted = 0
        self._completed = 0
        #: Set while the queue-depth warning has been said for this streak.
        self._queue_warned = False
        #: The last §4.3 ``warnings`` set the node reported, so a standing
        #: persistence failure is said once rather than once per attach.
        self._node_warnings: frozenset = frozenset()
        #: The last drift set reported, for the same reason — see
        #: :meth:`_on_drift_detected`.
        self._drift_reported: frozenset = frozenset()
        #: ISO 8601 expiry of the commissioning window the pairing menu opened,
        #: or ``None``. Held here rather than re-read from the node because the
        #: PRD §5.5 config readout is a dialog-open, and a dialog must not block
        #: on a WS round trip. Cleared by the §5 ``window_closed`` event, which
        #: fires on expiry AND on a commissioner completing.
        self.window_expires_at: Optional[str] = None
        #: The fabric set as of the last §5 ``fabrics_changed`` (or attach), for
        #: the same readout. ``None`` means "nothing has told us yet", which the
        #: readout must not render as "no ecosystems are paired".
        self.fabrics: Optional[list] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True while a client exists (whether or not it is connected)."""
        return self.client is not None

    @property
    def enabled(self) -> bool:
        """PRD §5.5's wholesale export switch, as the config readout reads it."""
        return self._export_enabled()

    def start(self) -> None:
        """Create and run the client. Idempotent.

        **Refuses while an un-export is in flight.** ``_replace_all_then_stop``
        sets ``self.client = None`` the instant it fires, so a user who empties
        the allow-list and immediately re-adds a device would otherwise build a
        second client on top of a socket the first one is still using to say
        "remove everything" — and then race it. The un-export's ``finally``
        picks the request back up, so the start is deferred, never dropped.
        """
        if self._un_exporting:
            self._start_after_un_export = True
            self._logger.debug(
                "Matter bridge: start deferred until the in-flight un-export finishes")
            return
        if self.client is not None:
            return
        if not self._export_enabled():
            # Not an error and not a latch: the user has turned export off in
            # Configure…, and every caller of start() is a path that would
            # ordinarily bring it up. Saying so once per attempt at debug is
            # enough — the config dialog's own readout is where this belongs.
            self._logger.debug("Matter bridge: export is switched off in Configure…; "
                               "not connecting to the bridge node")
            return
        # The agent BEFORE the client, and deliberately not conditional on it
        # having worked: a bridge that will not start is an export outage, and
        # the client's own unreachable path is what reports it with the node's
        # error log attached. Refusing to build the client here would replace one
        # diagnosis with none.
        self._ensure_agent()
        self.client = self._client_factory(
            self._logger, self._prefs_getter(),
            plugin_version=self._plugin_version,
            endpoint_provider=self.endpoint_specs,
            replace_all_provider=self._pending_replace_all,
            export_count_provider=self._declared_export_count,
            on_command=self.on_command,
            on_attached=self._on_attached,
            on_attach_refused=self._on_attach_refused,
            on_version_skew=self._on_version_skew,
            on_drift_detected=self._on_drift_detected,
            on_fabrics_changed=self._on_fabrics_changed,
            on_commissioned=self._on_commissioned,
            on_decommissioned=self._on_decommissioned,
            on_window_closed=self._on_window_closed,
            on_repeated_failure=self._on_unreachable,
        )
        self._unreachable_reported = False
        self._disconnect_ticks = 0
        # A new client is a new outage history: the halt/recovery/refusal
        # latches must not carry over, or a client swapped in by
        # revive_after_install (#154) that halts AGAIN — the reinstall did not
        # actually fix the skew — re-halts in silence, with the watchdog's
        # standing "nothing is being exported" line suppressed by the OLD
        # client's report. _on_attached also resets these on success;
        # this is the failure-path reset.
        self._halted_reported = False
        self._recovery_reported = False
        self._refusal_reported = None
        self._fire(self.client.run(), "bridge client run loop",
                   lost="nothing will be exported until the plugin is reloaded")
        self._logger.info(
            "Matter bridge: connecting to the bridge node (%d device(s) exported)",
            len(self._store))

    def stop(self, timeout: float = 4.0) -> None:
        """Close the client. Idempotent; never raises at shutdown."""
        self._stopped = True
        client, self.client = self.client, None
        executor, self._executor = self._executor, None
        if executor is not None:
            dropped = max(0, self._submitted - self._completed)
            if dropped:
                # These reached the plugin and never reached Indigo. The
                # ecosystem applied every one of them optimistically, so this
                # is the count of tiles now showing something the house is not
                # doing — and nothing retries a §5 command.
                self._logger.warning(
                    "Matter bridge: shutting down with %d ecosystem command(s) still queued or "
                    "in flight — they will NOT be applied, and paired ecosystems already show "
                    "them as done.", dropped)
            # Not `wait=True`: a dispatch blocked on a wedged IndigoServer would
            # hold plugin shutdown open, and the command it is running has
            # already been reported to the ecosystem either way.
            # `cancel_futures` so the ones that have not STARTED are dropped
            # rather than run against a plugin that is already tearing down.
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                # Injected doubles need not take the 3.9+ keyword.
                executor.shutdown(wait=False)
        if client is None:
            return
        try:
            self._runtime.submit(client.close()).result(timeout=timeout)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("bridge client close error: %s", exc)

    def retry_now(self) -> bool:
        """Cut the client's reconnect backoff short (issue #135).

        Pokes the live client, if there is one, and returns whether the poke
        actually landed. ``False`` while nothing is exported (XG5, no client to
        poke) or while the client itself declines (not running, closing, or
        halted — see :meth:`ws_json_client.WsJsonClient.retry_now`).
        Safe to call from the ``threading.Thread`` the npm install runs on.

        **Local capture, not ``self.client`` re-read.** :meth:`stop` and
        :meth:`_stop_soon` can null ``self.client`` from the loop thread at any
        point, including between a None-check and a call made against it — this
        runs on the install thread, so that is a genuine race, not a
        theoretical one. Capturing once and working from the local avoids an
        ``AttributeError`` (`.retry_now()` on `None`) that would otherwise reach
        the install thread and print a wrong "install failed" message for a
        poke that simply lost a race with an unrelated shutdown.
        """
        client = self.client
        if client is None:
            return False
        return client.retry_now()

    def revive_after_install(self) -> bool:
        """Replace a client halted on version skew, after a bridge reinstall (#154).

        ``TERMINAL_ATTACH_ERRORS[ERR_VERSION_MISMATCH]``'s remedy tells the user
        to restart the bridge agent — exactly what *Install/update the Matter
        bridge* just did — but a version-skew halt is fail-closed
        (``bridge_client.ClientHalted``) and nothing else in this engine revives
        a halted client: :meth:`ws_json_client.WsJsonClient.resume` only clears
        the latch and its own docstring says the caller must start a fresh
        ``run()`` task, and :meth:`start` no-ops while ``self.client is not
        None`` regardless of its state. So the prescribed remedy used to leave
        the user stuck until a full plugin reload — this is the seam that fixes
        that.

        Returns ``False`` (no-op) unless the live client is halted for a reason
        in :data:`REVIVABLE_HALT_REASONS`. When it is: drop the halted client,
        close its socket, and call :meth:`start` — the same path the
        empty→non-empty transition uses, which rebuilds a fresh client with
        every callback wired. ``_un_exporting``/``_export_enabled`` need no
        handling here; :meth:`start` already accounts for both.

        **Thread-safety.** Called from the ``threading.Thread`` the npm install
        runs on, exactly like :meth:`retry_now`. The guard is read from a LOCAL
        capture rather than a second ``self.client`` lookup, for the same
        reason :meth:`retry_now` gives: :meth:`stop`/:meth:`_stop_soon` can null
        ``self.client`` from the loop thread at any point. That still leaves one
        TOCTOU, same shape as :meth:`retry_now`'s: between the guard read below
        and the capture-and-null two lines later, a concurrent :meth:`stop`
        could take the client first. If it wins, the drop-and-close here is
        harmless (closing an already-closing client is idempotent) and the
        :meth:`start` that follows is the same no-op :meth:`exports_changed`
        already tolerates for a stop landing mid-transition.
        """
        client = self.client
        if client is None or not client.halted or client.halted_reason not in REVIVABLE_HALT_REASONS:
            return False
        # Null exactly the object the guard checked — NOT a fresh self.client
        # read: a stop() (or stop+start) racing in between would make a
        # re-read hand back None (the log line below would raise on the
        # install thread) or a healthy successor this must not drop.
        if self.client is client:
            self.client = None
        self._logger.info(
            "Matter bridge: the bridge node was reinstalled/updated — replacing the halted "
            "connection (%s) with a fresh one", client.halted_reason)
        try:
            self._runtime.submit(client.close()).result(timeout=4.0)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("bridge client close error during revival: %s", exc)
        self.start()
        return True

    def _stop_soon(self, why: str) -> None:
        """Drop the client without waiting — safe to call ON the loop thread.

        :meth:`stop` blocks on the close future, which from the loop thread is a
        self-deadlock: the coroutine it is waiting for can only run on the
        thread that is waiting. The one caller that needs this is
        :meth:`_on_attached`, which runs inside the client's own handshake.
        """
        client, self.client = self.client, None
        if client is not None:
            self._fire(client.close(), why)

    def exports_changed(self) -> None:
        """The allow-list changed — start or stop the client to match (XG5).

        Called by every path that mutates the store. Incremental endpoint
        updates are the caller's job (:meth:`upsert`/:meth:`remove`); this is
        only the empty↔non-empty transition.
        """
        self._skipped.clear()
        count = len(self._store)
        # Captured BEFORE it is overwritten: the un-export below has to size its
        # deadline over what the node is about to remove, and the store that
        # would have told it is already empty by the time we get here.
        removing, self._last_export_count = self._last_export_count, count
        if not self._export_enabled():
            # PRD §5.5's switch, and the ONE branch that must not un-export. See
            # PREF_EXPORT_ENABLED: off is "stop updating the accessories", not
            # "delete them from every ecosystem". Drop the socket and stop the
            # agent; the endpoints, the pairings and the allow-list all stand.
            if self.client is not None:
                self._logger.info("Matter bridge: export is switched off — disconnecting from the "
                                  "bridge node. Exported accessories are LEFT paired and will show "
                                  "as unavailable until export is switched back on.")
                self.stop()
                self._stopped = False   # a config change is not a shutdown
            self._stop_agent()
            return
        if count:
            self.start()
        elif self.client is not None:
            # PRD §7 "allow-list emptied": endpoints go, pairings stay. The node
            # needs the §3.1 opt-in for that, so it is a deliberate attach
            # rather than a disconnect — and only THEN do we close.
            self._replace_all_then_stop(removing)
        elif self._pending_replace_all():
            # Nothing is exported and there is no client — but the node is still
            # holding accessories from a previous session whose un-export never
            # landed. XG5 says no client while nothing is exported; this is the
            # one exception, and it lasts exactly one successful attach.
            self._logger.info(
                "Matter bridge: reconnecting to finish an un-export that did not complete "
                "earlier (%d accessory record(s) still owed removal)",
                self._pending_replace_all())
            self.start()

    def _export_enabled(self) -> bool:
        """PRD §5.5's wholesale switch. Absent, blank or unparseable means ON.

        Only an explicit, recognisable negative turns export off. Indigo
        checkboxes arrive as real bools once saved but as ``"true"``/``"false"``
        strings from other write paths, and ``bool("false")`` is ``True`` — so a
        string is parsed strictly and anything else falls back to *enabled*.
        Failing open is the right direction here and the opposite of the
        controller's attestation flag (``server_process._pref_flag``, which fails
        closed): the harm of misreading this one is un-running a working export
        for every user whose prefs predate the key.
        """
        raw = self._prefs_getter().get(PREF_EXPORT_ENABLED)
        if raw is None:
            # Absent, or present-and-null. `.get(key, True)` covers only the
            # first, and a null is what a hand-edited .indiPref or a partial
            # restore leaves behind — reading it as "off" would un-run the
            # export of anyone whose prefs file has been through either.
            return True
        if isinstance(raw, str):
            return raw.strip().lower() not in ("false", "no", "off", "0")
        return bool(raw)

    # ------------------------------------------------------------------
    # The bridge LaunchAgent (PRD §4.2 / XG5 / XAC1)
    # ------------------------------------------------------------------
    def _ensure_agent(self) -> None:
        """Bring the bridge agent up, if there is one injected. Never raises.

        Contained here rather than at the seam's implementation because the
        caller is an Indigo callback: a launchd fault, a full disk, a missing
        node — none of them may escape into ``deviceDeleted`` or a menu handler.
        The consequence of failing is an export outage, which the client's
        unreachable path already reports with the node's own error log attached.
        """
        if self._agent_start is None:
            return
        # Latched BEFORE the attempt: a start that raised may still have written
        # a plist and bootstrapped a job, so "we have not touched launchd" would
        # be a claim we cannot make afterwards — and the stop that would clear it
        # up is the thing this flag gates.
        self._agent_started = True
        try:
            self._agent_start()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter bridge: could not start the bridge node's LaunchAgent (%s). Indigo "
                "devices and inbound Matter control are unaffected; exported accessories will "
                "not be reachable until this is fixed.", exc)
            self._logger.exception(exc)

    def _stop_agent(self) -> None:
        """Take the bridge agent down. Never raises; same containment reasoning.

        Called only once there is genuinely nothing to serve — the allow-list is
        empty AND the un-export has landed, or export has been switched off. The
        pairings live in the node's storage dir, which stopping does not touch
        (PRD §5.4: a plugin reload must never un-pair anyone).
        """
        if self._agent_stop is None or not self._agent_started:
            # ⊗ XAC1. Nothing this plugin session started, nothing it stops: a
            # bootout issued by a plugin that never brought the agent up would
            # take down a bridge the *user* started by hand, and would run on
            # every reload of an install that has never exported anything.
            return
        self._agent_started = False
        try:
            self._agent_stop()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.warning(
                "Matter bridge: could not stop the bridge node's LaunchAgent (%s). It will keep "
                "running with nothing to export, which is harmless — pairings are untouched.", exc)

    async def _stop_agent_off_loop(self) -> None:
        """:meth:`_stop_agent` from a loop-thread caller, without blocking the loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._stop_agent)

    def note_agent_stopped(self) -> None:
        """Clear the XAC1 latch: something outside this engine stopped the agent.

        The latch means "this plugin session brought the agent up and is
        therefore entitled to take it down". A user who stops the bridge from the
        menu has made that false, and leaving it set would have the next
        empty-transition bootout a job the *user* may have started again by hand
        — the exact thing the latch exists to prevent.
        """
        self._agent_started = False

    def _pending_replace_all(self) -> int:
        """**How many** endpoints an un-export still owes the node (XAC7).

        This is :class:`bridge_client.BridgeClient`'s ``replace_all_provider``,
        read on every (re)connect, and it answers the DEBT question and nothing
        else: how much un-export is there that the node has not been told about?

        **A count, not a flag, and the difference is a real outage.** The client
        feeds this answer to :func:`bridge_client.attach_timeout_for` to size the
        discharge attach's deadline, because the node paces bulk removals ~100ms
        apart (§3.3) and the discharge attach sends *nothing* while removing
        *everything* — so its own ``len(specs)`` is exactly the wrong number. A
        wrapper here used to return ``bool``, and ``int(True) == 1`` sailed
        silently through the client's ``int()``: every discharge got the 8s
        floor, an 80-accessory un-export timed out mid-reconcile, the socket was
        torn down and retried, forever, with the accessories still in every
        ecosystem — the precise regression the deadline formula exists to stop.
        It also made the client's two "un-export of %d accessory record(s)" log
        lines say 1, whatever the truth was.

        It is still truth-tested in two places (here and in the client), which a
        count supports perfectly well: 0 debts is falsy.

        **The debt is never ANDed with an empty allow-list, and that used to
        produce a permanent halt.** The reasoning was that a re-populated
        allow-list supersedes the debt — but the two are not alternatives, they
        compose. Empty the list while the node is down (debt = 5), then export
        one different device: the next attach carries ``[B]``, so the AND said
        "no intent needed", and the node's §3.1 guard saw five removals against
        zero survivors, refused with ``mass_removal_refused``, and the client
        HALTED — forever, with a message telling the user to check an allow-list
        that was not the problem. Carrying the intent whenever a debt exists
        costs nothing in the ordinary case: the node's guard only fires when the
        result would be empty, so a legitimate non-empty attach is unaffected.

        A prefs value that is not a number is 0 **and says so**: the debt is the
        only thing that makes an un-export recoverable at all, so silently
        reading it as "nothing owed" is how every exported accessory is stranded
        in every paired ecosystem with nothing left anywhere that knows it.
        """
        raw = self._prefs_getter().get(PREF_PENDING_REPLACE_ALL)
        try:
            return int(raw or 0)
        except (TypeError, ValueError) as exc:
            self._logger.warning(
                "Matter bridge: the outstanding un-export count in prefs is unreadable (%r: %s) — "
                "treating it as nothing owed. If accessories from a previous session are still "
                "showing in a paired ecosystem, re-export one device and remove it again to "
                "re-record the removal.", raw, exc)
            return 0

    def _record_pending_replace_all(self, removing: int) -> None:
        """Record an un-export attempt — **accumulating**, never clearing.

        Written to prefs rather than held in memory because the failure it
        covers is precisely the one that outlives the process: the node is down
        or the plugin is reloading, the attach never lands, and every accessory
        stays in every paired ecosystem forever with nothing left anywhere that
        knows it should not (XAC7).

        **It takes the larger of the old debt and the new one, and it never
        writes 0.** Overwriting was wrong in both directions, and the second one
        destroyed the record outright:

        * *Under-sizing.* A debt of 80 sits unpaid because the node is down. The
          user exports one device and removes it again; this was called with 1
          and the pref became 1. The node still holds 81 records, so the
          discharge attach still has to remove 81 — but its deadline was sized
          for 1, which is the 8s floor, so it times out mid-reconcile and
          retries forever. The count is what buys the deadline (§3.3 pacing),
          so it has to describe everything the node might still be holding.
        * *Erasure.* ``exports_changed`` reconnects with no exports purely to
          discharge a debt, which sets ``_last_export_count`` to 0 — so the very
          next empty↔empty transition arrived here with ``removing == 0`` and
          POPPED a debt of 5 that nothing had discharged. If the attach then
          failed, the only surviving record that five accessories should be gone
          was gone with it, and no later attach would ever carry the intent.

        Clearing is therefore a separate, deliberate act with a separate name —
        :meth:`_clear_pending_replace_all` — reached only from the two places
        that have watched an attach *carrying the intent* succeed.
        """
        owed = max(self._pending_replace_all(), int(removing))
        if owed <= 0:
            return
        try:
            self._prefs_getter()[PREF_PENDING_REPLACE_ALL] = owed
            if self._save_prefs is not None:
                self._save_prefs()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.warning(
                "Matter bridge: could not record that the un-export is outstanding (%s). "
                "If it does not complete now, exported accessories may linger.", exc)

    def _clear_pending_replace_all(self) -> None:
        """The debt has been paid — an attach that CARRIED the intent landed.

        Split from :meth:`_record_pending_replace_all` so that clearing can only
        ever be the answer to a discharge somebody watched happen, rather than a
        side effect of passing 0 to a recorder (see there for what that cost).
        """
        try:
            self._prefs_getter().pop(PREF_PENDING_REPLACE_ALL, None)
            if self._save_prefs is not None:
                self._save_prefs()
        except Exception as exc:  # pylint: disable=broad-except
            # The opposite direction from a failed record, and the message used
            # to be the same one — which described a risk that no longer exists.
            # What a failed CLEAR means is that the debt is still on disk after
            # it has been paid, so the next attach will carry `replace_all`
            # again for an un-export that already happened.
            self._logger.warning(
                "Matter bridge: the un-export completed but the outstanding-work flag could "
                "not be cleared (%s). A later reconnect may repeat the removal request; "
                "nothing extra is removed by it.", exc)

    def _replace_all_then_stop(self, removing: int) -> None:
        """Un-export everything with the §3.1 intent, then drop the client.

        The attach and the close are one coroutine rather than two awaited
        steps, for two reasons: closing the socket before the attach is written
        would lose the un-export entirely, and *waiting* for the attach would
        block whichever Indigo thread emptied the list — which can be the
        device-delete callback, not just a menu click.

        ``removing`` is the size of the allow-list *before* it was emptied, and
        it — not the empty list being sent — is what the deadline is built from.
        The node answers an attach only after its ~100ms-paced removals (§3.3),
        so this one request costs `0.1s × every endpoint it holds`. Letting the
        client default from ``len([])`` gave it the 8s floor, which a set of
        much over 60 blows straight through: the attach "fails" on a timeout
        while the node is still working, the user is told accessories "may
        linger", and then ``close()`` pulls the socket out mid-reconcile — the
        one outcome the warning was describing. The count is an upper bound (a
        skipped export was never sent, so is not there to remove), which is the
        safe direction for a deadline.
        """
        client = self.client
        if client is None:
            return
        self.client = None                 # inert immediately; the close is in flight
        self._un_exporting = True
        self._pushed.clear()
        # Recorded BEFORE the attempt, not after a failure: the ways this does
        # not land include the plugin being reloaded and the Mac losing power
        # mid-attach, and neither of those reaches an `except`.
        self._record_pending_replace_all(removing)
        self._logger.info("Matter bridge: allow-list is now empty — removing every "
                          "exported accessory (pairings are kept)")

        async def _un_export() -> None:
            try:
                # Sized over everything the node may be holding, which after an
                # earlier failed un-export is more than this one emptied.
                await client.attach([], replace_all=True,
                                    timeout=attach_timeout_for(self._pending_replace_all()))
                self._clear_pending_replace_all()
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.warning(
                    "Matter bridge: could not tell the bridge node the export list is empty "
                    "(%s). Accessories will LINGER in paired ecosystems until this is finished. "
                    "It is recorded and will be finished automatically the next time the plugin "
                    "connects to the node — which is when you next export a device, or when the "
                    "plugin next starts up. Nothing happens before then; there is no retry loop.",
                    exc)
            finally:
                # The socket must be released whatever happened above —
                # including a CancelledError at shutdown, which is a
                # BaseException and so walks straight past the handler.
                await client.close()
                self._un_exporting = False
                if self._start_after_un_export:
                    self._start_after_un_export = False
                    self.start()
                else:
                    # **After** the un-export, never before it (XG5's other
                    # half). Stopping the agent first would take the node down
                    # with the removal request still unsent, and the debt in
                    # prefs would then have to reach a node the plugin has just
                    # switched off — which is only recoverable by exporting
                    # something again. Off the loop, because launchctl is
                    # subprocess I/O and this coroutine shares its loop with the
                    # inbound matter-server client.
                    #
                    # Deliberately runs even when the attach FAILED: the socket
                    # is closed either way, the allow-list is empty either way,
                    # and the debt is what carries the un-export forward — it
                    # survives in prefs and is discharged the next time anything
                    # brings the client back up.
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._stop_agent)

        if not self._fire(_un_export(), "un-exporting everything",
                          lost="exported accessories will linger in paired ecosystems"):
            # The coroutine that would have cleared this never ran. Left set,
            # `_un_exporting` gates `start()` for the life of the plugin — so a
            # user who re-adds a device gets a permanently inert bridge on top
            # of an un-export that also did not happen. The debt in prefs is
            # what makes the un-export recoverable; this must not block it.
            self._un_exporting = False
            if self._start_after_un_export:
                self._start_after_un_export = False
                self.start()

    # ------------------------------------------------------------------
    # The endpoint provider (§3.1 attach reconcile source)
    # ------------------------------------------------------------------
    def endpoint_specs(self) -> list:
        """Build the desired endpoint set from the allow-list, re-classified.

        Read fresh on every (re)connect, never cached: ``attach`` is a full
        reconcile and the allow-list may have changed while the socket was down.

        **Blocking, and called off the loop on purpose.** Each entry costs one
        synchronous ``indigo.devices[id]`` IPC copy, so a 60-device allow-list
        is 60 round trips to IndigoServer. ``bridge_client._attach`` runs this
        in an executor for that reason — the loop it would otherwise occupy is
        shared with the inbound matter-server client, and a slow Indigo server
        would stall live Matter device updates behind an export reconcile.
        """
        entries = self._store.all()
        specs = []
        for entry in entries:
            spec = self._spec_for(entry)
            if spec is not None:
                specs.append(spec)
        self._warn_if_wholly_unbridgeable(len(entries), specs)
        return specs

    def _warn_if_wholly_unbridgeable(self, declared: int, specs: list) -> None:
        """Say the thing a warning-per-device does not say.

        Every entry being skipped is not just N independent skips: it means the
        desired set is EMPTY while the allow-list is not, which since issue #141
        is the state that makes the node — now holding its restored endpoint set
        — remove every accessory it has (``bridge_client._replace_all``, which is
        also what stops this halting the client outright). The user has to hear
        that whole sentence, and to hear it *with the devices named*, because
        :meth:`_skip`'s own latch means the per-device lines are not re-printed
        on the reconnect where the removal actually happens.

        Latched on the set of reasons, exactly as :meth:`_skip` is and for the
        same reason: the provider runs once per (re)connect, so an un-latched
        line would turn one permanently broken device into a warning per
        reconnect. A resolved state clears the latch, so a recurrence is news.
        """
        if specs or declared <= 0:
            self._wholly_unbridgeable = ""
            return
        reasons = "; ".join(f"{device_id}: {why}" for device_id, why in sorted(self._skipped.items()))
        if self._wholly_unbridgeable == reasons:
            return
        self._wholly_unbridgeable = reasons
        self._logger.warning(
            "Matter bridge: NONE of the %d device(s) in the export list can be bridged right "
            "now — %s. Every exported accessory is being removed from paired ecosystems until "
            "this is fixed; their endpoint numbers are kept, so putting the devices right "
            "brings the same accessories back rather than new ones.",
            declared, reasons,
        )

    def _declared_export_count(self) -> int:
        """How many entries the allow-list DECLARES, before classification.

        The client's ``export_count_provider``. It is deliberately the store's
        own length and not ``len(self.endpoint_specs())``: the whole question it
        answers is what the user asked for, so that the client can tell an empty
        allow-list ("export nothing", and XG5 means no client at all) apart from
        a full one that classified down to nothing ("export these, but none of
        them can be bridged today"). Those two produce the identical empty
        attach and need opposite handling — see ``bridge_client._replace_all``.
        """
        return len(self._store.all())

    def _spec_for(self, entry) -> Optional[EndpointSpec]:
        """One §4.1 ``EndpointSpec``, or ``None`` with a warning."""
        device_id = entry.indigo_device_id
        dev = self._device_getter(device_id)
        if dev is None:
            return self._skip(device_id, "the Indigo device no longer exists")
        verdict = export_catalog.classify(dev, self._plugin_id)
        if isinstance(verdict, export_catalog.Excluded):
            return self._skip(device_id, f"it is no longer exportable: {verdict.reason}")
        if entry.role not in verdict.eligible_roles:
            return self._skip(device_id, f"it no longer offers the role {entry.role!r} "
                                         f"(now: {', '.join(verdict.eligible_roles)})")
        handler = export_handlers.handler_for(entry.role)
        if handler is None:
            return self._skip(device_id, f"the role {entry.role!r} is not one this plugin "
                                         "version can bridge")
        try:
            states = handler.states_for(dev, entry.options)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception(exc)
            # The exception TEXT is deliberately not part of the dedupe key: a
            # message carrying a timestamp, an address or an attempt counter
            # differs on every attach and would defeat the latch entirely,
            # turning a permanently-broken device into a warning per reconnect.
            return self._skip(device_id, "its state could not be read", detail=str(exc))
        self._skipped.pop(device_id, None)
        # A spec IS a push: `attach` and `upsert_endpoint` both carry `states`
        # and the node applies them, so this is the snapshot every subsequent
        # diff is measured from (see :meth:`device_updated`). Seeding it here
        # rather than at the call site keeps the two paths that build a spec —
        # the attach provider and `upsert` — from having to remember.
        self._pushed[device_id] = dict(states)
        return EndpointSpec(
            indigo_device_id=device_id,
            role=entry.role,
            label=entry.label_for(str(getattr(dev, "name", "") or "")),
            reachable=reachable_of(dev),
            states=states,
            options=dict(entry.options),
        )

    def _skip(self, device_id: int, why: str, detail: str = "") -> None:
        """Warn once per reason, then keep quiet — the provider runs per connect.

        ``why`` is the dedupe key and must be stable for a stable cause;
        ``detail`` is free-form context that goes in the line but never in the
        key (see the ``states_for`` call site for why that distinction exists).
        """
        # A device we are not sending is a device we are not pushing to, so its
        # snapshot is stale by definition; drop it and let the next successful
        # spec re-seed. This is also what bounds the dict for a device that has
        # been deleted from Indigo but not yet from the allow-list.
        self._pushed.pop(device_id, None)
        if self._skipped.get(device_id) != why:
            self._skipped[device_id] = why
            self._logger.warning(
                "Matter bridge: device %s is in the export list but will NOT be bridged — %s%s.",
                device_id, why, f" ({detail})" if detail else "")

    # ------------------------------------------------------------------
    # Indigo → node
    # ------------------------------------------------------------------
    def device_updated(self, orig_dev: Any, new_dev: Any) -> None:
        """Push what changed about an **already-known-exported** device.

        The caller has already established that this device is in the allow-list
        — that check is a set lookup on Indigo's thread and must stay there.

        **The diff is against the last state we PUSHED, not against
        ``orig_dev``** (E5). Every diff carries per-key tolerances
        (``export_handlers.ExportHandler.tolerances``), and a tolerance measured
        against the previous *Indigo reading* only ever bounds one step: a hue
        ramping 1° at a time past a ±1° tolerance reports "unchanged" on every
        single step, so the accessory sits at the colour it had when the ramp
        started while the lamp walks away from it — no error, no log line,
        unbounded drift. Measured against what the ecosystem was actually last
        told, the same tolerance bounds the *total* error at 1°, which is what
        a tolerance is supposed to mean. ``orig_dev`` is still needed, for the
        two things that are genuinely about the transition rather than about
        state: the rename and the reachability flip.
        """
        entry = self._store.get(new_dev.id)
        if entry is None:                      # removed between the check and here
            return
        handler = export_handlers.handler_for(entry.role)
        if handler is None:
            return                             # already warned by the provider
        client = self._live_client("the state update", new_dev.id)
        if client is None:
            return
        # Order matters only in that frames are applied in receipt order (§1):
        # identity first, then availability, then state.
        if entry.name_override is None and orig_dev.name != new_dev.name:
            self.upsert(new_dev.id)
        elif reachable_of(orig_dev) != reachable_of(new_dev):
            # An upsert already carries `reachable`, so only send the split
            # §3.5 command when we are not sending a whole spec anyway.
            self._fire(client.set_reachable(new_dev.id, reachable_of(new_dev)),
                       f"set_reachable dev {new_dev.id}")
        try:
            states, stopped = handler.diff_from(
                self._pushed.get(new_dev.id), new_dev, entry.options)
        except Exception as exc:  # pylint: disable=broad-except
            # Once per device per streak: this fires on every change of an
            # exported device, so a lamp on a dimmer ramp would otherwise write
            # one traceback per step — and a traceback with no device in it is
            # not a lead anyway.
            if new_dev.id not in self._update_failed:
                self._update_failed.add(new_dev.id)
                self._logger.error(
                    "Matter bridge: could not work out what changed about %s (id %s, exported "
                    "as %s) — %s. Its accessory will show stale state until this clears.",
                    getattr(new_dev, "name", ""), new_dev.id, entry.role, exc)
                self._logger.exception(exc)
            return
        self._update_failed.discard(new_dev.id)
        self._report_stopped_keys(new_dev, entry.role, stopped)
        if states:
            self._note_pushed(new_dev.id, states)
            self._fire(client.set_state(new_dev.id, states), f"set_state dev {new_dev.id}")

    def _note_pushed(self, device_id: int, states: dict) -> None:
        """Fold a push into the device's snapshot.

        Merged rather than replaced because ``set_state`` args are partial by
        design (§3.4) and the node leaves keys it was not given untouched — so
        the snapshot has to mirror that or the next diff would re-send every
        key the last one happened not to include.

        Recorded when the push is *scheduled*, not when it is acknowledged.
        ``set_state`` is fire-and-forget and its failure mode is the socket
        being gone, which the next ``attach`` fully reconciles from
        ``states_for`` anyway — whereas waiting for an ack would mean holding
        Indigo's device thread on the node.
        """
        snapshot = self._pushed.get(device_id)
        if snapshot is None:
            self._pushed[device_id] = dict(states)
        else:
            snapshot.update(states)

    def _report_stopped_keys(self, dev: Any, role: str, stopped: frozenset) -> None:
        """Say once when a device stops answering a key it used to publish.

        The protocol has no way to *push* an absence (see
        ``export_handlers.ExportHandler.diff_with_gaps`` for the full reasoning),
        so the ecosystem keeps the last value it was given — a sensor that has
        gone flat reads as its final temperature and a lock whose driver has
        lost the device reads "Locked" indefinitely. Keeping the last known good
        value is the least-bad answer; being quiet about it is not, because the
        symptom is a number that simply never changes again, which looks like
        nothing at all.

        Latched on the key SET, not merely on "there is a gap": a device that
        loses its temperature and then also loses its humidity has told us
        something new, and should say so.
        """
        if not stopped:
            self._stopped_keys.pop(dev.id, None)
            return
        if self._stopped_keys.get(dev.id) == stopped:
            return
        self._stopped_keys[dev.id] = stopped
        self._logger.warning(
            "Matter bridge: device %s (id %s, exported as %s) stopped reporting %s — paired "
            "ecosystems will keep showing the last known value for it until it reports again.",
            getattr(dev, "name", ""), dev.id, role, ", ".join(sorted(stopped)))

    def _live_client(self, what: str, device_id: int) -> Optional[BridgeClient]:
        """The client, but only while it can actually take an endpoint command.

        An incremental CRUD frame sent before ``attach`` completes is refused
        with ``not_attached`` (§1.1) — and would be pointless anyway, because
        the attach that is about to happen carries the full desired set and
        reconciles it (§3.1). So "not attached yet" is a no-op, not an error.

        Two of the three ways to be un-attached are NOT that, though, and both
        used to leave through this same silent ``return``:

        * **halted** — no reconnect is coming and no attach will reconcile
          anything, so the ecosystem shows stale state until a human acts;
        * **recovery** — the node is serving nothing at all until its
          endpoint-number map is rebuilt (§1.1).

        ``BridgeClient._log_dropped_state_push`` says exactly this, loudly, and
        is unreachable from here: the gate happens before ``set_state`` is ever
        called. So the message is replicated rather than routed through — the
        client's version cannot name the operation, and this one can.
        """
        client = self.client
        if client is None:
            self._logger.debug(
                "Matter bridge: no bridge client; dropping %s for device %s", what, device_id)
            return None
        if client.attached:
            return client
        if client.halted:
            self._logger.debug("Matter bridge: bridge client halted; dropping %s for device %s",
                               what, device_id)
            if not self._halted_reported:
                self._halted_reported = True
                self._logger.warning(
                    "Matter bridge: the bridge client is HALTED (%s) — device %s and everything "
                    "after it is NOT reaching any ecosystem, and nothing will retry on its own.",
                    client.halted_reason or "no reason recorded", device_id)
        elif client.recovery:
            self._logger.debug("Matter bridge: bridge in recovery; dropping %s for device %s",
                               what, device_id)
            if not self._recovery_reported:
                self._recovery_reported = True
                self._logger.warning(
                    "Matter bridge: the bridge node is awaiting an endpoint-map rebuild — "
                    "device %s and everything after it is NOT reaching any ecosystem.", device_id)
        else:
            self._logger.debug(
                "Matter bridge: bridge node not attached; dropping %s for device %s "
                "(the next attach reconciles it)", what, device_id)
        return None

    def upsert(self, device_id: int) -> None:
        """(Re)send one endpoint's full spec (§3.2). Fire-and-forget."""
        client = self._live_client("upsert_endpoint", device_id)
        if client is None:
            return
        entry = self._store.get(device_id)
        if entry is None:
            return
        spec = self._spec_for(entry)
        if spec is None:
            return
        self._fire(client.upsert_endpoint(spec), f"upsert_endpoint dev {device_id}")

    def remove(self, device_id: int) -> None:
        """Drop one endpoint (§3.3). Fire-and-forget; idempotent on the node."""
        self._skipped.pop(device_id, None)
        self._update_failed.discard(device_id)
        self._stopped_keys.pop(device_id, None)
        self._no_op_reported.pop(device_id, None)
        # The snapshot dies with the export — that is what bounds it (E5).
        self._pushed.pop(device_id, None)
        client = self._live_client("remove_endpoint", device_id)
        if client is None:
            return
        self._fire(client.remove_endpoint(device_id), f"remove_endpoint dev {device_id}")

    def replace(self, device_id: int) -> None:
        """Re-create one endpoint, because its **role** changed.

        §4.1 rejects a role change on an existing endpoint (``role_change``) —
        ecosystems cache the Matter device type per endpoint — so the only way
        through is remove-then-add. The accessory is genuinely new to every
        paired ecosystem afterwards: it loses its name and room assignment
        there, which is why the dialog says so out loud rather than letting the
        user discover it in the Home app.
        """
        client = self._live_client("the role change", device_id)
        if client is None:
            return

        async def _recreate() -> None:
            await client.remove_endpoint(device_id)
            entry = self._store.get(device_id)
            if entry is None:
                return
            spec = self._spec_for(entry)
            if spec is not None:
                await client.upsert_endpoint(spec)

        self._fire(_recreate(), f"role change for dev {device_id}")

    # ------------------------------------------------------------------
    # Node → Indigo (§5 command events; runs on the loop thread)
    # ------------------------------------------------------------------
    def on_command(self, command: bridge_protocol.BridgeCommand) -> None:
        """Apply one ecosystem-originated action to its Indigo device.

        Called from the client's frame loop, i.e. on the asyncio thread — and
        since E5 it does no Indigo IPC there. Everything from
        ``indigo.devices[id]`` onwards runs in
        :meth:`_command_worker`'s executor, because both halves of the tail are
        blocking calls into IndigoServer and this loop is **shared with the
        inbound matter-server client**: a slow Indigo server would otherwise
        stall live Matter device updates behind somebody pressing a button in
        the Home app. (That `indigo.*` device commands are safe from a
        non-Indigo thread at all remains unverified from the docs — the
        precedent, ``device_sync.apply_states``, covers state *writes* on our
        own devices. Moving them onto a worker thread does not change that
        claim; it only stops them blocking the loop.)

        The store lookup and the role lookup stay on the loop deliberately: both
        are in-process dictionary reads, and doing them here means a command for
        a device nobody exports costs no thread hop at all.
        """
        device_id = command.indigo_device_id
        entry = self._store.get(device_id)
        if entry is None:
            # PRD §7 race row: the endpoint outlived the allow-list entry.
            self._logger.warning(
                "Matter bridge: the bridge node sent %r for Indigo device %s, which is not "
                "exported — ignoring. The accessory should disappear at the next reconnect.",
                command.command, device_id)
            return
        handler = export_handlers.handler_for(entry.role)
        if handler is None:
            self._logger.warning(
                "Matter bridge: %r arrived for device %s exported as %s, a role this version "
                "cannot bridge — ignoring.", command.command, device_id, entry.role)
            return
        self._fire(self._dispatch_off_loop(command, entry, handler),
                   f"{command.command} for dev {device_id}")

    async def _dispatch_off_loop(self, command, entry, handler) -> None:
        """Run :meth:`_apply_command` on the command worker, under a deadline.

        **The deadline is the whole point.** One worker gives per-device FIFO
        (see :meth:`_command_worker`), and the price of that is that one wedged
        ``indigo.*`` call — a Z-Wave device that has stopped answering, a driver
        holding a lock — blocks every subsequent command for every exported
        device. Unbounded and unwatched, that is an outage with **no log output
        at all**: the frames keep arriving, the queue keeps growing, and from
        Indigo's side nothing happened.

        A timeout does not un-wedge the worker — the thread is still stuck in
        the call, and there is no way to interrupt it — so this is not a
        recovery. It is the thing that makes the failure *nameable*: which
        command, on which device, is the one that stopped.

        **And it deliberately does not correct the ecosystem.** :meth:`_correct`
        is the answer to a dispatch that *returned* a failure; there is no
        version of it that helps here. Reading the device back is itself Indigo
        IPC, so it would either queue behind the very call that is wedged (the
        worker is single-threaded, which is the whole point) or run on the loop
        — the one thing E5 moved off it — and the device it would be reading is
        the one that has stopped answering. So the ecosystem goes on showing the
        command it optimistically applied, and the log says so out loud rather
        than leaving the user to infer it.
        """
        worker = self._command_worker()
        if worker is None:
            return
        loop = asyncio.get_running_loop()
        self._submitted += 1
        future = loop.run_in_executor(worker, self._apply_command, command, entry, handler)
        # Counted where it actually finishes, not where we stop waiting: a
        # `run_in_executor` future cannot be cancelled once the thread has
        # picked it up, so a timed-out command is still running and must still
        # read as outstanding. `shield` is what keeps `wait_for` from pretending
        # otherwise. The callback also retrieves the result, so a late failure
        # is not an un-retrieved exception.
        future.add_done_callback(self._note_command_done)
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError:
            self._logger.error(
                "Matter bridge: %r for device %s (%s) has not returned after %.0fs. The command "
                "worker is single-threaded, so every §5 command after it is queued behind this "
                "one — check whether that Indigo device or its plugin is responding. The "
                "ecosystem that sent it already shows it as done and NOTHING will correct that "
                "until the device next reports or the bridge re-attaches.",
                command.command, command.indigo_device_id, entry.role, COMMAND_TIMEOUT)

    def _note_command_done(self, future) -> None:
        """One dispatch finished, whenever that turned out to be."""
        self._completed += 1
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            self._logger.warning("Matter bridge: a §5 command dispatch failed — %s", exc)

    def _command_worker(self):
        """The single thread §5 commands are applied on. Built on first use.

        **One worker, and that is a correctness choice, not a resource one.**
        §4.2 commands are state changes on a specific accessory, and a pool
        would let two for the same device run concurrently — a `setLevel 20`
        overtaking a `setLevel 80` leaves the lamp at the wrong brightness with
        nothing to correct it, because both "succeeded". A single worker makes
        the executor FIFO, which contains per-device FIFO, and matches the
        receipt order §1 already promises for the frames themselves. The cost is
        nil: these are human-paced button presses.
        """
        if self._stopped:
            # A coroutine that was already queued on the loop when `stop()` ran
            # would otherwise build a brand-new worker thread underneath a
            # bridge that has shut down, and nothing would ever join it.
            self._logger.debug("Matter bridge: command worker requested after stop; ignoring")
            return None
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def _apply_command(self, command: bridge_protocol.BridgeCommand, entry, handler) -> None:
        """The blocking half of :meth:`on_command`. Runs on the command worker."""
        device_id = command.indigo_device_id
        dev = self._device_getter(device_id)
        if dev is None:
            self._logger.warning(
                "Matter bridge: %r arrived for device %s, which no longer exists in Indigo — "
                "ignoring.", command.command, device_id)
            return
        try:
            outcome = handler.dispatch(command.command, command.args, dev, entry.options)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter bridge: %r failed for device %s (%s) with args %r — %s. The ecosystem "
                "still shows the state it asked for; pushing the real one back.",
                command.command, device_id, entry.role, command.args, exc)
            self._logger.exception(exc)
            self._correct(handler, dev, device_id, entry.options)
            return
        if outcome is False:
            self._logger.warning(
                "Matter bridge: the bridge node sent %r for device %s (%s), which that role "
                "does not define — ignoring.", command.command, device_id, entry.role)
        elif isinstance(outcome, str):
            self._report_no_op(command.command, device_id, entry.role, outcome)
        else:
            self._no_op_reported.pop(device_id, None)

    def _report_no_op(self, command: str, device_id: int, role: str, reason: str) -> None:
        """Say once when a command was accepted and lawfully changed nothing.

        The third dispatch outcome (``export_handlers.ExportHandler.dispatch``).
        It is neither an error — the role declares the command and §4.2 says it
        must — nor a success worth being silent about: the user pressed
        something in an ecosystem and the house did not move, and until now the
        only trace was a debug line from a stateless handler that could not name
        the device.

        Latched on the *reason* rather than the command name so the message a
        user can act on is said once per streak, however many times the
        ecosystem repeats the request.
        """
        if self._no_op_reported.get(device_id) == reason:
            return
        self._no_op_reported[device_id] = reason
        self._logger.warning(
            "Matter bridge: %r reached device %s (exported as %s) but changed nothing — %s.",
            command, device_id, role, reason)

    def _correct(self, handler, dev: Any, device_id: int,
                 options: Optional[dict] = None) -> None:
        """Push the device's real state after a command we could not apply (F5).

        An ecosystem applies a command optimistically the moment it sends it —
        the Home tile flips before anything reaches Indigo. If the dispatch then
        fails, logging and returning leaves Home showing "on" and the lamp off,
        permanently, until something else happens to that device. Re-reading and
        pushing the truth is the only thing that closes that gap, and it is safe
        to do unconditionally: ``set_state`` is fire-and-forget and the node
        echo-guards its own writes (§6.4).

        **``options`` is not optional in practice, whatever the signature says.**
        The export's §4.1 options are what a role's snapshot *means*: read an
        inverted covering without them and ``states_for`` returns ``100 -
        actual``, so a failed ``goToPosition`` on a blind wired backwards
        "corrected" the ecosystem to the mirror image of where the blind really
        is — a wrong answer pushed with the full authority of the truth, and
        stickier than the stale value it replaced because it looks like a fresh
        report. Every caller threads the entry's options through.
        """
        client = self._live_client("the corrective state push", device_id)
        if client is None:
            return
        try:
            states = handler.states_for(dev, options)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.warning(
                "Matter bridge: could not read device %s back to correct the ecosystem (%s) — "
                "it will show the failed command's state until the next attach.", device_id, exc)
            return
        if not states:
            # The promise was made one line up ("pushing the real one back"), so
            # the case where there is no real one to push cannot be a silent
            # return. It is the same absence `diff_with_gaps` reports: a lock
            # whose `onState` is None has no truth to tell.
            self._logger.warning(
                "Matter bridge: cannot push truth for device %s — it reports no readable state "
                "at all, so the ecosystem keeps showing the command that failed.", device_id)
            return
        self._note_pushed(device_id, states)
        self._fire(client.set_state(device_id, states),
                   f"corrective set_state dev {device_id}")

    # ------------------------------------------------------------------
    # Client callbacks
    # ------------------------------------------------------------------
    def _on_attached(self, status, carried_replace_all: bool = False) -> None:
        """A successful attach ends every outage, so it clears every latch.

        ``carried_replace_all`` is what the completed attach actually SENT, and
        it is the only safe thing to discharge the debt on. Reading live state
        here instead — "is a debt recorded now?" — discharged a debt that was
        written *while this attach was in flight*: the user empties the
        allow-list at the moment a reconnect lands, the reconnect's attach never
        carried the intent, and one step later the flag is cleared with a log
        line asserting an un-export that never happened. The accessories stay in
        every ecosystem, and nothing is left that knows they should not.
        """
        self._disconnect_ticks = 0
        self._unreachable_reported = False
        self._halted_reported = False
        self._recovery_reported = False
        self._refusal_reported = None
        # The attach's own §4.3 report is the fabric set's only source on a fresh
        # connection: `fabrics_changed` fires on CHANGES, so a bridge that has
        # been paired for months emits nothing at all and the §5.5 readout would
        # sit on "not known yet" forever.
        self.fabrics = list(getattr(status, "fabrics", ()) or [])
        # ⊗ The §5.5 window readout's other half. `window_expires_at` is written
        # by the pairing menu and cleared only by the §5 `window_closed` event,
        # which the node does NOT send on shutdown — so a plugin reload during a
        # real window left it None and the readout HID an open window, while a
        # node restart after one left it set forever. An attach is the one moment
        # both sides are present and the node can simply be asked. Fire-and-
        # forget: a readout is not worth blocking a handshake for.
        self._fire(self._refresh_pairing_window(), "reading the bridge node's pairing window")
        self._logger.info("Matter bridge: bridge node attached — %d endpoint(s) live, %s",
                          status.endpoint_count,
                          "commissioned" if status.commissioned else "not yet paired")
        self._report_node_warnings(status)
        if carried_replace_all and self._pending_replace_all():
            owed = self._pending_replace_all()
            self._clear_pending_replace_all()
            self._logger.info(
                "Matter bridge: the outstanding un-export completed — %d accessory record(s) "
                "removed from the bridge node; paired ecosystems will drop them.", owed)
            if len(self._store) == 0:
                # XG5 again: nothing is exported, so nothing needs a socket —
                # and, since E7, nothing needs an agent either. Only when the
                # list is still empty: a debt discharged by an attach that also
                # carried real endpoints is an export we must keep serving, not
                # one to hang up on.
                self._stop_soon("closing the bridge client after the outstanding un-export")
                self._fire(self._stop_agent_off_loop(),
                           "stopping the bridge agent after the outstanding un-export")

    def _report_node_warnings(self, status) -> None:
        """Say what the node could not persist (§4.3 ``warnings``).

        The node writes to stdout and, in this milestone, is **started by hand**
        — so its stdout is a terminal that closed hours ago. A map it could not
        write, a commissioning witness it could not clear: those are precisely
        the faults E5 exists to make visible, and this is the only channel that
        reaches a user's Indigo log.

        Latched on the warning SET, so a standing fault (a full disk does not
        un-fill) is said once per streak rather than once per 15s watchdog tick,
        while a *new* fault appearing beside it is said immediately.
        """
        warnings = frozenset(getattr(status, "warnings", ()) or ())
        if not warnings:
            self._node_warnings = frozenset()
            return
        if warnings == self._node_warnings:
            return
        self._node_warnings = warnings
        for warning in sorted(warnings):
            self._logger.warning("Matter bridge: the bridge node reports — %s", warning)

    def _on_attach_refused(self, code: str, details: str) -> None:
        """Surface a refusal with its remedy. The client has already triaged it.

        Terminal refusals (:data:`bridge_client.TERMINAL_ATTACH_ERRORS`) are
        said every time: each one is a distinct decision the node made and the
        client either halts or parks in recovery, so there is no loop to
        throttle. Everything else is retried on the ordinary backoff and will
        refuse again in ~30s, forever — so those get the same once-per-streak
        latch as ``_on_unreachable``, cleared by the attach that eventually
        succeeds.
        """
        if code == bridge_protocol.ERR_ENDPOINT_MAP_INVALID:
            # One code, two opposite remedies — see REFUSE_IDENTITY_UNREADABLE.
            # This branch used to hard-code the map wording for both, so a user
            # whose identity.json was unreadable was sent to a rebuild the node
            # refuses by design, and told it would duplicate their accessories
            # on the way.
            if bridge_protocol.REFUSE_IDENTITY_UNREADABLE in details:
                self._logger.error(
                    "Matter bridge: the bridge node is serving NOTHING because its identity file "
                    "is unreadable (%s). Rebuilding the endpoint map will NOT fix this and the "
                    "node refuses to try — the unusable file was moved aside as "
                    "identity.json.unreadable-<timestamp> in the bridge storage folder. Restore "
                    "or repair it and restart the bridge node. Deleting it instead starts a "
                    "brand-new bridge, which every paired ecosystem sees as a different device.",
                    details)
                return
            self._logger.error(
                "Matter bridge: the bridge node is serving NOTHING because its endpoint-number "
                "map is unreadable (%s). Nothing will be exported until it is rebuilt (Plugins "
                "▸ Matter ▸ Rebuild Matter Endpoint Map…). The rebuild renumbers nothing: if "
                "only the map file was damaged no paired ecosystem will see any change, and if "
                "the bridge's Matter storage was lost the duplication has already happened.",
                details)
            return
        if code not in TERMINAL_ATTACH_ERRORS:
            if self._refusal_reported == code:
                return
            self._refusal_reported = code
        self._logger.error("Matter bridge: the bridge node refused the connection (%s: %s). "
                           "Nothing is being exported.", code, details)

    def _on_version_skew(self, hello) -> None:
        self._logger.error(
            "Matter bridge: the bridge node speaks protocol version %s, this plugin speaks %s "
            "(node %s). Export is STOPPED and pairings are untouched — restart the bridge agent "
            "so it picks up the node that ships with this plugin.",
            hello.protocol_version, bridge_protocol.PROTOCOL_VERSION, hello.bridge_version)

    def _on_drift_detected(self, drift: list) -> None:
        """Report a drift SET once, however many times the node re-reports it.

        Drift is by design never repaired, so it is re-detected by every attach
        and every upsert for as long as it lasts. Unlatched, the one error that
        names the problem is buried under its own repetitions. Latched on the
        set, so a device joining the drift is still news.

        Since bridge-node 0.8.0 (issue #140) a ``factory_reset
        preserveEndpointNumbers: true`` no longer lands here at all: the NODE
        voids its own witness at reset time and silently adopts the renumbering
        the reset itself causes, because matter.js's allocation was erased
        along with the fabrics and no paired ecosystem could still be holding
        the old numbers. Anything that DOES reach this handler from a >=0.8.0
        node is therefore not the reset renumbering — it is the bridge's
        storage changing for some other reason, which is exactly the anomaly
        this detector exists to catch. The adoption lives node-side, which is
        why the message below names the bridge-node version, not the plugin's:
        a new plugin driving an old node still gets reset drift here, and
        claiming it away would be #132's mistake over again.
        """
        seen = frozenset((d.unique_id, d.expected, d.actual) for d in drift)
        if seen == self._drift_reported:
            return
        self._drift_reported = seen
        self._logger.error(
            "Matter bridge: endpoint-number DRIFT detected — %s. Exported accessories may have "
            "swapped identities in paired ecosystems. Bridge nodes 0.8.0 and newer adopt a "
            "factory reset's own renumbering automatically, so on a current node persistent "
            "drift means the bridge's storage changed OUTSIDE any reset — treat it as a real "
            "anomaly; there is deliberately no dismiss.",
            ", ".join(f"{d.unique_id}: expected {d.expected}, got {d.actual}" for d in drift))

    # ------------------------------------------------------------------
    # §5 pairing activity — the events that make the bridge's own state visible
    # ------------------------------------------------------------------
    def _on_fabrics_changed(self, fabrics: list, change: str) -> None:
        """An ecosystem was added, removed or renamed (§5 ``fabrics_changed``).

        Surfaced at INFO rather than debug, and unlatched, because every one of
        these is a discrete user-visible act — somebody paired Apple Home,
        somebody's Alexa dropped us — and there is no polling loop that would
        otherwise notice. The node emits it for the changes §3.9/§3.10 cause
        themselves as well as for ecosystem-originated ones, so this is also the
        acknowledgement the unpair menu reports against.
        """
        self.fabrics = list(fabrics)
        described = ", ".join(_describe_fabric(fabric) for fabric in fabrics) or "none"
        self._logger.info(
            "Matter bridge: the bridge node's paired ecosystems changed (%s) — now paired with: %s",
            change or "changed", described)

    def _on_commissioned(self) -> None:
        """First fabric (§5 ``commissioned``) — a transition, not a repeat."""
        self._logger.info(
            "Matter bridge: the Matter bridge has been PAIRED for the first time. Exported "
            "accessories should now appear in that ecosystem. To add a second ecosystem, use "
            "Plugins ▸ Matter ▸ Pair Matter Bridge… — the original pairing code no longer works.")

    def _on_decommissioned(self) -> None:
        """Last fabric gone (§5 ``decommissioned``).

        Worth a warning rather than an info: the fabric set emptying makes
        matter.js factory-reset itself, so every exported accessory has just
        disappeared from everywhere — and if the user did not do it deliberately
        (an ecosystem removed *us*), this line is the only notice they get.
        """
        self.fabrics = []
        self.window_expires_at = None
        self._logger.warning(
            "Matter bridge: the Matter bridge is no longer paired with ANY ecosystem. Every "
            "exported accessory has gone with the last fabric. Indigo devices are unaffected; "
            "use Plugins ▸ Matter ▸ Pair Matter Bridge… to pair it again.")

    def _on_window_closed(self, reason: str) -> None:
        """The commissioning window ended (§5 ``window_closed``)."""
        self.window_expires_at = None
        if reason == "commissioned":
            self._logger.info("Matter bridge: the pairing window closed — an ecosystem completed "
                              "commissioning.")
            return
        self._logger.info(
            "Matter bridge: the pairing window has expired without an ecosystem completing "
            "commissioning. Open a new one with Plugins ▸ Matter ▸ Pair Matter Bridge… — the "
            "code it showed is now dead.")

    def note_window_opened(self, expires_at: str) -> None:
        """Record a window the pairing menu just opened, for the §5.5 readout."""
        self.window_expires_at = expires_at

    async def _refresh_pairing_window(self) -> None:
        """Re-derive :attr:`window_expires_at` from the node (§3.7).

        The node is the only thing that knows; the plugin's copy is a cache with
        two failure directions (see the caller in :meth:`_on_attached`). Silent
        on failure at anything above debug: this is a config-dialog readout, and
        a warning about one would out-shout the outage that caused it.
        """
        client = self.client
        if client is None:
            return
        try:
            pairing = await client.get_pairing()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("Matter bridge: could not read the pairing window (%s)", exc)
            return
        self.window_expires_at = pairing.window_expires_at if pairing.window_open else None

    def note_fabrics(self, fabrics: list) -> None:
        """Replace the cached fabric set from a fresh authoritative read.

        The unpair menu's after-the-fact ``get_pairing`` uses this: a removal
        that the §5 event has not landed for yet must not leave the picker
        offering a fabric that has just gone.
        """
        self.fabrics = list(fabrics)

    def _on_unreachable(self, attempts: int) -> None:
        """The node is not answering — and since E7 the agent is asked why.

        The diagnosis matters more here than for the controller, because the
        bridge agent is started and stopped by the allow-list rather than at
        plugin startup: "not running" can mean launchd never got it up, that it
        is crash-looping on a bound Matter port, or that its package was never
        installed. All three present identically at the socket as "connection
        refused". The seam returns the agent's own error-log tail; a failure
        inside it is contained there and costs only the extra sentence.
        """
        if self._unreachable_reported:
            return
        self._unreachable_reported = True
        self._logger.warning(
            "Matter bridge: the bridge node is not responding after %d attempts on port %s. "
            "Indigo devices and inbound Matter control are unaffected; exported accessories "
            "will show as unavailable.%s",
            attempts,
            str(self._prefs_getter().get(bridge_protocol.PREF_WS_PORT)
                or bridge_protocol.DEFAULT_WS_PORT),
            self._agent_diagnosis())

    def _agent_diagnosis(self) -> str:
        """Ask the agent seam why the node is quiet. Never raises; may be empty."""
        if self._agent_diagnose is None:
            return ""
        try:
            detail = self._agent_diagnose()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("Matter bridge: the bridge agent diagnostic failed (%s)", exc)
            return ""
        return f" {detail}" if detail else ""

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------
    def health_tick(self) -> None:
        """One watchdog pass: read client state, log, and poll the node (§4.3).

        **The poll is the only reader the §4.3 ``warnings`` channel has.** Every
        docstring around it — and BRIDGE_PROTOCOL §4.3 itself — says the node's
        persistence failures reach a user because ``get_status`` is polled, and
        for one review cycle nothing polled it: :meth:`_report_node_warnings`
        ran on the attach response and nowhere else. Three of the four faults it
        was built for cannot happen at attach time and so reached the user as
        precisely nothing —

        * the identity witness write on FIRST commissioning (§4.3
          ``identity-write``), which happens when a fabric appears, long after
          the attach;
        * the witness clear on ``factory_reset``, whose failure means the very
          next start refuses to serve and blames lost storage for the reset the
          user asked for;
        * the endpoint-map write from ``upsert``/``remove``'s ``checkDrift``,
          which is a full disk quietly costing the ability to detect that every
          accessory has been renumbered.

        The node's own log is stdout, and in this milestone the node is started
        **by hand** — so stdout is a terminal that closed hours ago. There is no
        other channel.

        One WS round-trip per ~15s tick against a loopback socket, fire-and-
        forget like every other push here, so it costs Indigo's watchdog thread
        nothing: this method still does no blocking I/O of its own.
        """
        self._check_command_queue()
        client = self.client
        if client is None:
            return
        # Both of these persist until a human acts, so the tick that notices
        # them is a tick that will notice them again in 15s, and in 15s after
        # that — the same latch the drop path uses, for the same reason.
        if client.halted:
            if not self._halted_reported:
                self._halted_reported = True
                self._logger.warning(
                    "Matter bridge: the bridge client is HALTED (%s) — nothing is being exported "
                    "and it will not retry on its own.",
                    client.halted_reason or "no reason recorded")
            return
        if client.recovery:
            if not self._recovery_reported:
                self._recovery_reported = True
                self._logger.warning("Matter bridge: the bridge node is awaiting an endpoint-map "
                                     "rebuild; nothing is being exported.")
            return
        if client.attached:
            self._disconnect_ticks = 0
            self._poll_node_status(client)
            return
        self._disconnect_ticks += 1
        if self._disconnect_ticks == DISCONNECT_WARN_TICKS:
            self._logger.warning("Matter bridge: still not attached to the bridge node after "
                                 "~1 min")
        else:
            self._logger.debug("Matter bridge: bridge node not currently attached")

    def _poll_node_status(self, client) -> None:
        """Ask the node how it is, and say what it answers (§3.6 → §4.3).

        Only while attached: the recovery and halted states return above, and in
        both of them the node has already said the one thing that matters, at
        error level, through :meth:`_on_attach_refused`.

        The answer is handled in the coroutine rather than by awaiting it here,
        because this runs on Indigo's watchdog thread and nothing on that thread
        may block on the node (the whole reason ``health_tick`` was "no I/O").
        A failed poll is not itself news — the socket being gone is what
        ``_disconnect_ticks`` is for — so it goes to debug and the next tick
        tries again.
        """
        async def _poll() -> None:
            try:
                status = await client.get_status()
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.debug("Matter bridge: status poll failed (%s)", exc)
                return
            self._report_node_warnings(status)

        self._fire(_poll(), "the bridge node status poll")

    def _check_command_queue(self) -> None:
        """Say once when §5 dispatches are stacking up on the single worker.

        The gap between submitted and completed IS the queue depth, and a
        growing one is the only symptom of a wedged ``indigo.*`` call: every
        command still arrives, nothing raises, and the house simply stops
        responding to the Home app. Once per streak, cleared when it drains.
        """
        outstanding = self._submitted - self._completed
        if outstanding < COMMAND_QUEUE_WARN:
            self._queue_warned = False
            return
        if self._queue_warned:
            return
        self._queue_warned = True
        self._logger.warning(
            "Matter bridge: %d ecosystem command(s) are queued on the command worker and not "
            "completing. They run one at a time, so something at the front is not returning — "
            "the timeout line above names it.", outstanding)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _fire(self, coro, what: str, lost: str = "") -> bool:
        """Schedule ``coro`` on the loop and never wait for it.

        Returns whether it was scheduled — the un-export path needs to know,
        because its own ``finally`` is what releases the gate on :meth:`start`.

        The result is still collected by a done-callback: a ``set_state`` that
        failed looks exactly like "the ecosystem is showing stale state", so it
        must never be silent (§3.4). An un-retrieved future would swallow it.

        ``lost`` names the standing consequence of the coroutine never running
        at all, and its presence is what promotes a scheduling failure from
        debug to warning. Most callers have none: a dropped ``set_state`` or
        ``upsert_endpoint`` is re-delivered by the next attach, so the loop
        being down is a transient the system already recovers from. The run
        loop and the un-export have no such backstop — if those are never
        scheduled, nothing later puts them right.
        """
        try:
            future = self._runtime.submit(coro)
        except Exception as exc:  # pylint: disable=broad-except
            coro.close()
            if lost:
                self._logger.warning("Matter bridge: could not schedule %s (%s) — %s.",
                                     what, exc, lost)
            else:
                self._logger.debug("Matter bridge: could not schedule %s (%s)", what, exc)
            return False
        future.add_done_callback(lambda fut: self._log_future(fut, what))
        return True

    def _log_future(self, future, what: str) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            self._logger.warning("Matter bridge: %s failed — %s", what, exc)


#: Matter vendor IDs whose ecosystems a user is likely to recognise.
#:
#: **Not cosmetic, whatever the previous version of this comment said.** These
#: names are what the "Unpair an Ecosystem…" picker shows, and that picker
#: destroys every exported accessory in the ecosystem the user selects. A wrong
#: name there does not read as a wrong name; it reads as the right ecosystem,
#: and the user removes Apple Home believing they are removing Google.
#:
#: Every entry is verified against the CSA's Distributed Compliance Ledger
#: (``https://on.dcl.csa-iot.org/dcl/vendorinfo/vendors/<decimal id>``), which is
#: the registry that issues them, and the three matter.js also names agree with
#: it (``@matter/node``'s ``IcdMultiAdminError.TRUSTED_ECOSYSTEM_VENDORS``:
#: 0x1384, 0x110A, 0x134B). Two entries were WRONG before that check:
#:
#:   * ``0x100B`` was labelled "Google". The DCL says Signify (Philips Hue).
#:   * ``0x1075`` was labelled "SmartThings" and is not an issued vendor id at
#:     all; Samsung SmartThings is ``0x110A``.
#:
#: Apple appears TWICE by design, and the second one is not a duplicate: an
#: Apple Home pairing creates an ``Apple Home`` fabric AND an ``Apple Keychain``
#: fabric, which is the second Apple fabric ADR-0005 predicted from the observed
#: three-fabric count. A user seeing "vendor 0x1384" beside "Apple Home" cannot
#: tell it is theirs, and unpairing the wrong one of the pair is the same
#: accident as unpairing the wrong ecosystem.
#:
#: Unknown ids are rendered as hex, never guessed at — which is why an entry
#: that cannot be verified is removed rather than left in: hex is a question,
#: a wrong name is a false answer.
VENDOR_NAMES = {
    0x1349: "Apple Home",
    0x1384: "Apple Keychain",       # Apple's SECOND fabric, alongside Apple Home
    0x1217: "Amazon Alexa",
    0x6006: "Google",
    0x110A: "Samsung SmartThings",
    0x134B: "Home Assistant",
    0x100B: "Signify (Philips Hue)",
    0xFFF1: "test vendor",          # the spec's reserved test id; not in the DCL
}


def _describe_fabric(fabric: Any) -> str:
    """One fabric as a human would name it: ``Apple (index 1)``.

    The index is always shown because it is what §3.9 removes a fabric BY, so a
    user reading the log and a user picking from the unpair menu are looking at
    the same identifier.
    """
    vendor_id = int(getattr(fabric, "vendor_id", 0) or 0)
    name = VENDOR_NAMES.get(vendor_id) or f"vendor 0x{vendor_id:04X}"
    label = str(getattr(fabric, "label", "") or "").strip()
    index = getattr(fabric, "fabric_index", "?")
    return f"{name} (index {index})" if not label else f"{name} — {label} (index {index})"


def describe_fabric(fabric: Any) -> str:
    """Public alias of :func:`_describe_fabric` — the menu and the config readout
    render fabrics the same way the log does, so a user matches one to the other."""
    return _describe_fabric(fabric)


def reachable_of(dev: Any) -> bool:
    """§4.1 ``reachable`` for an Indigo device (XAC8).

    ``enabled`` is the user's comm-enabled flag and ``configured`` is Indigo's
    "this device's config dialog has been run" flag; a device failing either is
    one an ecosystem should grey out rather than time out against. Both are
    real base-class properties, and both default to *unreachable* when absent —
    a device we cannot read is not a device we should claim is fine.
    """
    return bool(getattr(dev, "enabled", False)) and bool(getattr(dev, "configured", False))


def _command_executor():
    """The default command worker — see :meth:`ExportBridge._command_worker`."""
    return ThreadPoolExecutor(max_workers=1, thread_name_prefix="matter-export-cmd")


def _indigo_device(device_id: int, logger: Any = None) -> Any:
    """``indigo.devices[device_id]`` or ``None``. Imported lazily, see below.

    The narrow catch is the point. "This id is not in the device table" is the
    expected answer for an allow-list entry whose device was deleted, and the
    caller handles it — but the bare ``except Exception`` that used to be here
    gave the same silent ``None`` to a broken IPC connection, a permissions
    failure, or any other reason IndigoServer could not answer. Those look
    identical from the outside ("device %s no longer exists") and lead the user
    to delete an export that was never the problem.
    """
    # The import is deferred so this module stays importable (and unit-testable)
    # without the Indigo runtime, the same posture export_catalog/export_store
    # take. Every real call site is inside the running plugin.
    import indigo  # pylint: disable=import-outside-toplevel

    try:
        return indigo.devices[int(device_id)]
    except (KeyError, IndexError, TypeError, ValueError):
        # Genuinely absent (or an id that is not one). The caller's message —
        # "the Indigo device no longer exists" — is true.
        return None
    except Exception as exc:  # pylint: disable=broad-except
        if logger is not None:
            logger.warning(
                "Matter bridge: could not read Indigo device %s (%s: %s). This is NOT the device "
                "having been deleted — it is Indigo failing to answer.",
                device_id, type(exc).__name__, exc)
        return None
