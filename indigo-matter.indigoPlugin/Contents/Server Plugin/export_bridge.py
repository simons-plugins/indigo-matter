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

* **A role the plugin cannot bridge is skipped, loudly, not sent.** The §5.1
  dialog already offers ``doorLock``/``windowCovering``/the sensors as roles, so
  the allow-list can hold E4 entries today. An unknown role fails the *whole*
  ``attach`` on the node side (E3a), so one E4 export would silently un-export
  every working one. Skip-with-warning keeps the blast radius at one device, and
  the count is surfaced in the dialog's status line.

* **Nothing here may block Indigo's thread.** ``deviceUpdated`` runs on Indigo's
  callback thread for *every* device on the server. State pushes are submitted
  to the loop and never awaited (§3.4); the result is logged by a done-callback
  so a failed push is never silent.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

import bridge_protocol
import export_catalog
import export_handlers
from bridge_client import BridgeClient
from bridge_protocol import EndpointSpec

#: How many consecutive watchdog ticks a disconnected bridge client tolerates
#: before the log escalates from debug to a single warning. Ticks are ~15s, so
#: this is ~1 minute — the same shape (and the same reasoning) as the
#: matter-server counter in ``plugin._health_tick``.
DISCONNECT_WARN_TICKS = 4


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
    """

    # The seams ARE the API, exactly as BridgeClient's callbacks are.
    # pylint: disable=too-many-arguments
    def __init__(self, store, runtime, logger, prefs_getter: Callable[[], dict], *,
                 plugin_version: str = "unknown",
                 plugin_id: str = export_catalog.DEFAULT_PLUGIN_ID,
                 device_getter: Optional[Callable[[int], Any]] = None,
                 client_factory: Optional[Callable[..., BridgeClient]] = None) -> None:
        self._store = store
        self._runtime = runtime
        self._logger = logger
        self._prefs_getter = prefs_getter
        self._plugin_version = plugin_version
        self._plugin_id = plugin_id
        self._device_getter = device_getter or _indigo_device
        self._client_factory = client_factory or BridgeClient

        #: The live client, or ``None`` while nothing is exported (XG5).
        self.client: Optional[BridgeClient] = None
        #: Last reason each device was skipped by the provider, so a permanent
        #: skip (an E4 role) logs once rather than on every reconnect.
        self._skipped: dict[int, str] = {}
        #: Consecutive watchdog ticks seen disconnected.
        self._disconnect_ticks = 0
        #: Set once the "the node is not running" line has been said for this
        #: outage, so a manually-started-later node does not fill the log first.
        self._unreachable_reported = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """True while a client exists (whether or not it is connected)."""
        return self.client is not None

    def start(self) -> None:
        """Create and run the client. Idempotent."""
        if self.client is not None:
            return
        self.client = self._client_factory(
            self._logger, self._prefs_getter(),
            plugin_version=self._plugin_version,
            endpoint_provider=self.endpoint_specs,
            on_command=self.on_command,
            on_attached=self._on_attached,
            on_attach_refused=self._on_attach_refused,
            on_version_skew=self._on_version_skew,
            on_drift_detected=self._on_drift_detected,
            on_repeated_failure=self._on_unreachable,
        )
        self._unreachable_reported = False
        self._disconnect_ticks = 0
        self._fire(self.client.run(), "bridge client run loop")
        self._logger.info(
            "Matter export: connecting to the bridge node (%d device(s) exported)",
            len(self._store))

    def stop(self, timeout: float = 4.0) -> None:
        """Close the client. Idempotent; never raises at shutdown."""
        client, self.client = self.client, None
        if client is None:
            return
        try:
            self._runtime.submit(client.close()).result(timeout=timeout)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.debug("bridge client close error: %s", exc)

    def exports_changed(self) -> None:
        """The allow-list changed — start or stop the client to match (XG5).

        Called by every path that mutates the store. Incremental endpoint
        updates are the caller's job (:meth:`upsert`/:meth:`remove`); this is
        only the empty↔non-empty transition.
        """
        self._skipped.clear()
        if len(self._store):
            self.start()
        elif self.client is not None:
            # PRD §7 "allow-list emptied": endpoints go, pairings stay. The node
            # needs the §3.1 opt-in for that, so it is a deliberate attach
            # rather than a disconnect — and only THEN do we close.
            self._replace_all_then_stop()

    def _replace_all_then_stop(self) -> None:
        """Un-export everything with the §3.1 intent, then drop the client.

        The attach and the close are one coroutine rather than two awaited
        steps, for two reasons: closing the socket before the attach is written
        would lose the un-export entirely, and *waiting* for the attach would
        block whichever Indigo thread emptied the list — which can be the
        device-delete callback, not just a menu click.
        """
        client = self.client
        if client is None:
            return
        self.client = None                 # inert immediately; the close is in flight
        self._logger.info("Matter export: allow-list is now empty — removing every "
                          "exported accessory (pairings are kept)")

        async def _un_export() -> None:
            try:
                await client.attach([], replace_all=True)
            except Exception as exc:  # pylint: disable=broad-except
                self._logger.warning(
                    "Matter export: could not tell the bridge node the export list is empty "
                    "(%s). Accessories may linger in paired ecosystems until it restarts.", exc)
            await client.close()

        self._fire(_un_export(), "un-exporting everything")

    # ------------------------------------------------------------------
    # The endpoint provider (§3.1 attach reconcile source)
    # ------------------------------------------------------------------
    def endpoint_specs(self) -> list:
        """Build the desired endpoint set from the allow-list, re-classified.

        Read fresh on every (re)connect, never cached: ``attach`` is a full
        reconcile and the allow-list may have changed while the socket was down.
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
            return self._skip(device_id, f"the role {entry.role!r} cannot be bridged yet — "
                                         "sensors, locks, coverings and thermostats land in E4")
        try:
            states = handler.states_for(dev)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception(exc)
            return self._skip(device_id, f"its state could not be read ({exc})")
        self._skipped.pop(device_id, None)
        return EndpointSpec(
            indigo_device_id=device_id,
            role=entry.role,
            label=entry.label_for(str(getattr(dev, "name", "") or "")),
            reachable=reachable_of(dev),
            states=states,
            options=dict(entry.options),
        )

    def _skip(self, device_id: int, why: str) -> None:
        """Warn once per reason, then keep quiet — the provider runs per connect."""
        if self._skipped.get(device_id) != why:
            self._skipped[device_id] = why
            self._logger.warning(
                "Matter export: device %s is in the export list but will NOT be bridged — %s.",
                device_id, why)

    # ------------------------------------------------------------------
    # Indigo → node
    # ------------------------------------------------------------------
    def device_updated(self, orig_dev: Any, new_dev: Any) -> None:
        """Push what changed about an **already-known-exported** device.

        The caller has already established that this device is in the allow-list
        — that check is a set lookup on Indigo's thread and must stay there.
        """
        entry = self._store.get(new_dev.id)
        if entry is None:                      # removed between the check and here
            return
        handler = export_handlers.handler_for(entry.role)
        if handler is None:
            return                             # already warned by the provider
        client = self._live_client()
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
            states = handler.diff(orig_dev, new_dev)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.exception(exc)
            return
        if states:
            self._fire(client.set_state(new_dev.id, states), f"set_state dev {new_dev.id}")

    def _live_client(self) -> Optional[BridgeClient]:
        """The client, but only while it can actually take an endpoint command.

        An incremental CRUD frame sent before ``attach`` completes is refused
        with ``not_attached`` (§1.1) — and would be pointless anyway, because
        the attach that is about to happen carries the full desired set and
        reconciles it (§3.1). So "not attached yet" is a no-op, not an error.
        """
        client = self.client
        return client if client is not None and client.attached else None

    def upsert(self, device_id: int) -> None:
        """(Re)send one endpoint's full spec (§3.2). Fire-and-forget."""
        client = self._live_client()
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
        client = self._live_client()
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
        client = self._live_client()
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

        Called from the client's frame loop, i.e. on the asyncio thread. That is
        the same discipline ``device_sync.apply_states`` already follows for the
        inbound direction: Indigo's ``indigo.*`` calls are thread-safe IPC and
        may be made from the loop. Keeping it in one method here preserves the
        single-seam property — if the loop is ever seen stalling on Indigo IPC,
        this is the only place that has to move to ``run_in_executor``.
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
        dev = self._device_getter(device_id)
        if dev is None:
            self._logger.warning(
                "Matter export: %r arrived for device %s, which no longer exists in Indigo — "
                "ignoring.", command.command, device_id)
            return
        try:
            if not handler.dispatch(command.command, command.args, dev):
                self._logger.warning(
                    "Matter export: the bridge node sent %r for device %s (%s), which that role "
                    "does not define — ignoring.", command.command, device_id, entry.role)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter export: %r failed for device %s (%s) with args %r — %s",
                command.command, device_id, entry.role, command.args, exc)
            self._logger.exception(exc)

    # ------------------------------------------------------------------
    # Client callbacks
    # ------------------------------------------------------------------
    def _on_attached(self, status) -> None:
        self._disconnect_ticks = 0
        self._unreachable_reported = False
        self._logger.info("Matter export: bridge node attached — %d endpoint(s) live, %s",
                          status.endpoint_count,
                          "commissioned" if status.commissioned else "not yet paired")

    def _on_attach_refused(self, code: str, details: str) -> None:
        """Surface a refusal with its remedy. The client has already triaged it."""
        if code == bridge_protocol.ERR_ENDPOINT_MAP_INVALID:
            self._logger.error(
                "Matter export: the bridge node is serving NOTHING because its endpoint-number "
                "map is unreadable (%s). Nothing will be exported until it is rebuilt — and a "
                "rebuild WILL duplicate accessories in ecosystems that are already paired.",
                details)
            return
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
        if client.halted:
            self._logger.warning(
                "Matter export: the bridge client is HALTED (%s) — nothing is being exported "
                "and it will not retry on its own.", client.halted_reason or "no reason recorded")
            return
        if client.recovery:
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
    def _fire(self, coro, what: str) -> None:
        """Schedule ``coro`` on the loop and never wait for it.

        The result is still collected by a done-callback: a ``set_state`` that
        failed looks exactly like "the ecosystem is showing stale state", so it
        must never be silent (§3.4). An un-retrieved future would swallow it.
        """
        try:
            future = self._runtime.submit(coro)
        except Exception as exc:  # pylint: disable=broad-except
            coro.close()
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
