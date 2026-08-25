"""The bridge health band, split out of :mod:`export_bridge` (refactor B2).

Everything here is the ONE band of :class:`export_bridge.ExportBridge` that
reads and writes an Indigo device directly — the standing ``matterBridgeHealth``
device (issue #286) and its per-fabric slots (issue #288), plus the §4.3
``sessionHygiene`` log lines (issue #283 "Finding 2"). Every other band talks to
Indigo only through injected seams or the wire protocol; this one is the
exception, which is why it is the highest-risk cut of the B2 split.

:class:`BridgeHealthReporter` holds a back-reference to the
:class:`export_bridge.ExportBridge` that owns it (``self._bridge``) rather than
copying its state, because three of the things it reads —
``_disconnect_ticks``, ``_halted_reported``, ``_recovery_reported`` — are
mutable latches **shared** with the rest of ``ExportBridge`` (:meth:`_live_client`
also reads/writes ``_halted_reported``/``_recovery_reported``, and ``start``/
``_on_attached`` reset all three). A copy would give each latch two owners and
turn a once-per-streak notice into two.

This module must NOT import matter.js anything (ADR-0006, workspace) and must
not import ``plugin.py`` or any mixin — it is reached only through
``ExportBridge``, exactly as the rest of the export engine is.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

#: How many consecutive watchdog ticks a disconnected bridge client tolerates
#: before the log escalates from debug to a single warning. Ticks are ~15s, so
#: this is ~1 minute — the same shape (and the same reasoning) as the
#: matter-server counter in ``plugin._health_tick``.
DISCONNECT_WARN_TICKS = 4

#: The `matterBridgeHealth` device type (Devices.xml, issue #286) — the one
#: standing Indigo device that represents the bridge NODE PROCESS itself,
#: as opposed to any individual exported accessory. Found-or-created lazily;
#: see :meth:`BridgeHealthReporter._ensure_health_device`.
HEALTH_DEVICE_TYPE_ID = "matterBridgeHealth"
HEALTH_DEVICE_NAME = "Matter Bridge Health"

#: `subscriptionHealth` state values (Devices.xml). "unknown" is never read as
#: an all-clear — see :meth:`BridgeHealthReporter._apply_subscription_churn`
#: and BRIDGE_PROTOCOL §4.3's `checked` gate.
HEALTH_UNKNOWN = "unknown"
HEALTH_HEALTHY = "healthy"
HEALTH_CHURNING = "churning"

#: Fixed positional fabric slots `matterBridgeHealth` carries (issue #288).
#: ADR-0007: a shipped state is permanent, so this count is a one-time
#: deliberate choice, not a config knob — 5 covers every ecosystem
#: combination actually run (Apple Home, Apple Keychain, Alexa, Google,
#: SmartThings...) with room to spare, without an unbounded state list. A
#: 6th+ connected fabric is dropped from the slots (never silently — see
#: :meth:`BridgeHealthReporter._warn_if_fabrics_dropped`), not made to grow
#: the list.
FABRIC_SLOT_COUNT = 5

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
    render fabrics the same way the log does, so a user matches one to the other.

    Re-exported as ``export_bridge.describe_fabric`` (pairing_menu_mixin.py,
    pairing_page.py, plugin.py all import it from there) — kept here so the
    reporter's own :meth:`BridgeHealthReporter._on_fabrics_changed` and every
    existing caller resolve to the same function object.
    """
    return _describe_fabric(fabric)


def _describe_churn(churn: Any) -> str:
    """The bridge health device's ``churnDetail`` line for an active verdict
    (§4.3, issue #286): names the peer(s), because "a controller is churning"
    leaves a user reading the device nothing to act on, and two Echoes on one
    fabric are not interchangeable.
    """
    peers = [
        f"{getattr(peer, 'peer_node_id', '?')} (fabric {getattr(peer, 'fabric_index', '?')}): "
        f"{getattr(peer, 'invalid_deletions', 0)} deletion(s)/"
        f"{getattr(peer, 'window_minutes', 0)}min, "
        f"{getattr(peer, 'live_sessions', 0)} live session(s)"
        for peer in (getattr(churn, "peers", None) or [])
    ]
    return "; ".join(peers)


def _fabric_slot_key(slot: int, field: str) -> str:
    """``fabric1Name``, ``fabric3Health``, ... — 1-based, matching the state
    ids Devices.xml declares for ``matterBridgeHealth`` (issue #288)."""
    return f"fabric{slot}{field}"


def _describe_fabric_slot(fabric: Any) -> str:
    """A fabric slot's Name (issue #288 review finding A): vendor FIRST, the
    label as an optional suffix — the same shape :func:`_describe_fabric`
    already uses, and for the same reason: real Apple/Alexa/Google fabrics
    never call ``UpdateFabricLabel``, so ``label`` is empty on every fabric
    Simon's own rig has ever shown. A label-only reading would put "fabric 1
    / fabric 8 / fabric 9" in every slot — meaningless, and unable to tell
    Apple Home from Apple Keychain apart (only ``VENDOR_NAMES`` can, off the
    vendor id, since both leave the label blank).

    A present ``vendor_id`` ALWAYS renders something — the known name, or the
    ``vendor 0x%04X`` fallback for one the DCL table doesn't have — with the
    label appended only when non-empty. ``fabric <index>`` is the last
    resort, reached only when the vendor id itself is missing (0/absent) AND
    there is no label either.
    """
    vendor_id = int(getattr(fabric, "vendor_id", 0) or 0)
    label = str(getattr(fabric, "label", "") or "").strip()
    if vendor_id:
        name = VENDOR_NAMES.get(vendor_id) or f"vendor 0x{vendor_id:04X}"
        return f"{name} — {label}" if label else name
    if label:
        return label
    return f"fabric {getattr(fabric, 'fabric_index', '?')}"


def _fabric_slot_plan(fabrics: list, churn: Optional[Any]) -> tuple:
    """The desired ``(names, healths, dropped)`` for ``FABRIC_SLOT_COUNT``
    positional slots (issue #288), from a CHECKED verdict's own fabric list.

    Positional, not tied to real fabric indices: fabrics are sorted by
    ``fabric_index`` and packed into slots 1..N in order, so a fabric leaving
    repacks the rest — a vacated slot goes empty, it does not hold the next
    fabric's OLD position (Simon's explicit design call, issue #288). Slots
    beyond ``FABRIC_SLOT_COUNT`` connected fabrics are reported in ``dropped``
    (oldest-index-excess first) rather than silently discarded — the caller
    is responsible for surfacing that
    (:meth:`BridgeHealthReporter._warn_if_fabrics_dropped`).

    Per-slot health is ``HEALTH_CHURNING`` for a fabric any churn peer names,
    else ``HEALTH_HEALTHY`` — UNLESS the verdict is ``active`` and NONE of its
    peers name a fitted fabric, in which case every fitted slot reads
    ``HEALTH_UNKNOWN`` instead of ``HEALTH_HEALTHY`` (issue #288 review
    finding B). That happens whenever the churn is real but cannot be pinned
    to a visible slot: ``peers`` came back empty despite ``active`` (every
    entry was malformed and dropped by ``_parse_churn_peer``), a peer's
    ``fabric_index`` defaulted to 0 and matches nothing connected, or the
    churning peer's fabric is one of ``dropped`` — the same "no fitted
    match" test catches that last case too, since ``fitted``/``dropped``
    partition the connected fabrics and a dropped index can never intersect
    ``fitted``'s. Asserting "healthy" here would be exactly the all-clear
    this feature exists never to claim without having actually observed it.

    This helper is only ever called against a CHECKED verdict; the unchecked
    case is a different, device-state-driven path entirely (see
    :meth:`BridgeHealthReporter._apply_subscription_churn`).
    """
    ordered = sorted(fabrics, key=lambda fabric: getattr(fabric, "fabric_index", 0))
    fitted, dropped = ordered[:FABRIC_SLOT_COUNT], ordered[FABRIC_SLOT_COUNT:]
    peers = getattr(churn, "peers", None) or []
    churning_indices = {getattr(peer, "fabric_index", None) for peer in peers}
    fitted_indices = {getattr(fabric, "fabric_index", None) for fabric in fitted}
    active = bool(getattr(churn, "active", False))
    unattributable = active and not (churning_indices & fitted_indices)
    names = [""] * FABRIC_SLOT_COUNT
    healths = [""] * FABRIC_SLOT_COUNT
    for i, fabric in enumerate(fitted):
        names[i] = _describe_fabric_slot(fabric)
        if getattr(fabric, "fabric_index", None) in churning_indices:
            healths[i] = HEALTH_CHURNING
        elif unattributable:
            healths[i] = HEALTH_UNKNOWN
        else:
            healths[i] = HEALTH_HEALTHY
    return names, healths, dropped


def _find_health_device_only(logger: Any = None) -> Any:
    """Find the standing ``matterBridgeHealth`` device (Devices.xml, issue
    #286), or ``None`` — NEVER creates one. Imported lazily, same posture as
    :func:`export_bridge._indigo_device`.

    The find-only counterpart of :func:`_create_health_device`. Every
    "nothing observed" path (:meth:`BridgeHealthReporter._mark_health_unknown`,
    via :meth:`BridgeHealthReporter._find_health_device`) uses this one, so a
    user-deleted device — or one that has simply never been created yet — is
    never recreated just to be told "unknown" (review finding 1).

    ``indigo.devices.iter("self")`` is this plugin's own devices only, so a
    same-named device belonging to another plugin can never be mistaken for
    it. One such device is ever wanted; the first match wins.
    """
    import indigo  # pylint: disable=import-outside-toplevel

    for device in indigo.devices.iter("self"):
        if device.deviceTypeId == HEALTH_DEVICE_TYPE_ID:
            return device
    return None


def _unique_health_device_name(indigo_module: Any, name: str) -> str:
    """The same numeric-suffix collision policy ``device_sync._unique_name``
    uses (review finding 5).

    Indigo device names are server-global, so handing ``indigo.device.create``
    a fixed name is not safe: ANY existing device already named
    "Matter Bridge Health" — including one whose type was since changed via
    Indigo's Edit Device Type menu, which leaves it ``configured=False`` and
    invisible to ``indigo.devices.iter("self")`` (issue #62's failure mode) —
    makes ``create`` raise ``NameNotUnique`` forever, with a retry that could
    never succeed on its own.
    """
    existing = {dev.name for dev in indigo_module.devices}
    if name not in existing:
        return name
    suffix = 2
    while f"{name} {suffix}" in existing:
        suffix += 1
    return f"{name} {suffix}"


def _create_health_device(logger: Any = None) -> Any:
    """Create the standing ``matterBridgeHealth`` device (issue #286).
    Imported lazily, same posture as :func:`export_bridge._indigo_device`.

    Only ever called after :func:`_find_health_device_only` has confirmed none
    exists (:meth:`BridgeHealthReporter._ensure_health_device`) — this function
    itself does not check, so calling it twice makes two devices.
    """
    import indigo  # pylint: disable=import-outside-toplevel

    name = _unique_health_device_name(indigo, HEALTH_DEVICE_NAME)
    return indigo.device.create(
        protocol=indigo.kProtocol.Plugin,
        deviceTypeId=HEALTH_DEVICE_TYPE_ID,
        name=name,
    )


class BridgeHealthReporter:
    """The `matterBridgeHealth` device + `sessionHygiene` log band, split out
    of :class:`export_bridge.ExportBridge` (refactor B2).

    :param bridge: the owning :class:`export_bridge.ExportBridge` — see the
        module docstring for why this is a back-reference rather than copied
        state.
    :param health_device_finder: finds the standing ``matterBridgeHealth``
        device WITHOUT creating one (issue #286); injected so this module
        unit-tests without the Indigo runtime. Every "nothing observed" path
        uses this one — see :meth:`_find_health_device`.
    :param health_device_factory: CREATES the ``matterBridgeHealth`` device.
        Only ever called after :attr:`_health_device_finder` has confirmed
        none exists — see :meth:`_ensure_health_device`.
    """
    # `self._bridge.<attr>` throughout this class is the back-reference design
    # itself (see the module docstring for why it is a reference, not a copy)
    # — this collaborator and `export_bridge.ExportBridge` are one unit split
    # across two files for size, not for encapsulation between them.
    # pylint: disable=protected-access

    def __init__(self, bridge, *,
                 health_device_finder: Optional[Callable[[], Any]] = None,
                 health_device_factory: Optional[Callable[[], Any]] = None) -> None:
        self._bridge = bridge
        self._health_device_finder = health_device_finder or (
            lambda: _find_health_device_only(self._bridge._logger))
        self._health_device_factory = health_device_factory or (
            lambda: _create_health_device(self._bridge._logger))
        #: id of the standing `matterBridgeHealth` device (issue #286), once
        #: resolved — cached so a healthy bridge does not rescan
        #: ``indigo.devices`` on every ~15s watchdog tick. ``None`` until the
        #: first churn signal that needs it.
        self._health_device_id: Optional[int] = None
        #: Set once the "could not find/create the bridge health device"
        #: warning has been said, so a standing problem (a full disk, same
        #: shape as ``ExportBridge._node_warnings``) is not repeated on every
        #: subsequent change.
        self._health_device_warned = False
        #: Same latch shape, for a WRITE that reaches an existing device but
        #: fails (issue #286 review finding 3) — distinct from
        #: ``_health_device_warned`` (finding/creating the device itself)
        #: because either can fail independently and each deserves its own
        #: one-per-streak line.
        self._health_write_warned = False
        #: ``(subscriptionHealth, churnDetail)`` last actually WRITTEN to the
        #: bridge health device, or ``None`` before the first write. What
        #: :meth:`_apply_subscription_churn` diffs against, so a standing
        #: churn verdict is not rewritten — or re-logged — on every ~15s tick,
        #: the same discipline ``ExportBridge._report_node_warnings`` keeps
        #: for ``warnings``. Also advanced (with no device touched) once an
        #: "unknown" verdict has been checked against a bridge with no health
        #: device to represent it at all — see :meth:`_apply_subscription_churn`.
        #: Since issue #288 the tuple is
        #: ``(subscriptionHealth, churnDetail, slot_names, slot_healths)`` —
        #: ``slot_names``/``slot_healths`` are ``FABRIC_SLOT_COUNT``-tuples, or
        #: bare ``None`` for "no device exists to have slots at all".
        self._health_state: Optional[tuple] = None
        #: The fabric_index set named in the last "more connected fabrics than
        #: slots" warning (issue #288), so a STANDING overflow (still the same
        #: 6 fabrics next tick) is said once per streak — same shape as
        #: ``ExportBridge._node_warnings``. Cleared the moment the overflow set
        #: changes, including back to none.
        self._dropped_fabrics_warned: frozenset = frozenset()
        #: Set once :meth:`_find_health_device_for_unknown` has performed its
        #: ONE-per-session full ``indigo.devices.iter("self")`` scan (issue
        #: #288 review finding C) — an unchecked verdict (no client, halted,
        #: recovering, detached, export off) recurs every ~15s watchdog tick
        #: for as long as a bridge is never attached, and without this a user
        #: who never enables export pays that scan forever. Never reset once
        #: True: a device the scan did not find is not looked for again this
        #: session by THIS path — a CHECKED verdict's own
        #: :meth:`_ensure_health_device` (unaffected by this flag) is what
        #: creates one if warranted, and sets :attr:`_health_device_id`
        #: directly, which the cheap cached-id branch then picks up.
        self._health_reconcile_scanned = False
        #: Same latch shape as :attr:`_health_write_warned`, for a READ that
        #: fails inside the unchecked branch (issue #288 review finding C) —
        #: this runs on Indigo's watchdog thread, which only catches
        #: ``StopThread``, so an unguarded IPC exception here would kill
        #: health ticks for the rest of the plugin session.
        self._health_read_warned = False
        #: Set while the "session hygiene has stopped watching" warning has
        #: been said for this streak (issue #283 "Finding 2" review) — same
        #: latch shape as `_health_device_warned`. Cleared the moment
        #: `sessionHygiene.checked` goes back to `true`, which is what draws
        #: the one-off recovery INFO line in `_apply_session_hygiene`.
        self._hygiene_warned = False
        #: `sessionHygiene.closed`'s totals as of the last CHECKED report,
        #: keyed by reason — what `_apply_session_hygiene` diffs the next
        #: report against to log a per-reason DELTA rather than restating a
        #: running total on every ~15s tick. `None` before the first checked
        #: report this session has seen.
        self._hygiene_closed_totals: Optional[dict] = None

    def _find_health_device(self) -> Optional[Any]:
        """Resolve the standing ``matterBridgeHealth`` device WITHOUT creating
        one (issue #286 review finding 1).

        Checked first by the cached id (a plain ``device_getter`` round trip),
        failing that — the id unset, e.g. the very first tick after a plugin
        restart, or the cached device having been deleted out-of-band — by an
        actual find-only scan (:attr:`_health_device_finder`). Used by
        :meth:`_ensure_health_device`, i.e. only from a CHECKED verdict (an
        attach or a successful poll) — never creates on its own, but may scan
        on EVERY call that misses the cache, unthrottled. An UNCHECKED
        verdict uses :meth:`_find_health_device_for_unknown` instead, which
        adds the once-per-session throttle finding C needs: a checked verdict
        recurs only while genuinely attached, so its scan cost is bounded by
        real activity; an unchecked one recurs forever for a bridge that is
        never attached at all.
        """
        if self._health_device_id is not None:
            device = self._bridge._device_getter(self._health_device_id)
            if device is not None:
                return device
            self._health_device_id = None
        device = self._health_device_finder()
        if device is not None:
            self._health_device_id = device.id
        return device

    def _find_health_device_for_unknown(self) -> Optional[Any]:
        """Resolve the health device for an UNCHECKED verdict, cheaply on
        every call after the first (issue #288 review finding C).

        If :attr:`_health_device_id` is already cached, this is a single
        ``device_getter`` round trip — identical cost to
        :meth:`_find_health_device`. If it is NOT cached, the full
        ``indigo.devices.iter("self")`` scan behind that method runs AT MOST
        ONCE PER PLUGIN SESSION from here — the one scan issue #286's
        restart-reconcile needs (a device persisted "healthy" from a
        PREVIOUS session must still be found and corrected) — and
        :attr:`_health_reconcile_scanned` remembers the answer, found or not,
        afterwards. Without that memo a user who has never enabled export
        pays a fresh scan on every ~15s watchdog tick, forever, since there
        is never a real signal to cache an id from.

        Does NOT gate :meth:`_ensure_health_device` — a CHECKED verdict's own
        attach or successful poll may still scan (and create) as before;
        only this "nothing was ever checked" cold path is throttled.

        Raises whatever ``device_getter``/the finder raise. The caller
        (:meth:`_apply_subscription_churn`'s unchecked branch) is the one
        place both this method's reads and the device's own ``.states`` read
        are wrapped, so a single latch covers every Indigo read the branch
        makes — this runs on Indigo's watchdog thread, which catches only
        ``StopThread``, so an unguarded exception here would kill health
        ticks for the rest of the session.
        """
        if self._health_device_id is not None:
            device = self._bridge._device_getter(self._health_device_id)
            if device is not None:
                return device
            # Deleted out-of-band. Not re-scanned for: there is only ever
            # meant to be one, and a CHECKED verdict's own
            # `_ensure_health_device` is what recreates it if warranted — at
            # which point it sets `_health_device_id` directly and this
            # method's cheap cached-id branch picks it up next time.
            self._health_device_id = None
        if self._health_reconcile_scanned:
            return None
        self._health_reconcile_scanned = True
        device = self._health_device_finder()
        if device is not None:
            self._health_device_id = device.id
        return device

    def _ensure_health_device(self) -> Optional[Any]:
        """Resolve the standing ``matterBridgeHealth`` device, CREATING one if
        :meth:`_find_health_device` confirms none exists.

        Only called from a REAL churn signal — an attach or a successful poll
        (:meth:`_apply_subscription_churn`'s ``may_create`` branch); every
        "nothing observed" path uses the find-only resolver instead (finding
        1). Never raises: a creation failure must not break the poll loop, so
        it is logged once (the same latch shape as
        ``ExportBridge._node_warnings``) and retried the next time a state
        changes. A factory that returns ``None`` without raising is not
        silently accepted either (finding 5) — it gets the same one-per-streak
        warning as an exception would.
        """
        device = self._find_health_device()
        if device is not None:
            return device
        try:
            device = self._health_device_factory()
        except Exception as exc:  # pylint: disable=broad-except
            if not self._health_device_warned:
                self._health_device_warned = True
                self._bridge._logger.warning(
                    "Matter bridge: could not find or create the bridge health device (%s) — "
                    "controller subscription health will not be visible as an Indigo device "
                    "until this is fixed.", exc)
            return None
        if device is None:
            if not self._health_device_warned:
                self._health_device_warned = True
                self._bridge._logger.warning(
                    "Matter bridge: could not create the bridge health device — controller "
                    "subscription health will not be visible as an Indigo device until this "
                    "is fixed.")
            return None
        self._health_device_id = device.id
        self._health_device_warned = False
        return device

    def _apply_subscription_churn(self, churn: Optional[Any],
                                  fabrics: Optional[list] = None) -> None:
        """Drive ``matterBridgeHealth``'s states from a §4.3 ``subscriptionChurn``
        verdict (issue #286) and the ``fabrics`` it came with (issue #288's
        per-fabric slots), or from ``None``/no fabrics for "nothing checked it".

        ``checked=False`` — including a bare ``None``, an old node's status,
        or a halted/detached client — is ``unknown``, never ``healthy``: this
        must never claim an all-clear it did not actually observe, the same
        rule BRIDGE_PROTOCOL §4.3 states for ``driftChecked``. That branch
        only ever FINDS the device (:meth:`_find_health_device_for_unknown`,
        throttled to one full scan per session — issue #288 review finding
        C) — never creates one, because there is nothing yet to correct on a
        bridge that has never attached — and it only ever touches the HEALTH
        half of a slot it finds already occupied (a non-empty Name), read
        straight off the device's own current states: there is no local
        memory of the slot plan to fall back on across a plugin restart, and
        the device itself is the one thing that survived one. A real
        (checked) verdict may create the device
        (:meth:`_ensure_health_device`) and fully RECOMPUTES the slot plan
        from the fresh ``fabrics`` list — slots are positional, not tied to
        real fabric indices, so a fabric leaving repacks the rest rather than
        leaving a gap (issue #288 design, Simon's explicit call).

        Writes happen only on a CHANGE — the watchdog re-polls every ~15s and
        a standing verdict must not become a rewrite on every tick, matching
        ``ExportBridge._report_node_warnings``'s own discipline;
        :meth:`_write_health_state` is the shared write/latch/recovery-log
        tail both branches use. The warning sentence for an active verdict
        already rides the §4.3 ``warnings`` channel
        (``ExportBridge._report_node_warnings``); this only logs the
        RECOVERY, so the two channels never say the same thing twice.
        """
        if churn is not None and getattr(churn, "checked", False):
            active = bool(getattr(churn, "active", False))
            health = HEALTH_CHURNING if active else HEALTH_HEALTHY
            detail = _describe_churn(churn) if active else ""
            slot_names, slot_healths, dropped = _fabric_slot_plan(fabrics or [], churn)
            self._warn_if_fabrics_dropped(dropped)
            new_state = (health, detail, tuple(slot_names), tuple(slot_healths))
            if new_state == self._health_state:
                return
            previous = self._health_state
            device = self._ensure_health_device()
            if device is None:
                return  # already logged; retried on the next change
            self._write_health_state(device, new_state, previous)
            return

        # Unchecked verdict: never creates, and only ever corrects HEALTH on
        # slots the device already shows occupied — see the docstring above.
        # Contract (issue #288 review finding C): device resolution is at
        # most ONE `indigo.devices.iter("self")` scan per plugin session
        # (`_find_health_device_for_unknown`, not the unthrottled
        # `_find_health_device`), and every Indigo read this branch makes —
        # that resolution AND the device's own `.states` — is inside this one
        # try/except, so a failure degrades (logged once, latched) instead of
        # propagating out of `health_tick` on the watchdog thread.
        try:
            device = self._find_health_device_for_unknown()
            if device is None:
                slot_names = slot_healths = None
            else:
                slot_names = tuple(
                    str(device.states.get(_fabric_slot_key(i + 1, "Name"), "") or "")
                    for i in range(FABRIC_SLOT_COUNT))
                slot_healths = tuple(HEALTH_UNKNOWN if name else "" for name in slot_names)
        except Exception as exc:  # pylint: disable=broad-except
            if not self._health_read_warned:
                self._health_read_warned = True
                self._bridge._logger.warning(
                    "Matter bridge: could not read the bridge health device (%s) — controller "
                    "subscription health may be showing a stale reading until this is fixed.",
                    exc)
            return
        self._health_read_warned = False
        if device is None:
            # Nothing to represent, and nothing wrong either — remember this
            # WAS checked, so the same "no device yet" answer is not looked
            # for again (see `_find_health_device_for_unknown`). `None` slots
            # (not empty tuples) mark "no device", distinct from a real
            # device with every slot genuinely vacant.
            new_state = (HEALTH_UNKNOWN, "", None, None)
            if new_state != self._health_state:
                self._health_state = new_state
            return
        new_state = (HEALTH_UNKNOWN, "", slot_names, slot_healths)
        if new_state == self._health_state:
            return
        previous = self._health_state
        self._write_health_state(device, new_state, previous)

    def _write_health_state(self, device: Any, new_state: tuple,
                            previous: Optional[tuple]) -> None:
        """Push ``new_state`` to ``device`` and advance ``_health_state`` on
        success (issue #288) — the shared tail of both
        :meth:`_apply_subscription_churn` branches, which differ in how they
        resolve the device and compute the desired slot plan but not in how
        the write, the failure latch, or the recovery log line work.

        Never raises: a write failure must not break the poll loop. Latched
        the same shape as :attr:`_health_device_warned` — the first failure
        warns, cleared on the next successful write — and ``_health_state``
        is deliberately NOT advanced on failure, so the next change (or the
        very next tick, since the unchanged value would otherwise look
        identical) retries the write rather than believing it landed.
        """
        health, detail, slot_names, slot_healths = new_state
        kv = [
            {"key": "subscriptionHealth", "value": health},
            {"key": "churnDetail", "value": detail},
        ]
        for i in range(FABRIC_SLOT_COUNT):
            kv.append({"key": _fabric_slot_key(i + 1, "Name"), "value": slot_names[i]})
            kv.append({"key": _fabric_slot_key(i + 1, "Health"), "value": slot_healths[i]})
        try:
            device.updateStatesOnServer(kv)
        except Exception as exc:  # pylint: disable=broad-except
            if not self._health_write_warned:
                self._health_write_warned = True
                self._bridge._logger.warning(
                    "Matter bridge: could not update the bridge health device (%s) — it may be "
                    "showing a stale reading until this is fixed.", exc)
            return
        self._health_write_warned = False
        self._health_state = new_state
        # Only churning earns the recovery line: unknown -> healthy is an
        # ordinary reattach (node restart, plugin start), not a churn ending.
        if previous is not None and previous[0] == HEALTH_CHURNING and health == HEALTH_HEALTHY:
            self._bridge._logger.info("Matter bridge: controller subscription churn has recovered")

    def _warn_if_fabrics_dropped(self, dropped: list) -> None:
        """Say, once per streak, which connected fabrics did not fit
        ``FABRIC_SLOT_COUNT`` slots (issue #288) — no silent caps, matching
        the workspace's degradation-path convention. Cleared the moment the
        overflow set changes, including back to none, the same latch shape
        as ``ExportBridge._report_node_warnings``.
        """
        indices = frozenset(getattr(fabric, "fabric_index", None) for fabric in dropped)
        if not indices:
            self._dropped_fabrics_warned = frozenset()
            return
        if indices == self._dropped_fabrics_warned:
            return
        self._dropped_fabrics_warned = indices
        names = ", ".join(_describe_fabric_slot(fabric) for fabric in dropped)
        self._bridge._logger.warning(
            "Matter bridge: %d connected fabric(s) exceed the %d slots \"%s\" tracks — not "
            "shown: %s.", len(dropped), FABRIC_SLOT_COUNT, HEALTH_DEVICE_NAME, names)

    def _apply_session_hygiene(self, hygiene: Optional[Any]) -> None:
        """Surface §4.3's ``sessionHygiene`` verdict (issue #283 "Finding 2"
        review) — until this, ``checked: False`` and the per-peer/per-reason
        counts reached nobody: the node ACTS on its own (it force-closes
        sessions through matter.js's session-layer API), so nothing here
        gates behaviour, and this stays deliberately minimal beside
        :meth:`_apply_subscription_churn` — log lines only, no device, no UI.

        ``hygiene.sent`` is what makes the WARNING meaningful: a pre-0.17.0
        node never sends the ``sessionHygiene`` field at all, and
        :func:`bridge_protocol.parse_session_hygiene` collapses that absence
        to the same ``checked=False`` a CURRENT node reports when its
        mitigation has failed — both mean "never looked", but only the
        second is news. An old node must stay silent forever; a current one
        that goes from checked to unchecked must say so once, the same latch
        shape ``ExportBridge._report_node_warnings`` uses for the
        persistence-failure set.
        """
        if hygiene is None or not getattr(hygiene, "sent", False):
            return
        if not hygiene.checked:
            if not self._hygiene_warned:
                self._hygiene_warned = True
                self._bridge._logger.warning(
                    "Matter bridge: session hygiene has stopped watching the bridge node's "
                    "session layer — the superseded/dead/rotated-session mitigations for "
                    "issue #283 are no longer running. Restart the Matter bridge node to "
                    "restore it.")
            return
        if self._hygiene_warned:
            self._hygiene_warned = False
            self._bridge._logger.info(
                "Matter bridge: session hygiene has recovered and is watching the bridge "
                "node's session layer again.")
        closed = hygiene.closed
        totals = {
            "superseded": closed.superseded,
            "dead": closed.dead,
            "rotated": closed.rotated,
        }
        previous = self._hygiene_closed_totals
        self._hygiene_closed_totals = totals
        if previous is None:
            # The first checked report this session — a baseline, not a
            # change, so there is nothing yet to diff it against.
            return
        deltas = {reason: totals[reason] - previous[reason]
                  for reason in totals if totals[reason] != previous[reason]}
        if not deltas:
            return
        peers = ", ".join(
            f"{peer.peer_node_id} (fabric {peer.fabric_index}): {peer.live_sessions}"
            for peer in hygiene.peers) or "none"
        self._bridge._logger.debug(
            "Matter bridge: session hygiene closed %s since the last report; live CASE "
            "sessions per peer now: %s",
            ", ".join(f"{reason} +{delta}" for reason, delta in deltas.items()), peers)

    def _mark_health_unknown(self) -> None:
        """§4.3 issue #286 — the node halted, detached, export was switched
        off, or this is simply the first health tick since a plugin restart
        with a "healthy"/"churning" device still on disk from a previous
        session (review finding 1) — any churn verdict this plugin was
        holding is stale, or was never actually observed this session at all.

        Never CREATES a device (:meth:`_find_health_device_for_unknown`) — only corrects
        one that already exists (from this session OR a previous one) or
        leaves a bridge with none alone.
        """
        self._apply_subscription_churn(None)

    def _on_fabrics_changed(self, fabrics: list, change: str) -> None:
        """An ecosystem was added, removed or renamed (§5 ``fabrics_changed``).

        Surfaced at INFO rather than debug, and unlatched, because every one of
        these is a discrete user-visible act — somebody paired Apple Home,
        somebody's Alexa dropped us — and there is no polling loop that would
        otherwise notice. The node emits it for the changes §3.9/§3.10 cause
        themselves as well as for ecosystem-originated ones, so this is also the
        acknowledgement the unpair menu reports against.
        """
        self._bridge.fabrics = list(fabrics)
        described = ", ".join(_describe_fabric(fabric) for fabric in fabrics) or "none"
        self._bridge._logger.info(
            "Matter bridge: the bridge node's paired ecosystems changed (%s) — now paired with: %s",
            change or "changed", described)

    def note_fabrics(self, fabrics: list) -> None:
        """Replace the cached fabric set from a fresh authoritative read.

        The unpair menu's after-the-fact ``get_pairing`` uses this: a removal
        that the §5 event has not landed for yet must not leave the picker
        offering a fabric that has just gone.
        """
        self._bridge.fabrics = list(fabrics)

    def health_tick(self) -> None:
        """One watchdog pass: read client state, log, and poll the node (§4.3).

        **The poll is the only reader the §4.3 ``warnings`` channel has.** Every
        docstring around it — and BRIDGE_PROTOCOL §4.3 itself — says the node's
        persistence failures reach a user because ``get_status`` is polled, and
        for one review cycle nothing polled it: ``_report_node_warnings`` ran on
        the attach response and nowhere else. Three of the four faults it was
        built for cannot happen at attach time and so reached the user as
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

        ``_check_command_queue``/``_poll_node_status`` stay on
        :class:`export_bridge.ExportBridge` (they touch its own
        ``_submitted``/``_completed``/client rather than any health state), so
        this reaches them through the back-reference.
        """
        self._bridge._check_command_queue()
        client = self._bridge.client
        if client is None:
            # Also covers the very first tick after a plugin restart (review
            # finding 1): nothing is exported YET this session, but a device
            # from a PREVIOUS one may still read "healthy" with nothing behind
            # it. Find-only, never creates — see `_mark_health_unknown`.
            self._mark_health_unknown()
            return
        # Both of these persist until a human acts, so the tick that notices
        # them is a tick that will notice them again in 15s, and in 15s after
        # that — the same latch the drop path uses, for the same reason.
        if client.halted:
            self._mark_health_unknown()
            if not self._bridge._halted_reported:
                self._bridge._halted_reported = True
                self._bridge._logger.warning(
                    "Matter bridge: the bridge client is HALTED (%s) — nothing is being exported "
                    "and it will not retry on its own.",
                    client.halted_reason or "no reason recorded")
            return
        if client.recovery:
            self._mark_health_unknown()
            if not self._bridge._recovery_reported:
                self._bridge._recovery_reported = True
                self._bridge._logger.warning("Matter bridge: the bridge node is awaiting an "
                                             "endpoint-map rebuild; nothing is being exported.")
            return
        if client.attached:
            self._bridge._disconnect_ticks = 0
            self._bridge._poll_node_status(client)
            return
        self._mark_health_unknown()
        self._bridge._disconnect_ticks += 1
        if self._bridge._disconnect_ticks == DISCONNECT_WARN_TICKS:
            self._bridge._logger.warning("Matter bridge: still not attached to the bridge node "
                                         "after ~1 min")
        else:
            self._bridge._logger.debug("Matter bridge: bridge node not currently attached")
