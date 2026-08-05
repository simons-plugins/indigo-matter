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

#: Pref key holding the size of an un-export whose ``attach`` never landed
#: (XAC7). Absent or 0 means there is nothing owed to the node. An int rather
#: than a bool because the §3.1 attach that discharges it has to size its own
#: deadline over the removals — see :func:`bridge_client.attach_timeout_for`.
PREF_PENDING_REPLACE_ALL = "matterExportPendingReplaceAll"


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
    """

    # The seams ARE the API, exactly as BridgeClient's callbacks are.
    # pylint: disable=too-many-arguments
    def __init__(self, store, runtime, logger, prefs_getter: Callable[[], dict], *,
                 plugin_version: str = "unknown",
                 plugin_id: str = export_catalog.DEFAULT_PLUGIN_ID,
                 device_getter: Optional[Callable[[int], Any]] = None,
                 client_factory: Optional[Callable[..., BridgeClient]] = None,
                 save_prefs: Optional[Callable[[], None]] = None,
                 executor_factory: Optional[Callable[[], Any]] = None) -> None:
        self._store = store
        self._runtime = runtime
        self._logger = logger
        self._prefs_getter = prefs_getter
        self._plugin_version = plugin_version
        self._plugin_id = plugin_id
        self._device_getter = device_getter or _indigo_device
        self._client_factory = client_factory or BridgeClient
        self._save_prefs = save_prefs
        self._executor_factory = executor_factory or _command_executor

        #: The live client, or ``None`` while nothing is exported (XG5).
        self.client: Optional[BridgeClient] = None
        #: Last reason each device was skipped by the provider, so a permanent
        #: skip (an unbridgeable role) logs once, not on every reconnect.
        self._skipped: dict[int, str] = {}
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

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True while a client exists (whether or not it is connected)."""
        return self.client is not None

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
                "Matter export: start deferred until the in-flight un-export finishes")
            return
        if self.client is not None:
            return
        self.client = self._client_factory(
            self._logger, self._prefs_getter(),
            plugin_version=self._plugin_version,
            endpoint_provider=self.endpoint_specs,
            replace_all_provider=self._owes_replace_all,
            on_command=self.on_command,
            on_attached=self._on_attached,
            on_attach_refused=self._on_attach_refused,
            on_version_skew=self._on_version_skew,
            on_drift_detected=self._on_drift_detected,
            on_repeated_failure=self._on_unreachable,
        )
        self._unreachable_reported = False
        self._disconnect_ticks = 0
        self._fire(self.client.run(), "bridge client run loop",
                   lost="nothing will be exported until the plugin is reloaded")
        self._logger.info(
            "Matter export: connecting to the bridge node (%d device(s) exported)",
            len(self._store))

    def stop(self, timeout: float = 4.0) -> None:
        """Close the client. Idempotent; never raises at shutdown."""
        client, self.client = self.client, None
        executor, self._executor = self._executor, None
        if executor is not None:
            # Not `wait=True`: a dispatch blocked on a wedged IndigoServer would
            # hold plugin shutdown open, and the command it is running has
            # already been reported to the ecosystem either way.
            executor.shutdown(wait=False)
        if client is None:
            return
        try:
            self._runtime.submit(client.close()).result(timeout=timeout)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("bridge client close error: %s", exc)

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
        if count:
            self.start()
        elif self.client is not None:
            # PRD §7 "allow-list emptied": endpoints go, pairings stay. The node
            # needs the §3.1 opt-in for that, so it is a deliberate attach
            # rather than a disconnect — and only THEN do we close.
            self._replace_all_then_stop(removing)
        elif self._owes_replace_all():
            # Nothing is exported and there is no client — but the node is still
            # holding accessories from a previous session whose un-export never
            # landed. XG5 says no client while nothing is exported; this is the
            # one exception, and it lasts exactly one successful attach.
            self._logger.info(
                "Matter export: reconnecting to finish an un-export that did not complete "
                "earlier (%d accessory record(s) still owed removal)",
                self._pending_replace_all())
            self.start()

    def _pending_replace_all(self) -> int:
        """How many endpoints an un-export still owes the node, from prefs."""
        try:
            return int(self._prefs_getter().get(PREF_PENDING_REPLACE_ALL) or 0)
        except (TypeError, ValueError):
            return 0

    def _owes_replace_all(self) -> bool:
        """§3.1 ``intent: replace_all`` on the next attach? (XAC7)

        Read by :class:`bridge_client.BridgeClient` on every (re)connect. It is
        deliberately ANDed with an empty allow-list: the flag says an un-export
        did not land, and an allow-list that has since been re-populated has
        superseded it — that attach carries real endpoints, cannot empty the
        live set, and needs no opt-in.
        """
        return self._pending_replace_all() > 0 and not len(self._store)

    def _record_pending_replace_all(self, removing: int) -> None:
        """Persist (or clear) the un-export debt.

        Written to prefs rather than held in memory because the failure it
        covers is precisely the one that outlives the process: the node is down
        or the plugin is reloading, the attach never lands, and every accessory
        stays in every paired ecosystem forever with nothing left anywhere that
        knows it should not (XAC7). A flush failure is logged, not raised —
        losing the flag is the pre-E5 behaviour, and it must not take the
        un-export attempt down with it.
        """
        try:
            prefs = self._prefs_getter()
            if removing > 0:
                prefs[PREF_PENDING_REPLACE_ALL] = removing
            else:
                prefs.pop(PREF_PENDING_REPLACE_ALL, None)
            if self._save_prefs is not None:
                self._save_prefs()
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.warning(
                "Matter export: could not record that the un-export is outstanding (%s). "
                "If it does not complete now, exported accessories may linger.", exc)

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
        self._logger.info("Matter export: allow-list is now empty — removing every "
                          "exported accessory (pairings are kept)")

        async def _un_export() -> None:
            try:
                await client.attach([], replace_all=True,
                                    timeout=attach_timeout_for(removing))
                self._record_pending_replace_all(0)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.warning(
                    "Matter export: could not tell the bridge node the export list is empty "
                    "(%s). Accessories will linger in paired ecosystems until the plugin can "
                    "reach the node again — it will retry on its own.", exc)
            finally:
                # The socket must be released whatever happened above —
                # including a CancelledError at shutdown, which is a
                # BaseException and so walks straight past the handler.
                await client.close()
                self._un_exporting = False
                if self._start_after_un_export:
                    self._start_after_un_export = False
                    self.start()

        self._fire(_un_export(), "un-exporting everything",
                   lost="exported accessories will linger in paired ecosystems")

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
        specs = []
        for entry in self._store.all():
            spec = self._spec_for(entry)
            if spec is not None:
                specs.append(spec)
        return specs

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
                "Matter export: device %s is in the export list but will NOT be bridged — %s%s.",
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
                    "Matter export: could not work out what changed about %s (id %s, exported "
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
            "Matter export: device %s (id %s, exported as %s) stopped reporting %s — paired "
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
                "Matter export: no bridge client; dropping %s for device %s", what, device_id)
            return None
        if client.attached:
            return client
        if client.halted:
            self._logger.debug("Matter export: bridge client halted; dropping %s for device %s",
                               what, device_id)
            if not self._halted_reported:
                self._halted_reported = True
                self._logger.warning(
                    "Matter export: the bridge client is HALTED (%s) — device %s and everything "
                    "after it is NOT reaching any ecosystem, and nothing will retry on its own.",
                    client.halted_reason or "no reason recorded", device_id)
        elif client.recovery:
            self._logger.debug("Matter export: bridge in recovery; dropping %s for device %s",
                               what, device_id)
            if not self._recovery_reported:
                self._recovery_reported = True
                self._logger.warning(
                    "Matter export: the bridge node is awaiting an endpoint-map rebuild — "
                    "device %s and everything after it is NOT reaching any ecosystem.", device_id)
        else:
            self._logger.debug(
                "Matter export: bridge node not attached; dropping %s for device %s "
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
                "Matter export: the bridge node sent %r for Indigo device %s, which is not "
                "exported — ignoring. The accessory should disappear at the next reconnect.",
                command.command, device_id)
            return
        handler = export_handlers.handler_for(entry.role)
        if handler is None:
            self._logger.warning(
                "Matter export: %r arrived for device %s exported as %s, a role this version "
                "cannot bridge — ignoring.", command.command, device_id, entry.role)
            return
        self._fire(self._dispatch_off_loop(command, entry, handler),
                   f"{command.command} for dev {device_id}")

    async def _dispatch_off_loop(self, command, entry, handler) -> None:
        """Run :meth:`_apply_command` on the command worker."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._command_worker(),
                                   self._apply_command, command, entry, handler)

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
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def _apply_command(self, command: bridge_protocol.BridgeCommand, entry, handler) -> None:
        """The blocking half of :meth:`on_command`. Runs on the command worker."""
        device_id = command.indigo_device_id
        dev = self._device_getter(device_id)
        if dev is None:
            self._logger.warning(
                "Matter export: %r arrived for device %s, which no longer exists in Indigo — "
                "ignoring.", command.command, device_id)
            return
        try:
            outcome = handler.dispatch(command.command, command.args, dev, entry.options)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter export: %r failed for device %s (%s) with args %r — %s. The ecosystem "
                "still shows the state it asked for; pushing the real one back.",
                command.command, device_id, entry.role, command.args, exc)
            self._logger.exception(exc)
            self._correct(handler, dev, device_id, entry.options)
            return
        if outcome is False:
            self._logger.warning(
                "Matter export: the bridge node sent %r for device %s (%s), which that role "
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
            "Matter export: %r reached device %s (exported as %s) but changed nothing — %s.",
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
                "Matter export: could not read device %s back to correct the ecosystem (%s) — "
                "it will show the failed command's state until the next attach.", device_id, exc)
            return
        if not states:
            # The promise was made one line up ("pushing the real one back"), so
            # the case where there is no real one to push cannot be a silent
            # return. It is the same absence `diff_with_gaps` reports: a lock
            # whose `onState` is None has no truth to tell.
            self._logger.warning(
                "Matter export: cannot push truth for device %s — it reports no readable state "
                "at all, so the ecosystem keeps showing the command that failed.", device_id)
            return
        self._note_pushed(device_id, states)
        self._fire(client.set_state(device_id, states),
                   f"corrective set_state dev {device_id}")

    # ------------------------------------------------------------------
    # Client callbacks
    # ------------------------------------------------------------------
    def _on_attached(self, status) -> None:
        """A successful attach ends every outage, so it clears every latch."""
        self._disconnect_ticks = 0
        self._unreachable_reported = False
        self._halted_reported = False
        self._recovery_reported = False
        self._refusal_reported = None
        self._logger.info("Matter export: bridge node attached — %d endpoint(s) live, %s",
                          status.endpoint_count,
                          "commissioned" if status.commissioned else "not yet paired")
        if self._owes_replace_all():
            # The attach that just succeeded carried `intent: replace_all` and
            # an empty set (see `_owes_replace_all`), so the debt is discharged.
            owed = self._pending_replace_all()
            self._record_pending_replace_all(0)
            self._logger.info(
                "Matter export: the outstanding un-export completed — %d accessory record(s) "
                "removed from the bridge node; paired ecosystems will drop them.", owed)
            # XG5 again: nothing is exported, so nothing needs a socket.
            self._stop_soon("closing the bridge client after the outstanding un-export")

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
            self._logger.error(
                "Matter export: the bridge node is serving NOTHING because its endpoint-number "
                "map is unreadable (%s). Nothing will be exported until it is rebuilt — and a "
                "rebuild WILL duplicate accessories in ecosystems that are already paired.",
                details)
            return
        if code not in TERMINAL_ATTACH_ERRORS:
            if self._refusal_reported == code:
                return
            self._refusal_reported = code
        self._logger.error("Matter export: the bridge node refused the connection (%s: %s). "
                           "Nothing is being exported.", code, details)

    def _on_version_skew(self, hello) -> None:
        self._logger.error(
            "Matter export: the bridge node speaks protocol version %s, this plugin speaks %s "
            "(node %s). Export is STOPPED and pairings are untouched — restart the bridge agent "
            "so it picks up the node that ships with this plugin.",
            hello.protocol_version, bridge_protocol.PROTOCOL_VERSION, hello.bridge_version)

    def _on_drift_detected(self, drift: list) -> None:
        self._logger.error(
            "Matter export: endpoint-number DRIFT detected — %s. Exported accessories may have "
            "swapped identities in paired ecosystems. This is never repaired automatically.",
            ", ".join(f"{d.unique_id}: expected {d.expected}, got {d.actual}" for d in drift))

    def _on_unreachable(self, attempts: int) -> None:
        """The node is not answering. In E3 that usually means it is not running."""
        if self._unreachable_reported:
            return
        self._unreachable_reported = True
        self._logger.warning(
            "Matter export: the bridge node is not responding after %d attempts on port %s. "
            "In this build the node is started by hand — check it is running. Indigo devices "
            "are unaffected; exported accessories will show as unavailable.",
            attempts,
            str(self._prefs_getter().get(bridge_protocol.PREF_WS_PORT)
                or bridge_protocol.DEFAULT_WS_PORT))

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------
    def health_tick(self) -> None:
        """One watchdog pass. No I/O — it only reads client state and logs."""
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
                    "Matter export: the bridge client is HALTED (%s) — nothing is being exported "
                    "and it will not retry on its own.",
                    client.halted_reason or "no reason recorded")
            return
        if client.recovery:
            if not self._recovery_reported:
                self._recovery_reported = True
                self._logger.warning("Matter export: the bridge node is awaiting an endpoint-map "
                                     "rebuild; nothing is being exported.")
            return
        if client.attached:
            self._disconnect_ticks = 0
            return
        self._disconnect_ticks += 1
        if self._disconnect_ticks == DISCONNECT_WARN_TICKS:
            self._logger.warning("Matter export: still not attached to the bridge node after "
                                 "~1 min")
        else:
            self._logger.debug("Matter export: bridge node not currently attached")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _fire(self, coro, what: str, lost: str = "") -> None:
        """Schedule ``coro`` on the loop and never wait for it.

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
                self._logger.warning("Matter export: could not schedule %s (%s) — %s.",
                                     what, exc, lost)
            else:
                self._logger.debug("Matter export: could not schedule %s (%s)", what, exc)
            return
        future.add_done_callback(lambda fut: self._log_future(fut, what))

    def _log_future(self, future, what: str) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            self._logger.warning("Matter export: %s failed — %s", what, exc)


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


def _indigo_device(device_id: int) -> Any:
    """``indigo.devices[device_id]`` or ``None``. Imported lazily, see below."""
    # The import is deferred so this module stays importable (and unit-testable)
    # without the Indigo runtime, the same posture export_catalog/export_store
    # take. Every real call site is inside the running plugin.
    import indigo  # pylint: disable=import-outside-toplevel

    try:
        return indigo.devices[int(device_id)]
    except Exception:  # pylint: disable=broad-except
        return None
