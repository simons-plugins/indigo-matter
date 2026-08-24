"""The observed-clamp physical colour-temperature bounds learner (issue #293).

**Why a learner, not just a declared range.** Even a device's own reported
range is only a claim — the MiBoxer strip that triggered this issue claimed
2200K in its z2m props while the silicon physically clamps at 2500K. Native
Matter bulbs' declared bounds hold only because the claim and the hardware
share a vendor; ours never do. So the plugin does not trust ANY declared
number as ground truth — it watches what the hardware actually does and
learns from that instead (issue #293's 2026-08-23 design revision; see
``docs/adr/0014-*.md``).

**The evidence.** ADR-0013 already pushes a confirmed, commanded colour
temperature as state and relies on ``CT_TOLERANCE_MIREDS`` to absorb the
device's own contradicting echo. That echo — a commanded 426 mireds answered
by a device-confirmed 400 — IS the device stating its warm floor. This module
watches for exactly that pattern:

* :meth:`CTBoundsLearner.record_commanded` notes the reference point, called
  from ``export_bridge.ExportBridge._push_commanded`` after Indigo has
  confirmed a ``setColorTemp`` dispatch (never merely requested — ADR-0013's
  own doctrine again);
* :meth:`CTBoundsLearner.observe` feeds every fresh CT reading, called from
  ``ExportBridge.device_updated`` BEFORE (and independent of) the ordinary
  diff's tolerance — the tolerance is precisely the shortfall band this
  learner exists to see, so gating this on the diff would hide the evidence
  from the one place looking for it.

**Two ways a bound moves**, both persisted via the injected ``store`` and
republished via the injected ``republish`` callable (``ExportBridge.upsert``):

* **shortfall adoption** — two CONSECUTIVE, roughly-matching echoes that fall
  short of a *fresh* (≤15s old) commanded write, on the same side, adopt the
  echoed value as that side's learned bound. Two, not one, to filter a single
  transient (a driver hiccup, a read mid-transition) from a real clamp; the
  freshness window is what stops an unrelated Indigo-side change hours later
  from ever being misread as hardware evidence — see
  :data:`FRESHNESS_WINDOW_SECONDS`'s own docstring for the measured trap this
  guards against. The two confirmations must also answer two DISTINCT
  commanded dispatches, not one dispatch heard twice — a live incident
  (2026-08-24 16:40, device 1894385558, on the 2026.27.1 build) showed a
  single z2m state change firing ``deviceUpdated`` more than once with the
  same lagged value, which satisfied a same-dispatch "two observations" rule
  without the hardware ever having been asked twice; see :class:`_Pending`'s
  ``commanded_at`` field and :meth:`CTBoundsLearner._observe_locked` for the
  mechanism.
* **re-widening** — ANY reading outside the CURRENT effective bounds proves
  reach immediately, no streak needed: the device just did the thing the
  bounds said it could not, which is its own proof. This is what self-heals
  a wrong seed (the whole reason a declared value is only ever a starting
  point) without waiting for a second confirmation of something already
  demonstrated.

**What this module deliberately does NOT touch.** ADR-0013's commanded push
stays exactly as it was: the pushed value is the command, clamped only to
the generic 153/500 domain — never to the effective learned/seeded bounds.
Clamping the push would mean the fabric attribute never even ASKS past the
current bounds, and it is exactly that overreach that produces the shortfall
evidence this learner exists to read. See ``export_handlers.
ColorTemperatureLightExport.commanded_states`` and
``ExportBridge._push_commanded`` — neither changed by this feature.

No Indigo import: like ``export_store``, this class takes an injected
duck-typed ``store`` (``.get(device_id)`` / ``.upsert(entry)``) and unit-tests
against a fake.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Optional

import ct_bounds
from matter_handlers.color_control import mireds_to_kelvin

#: How stale a commanded reference may be and still count as "this echo is
#: answering that command". Live-measured design constraint (issue #293
#: pre-implementation notes): the trap this guards against is a scene change
#: or an unrelated Indigo-side edit landing on a device HOURS after the last
#: plugin-issued ``setColorTemp`` — coincidentally inside the old shortfall
#: band — being read as fresh clamp evidence. 15s comfortably covers the
#: round trip of a real command (dispatch → driver → hardware → z2m echo →
#: ``deviceUpdated``) while excluding everything that is not a direct answer
#: to it.
FRESHNESS_WINDOW_SECONDS = 15.0

#: An echo has to differ from what was commanded by at least this many
#: mireds to count as a candidate shortfall at all. Every hardware clamp
#: this project has actually measured is 26-47 mireds: the MiBoxer strip
#: that opened issue #281 (426 asked, 400 echoed — a 26-mired gap, see
#: ``docs/DEVICE-NOTES.md``), Innr sideboards (153 -> 200 = 47 mireds cool,
#: 500 -> 454 = 46 mireds warm) and Hue pendants (500 -> 454 = 46 mireds
#: warm) — ADR-0014's own measured-hardware table. A transition/quantization
#: transient measures nothing like that: the live incident that forced this
#: threshold up (2026-08-24 15:07, device 1894385558) commanded 206 mireds
#: and got a mid-transition echo of 202 TWICE — a 4-mired gap that
#: nonetheless repeated identically and was adopted as a false warm bound,
#: only for the settled 206 reading to re-widen it back moments later (75ms
#: of pure store-write/republish churn for nothing learned). And a genuine
#: clamp smaller than ``export_handlers.CT_TOLERANCE_MIREDS`` (30) needs no
#: published bound at all — ADR-0013's own tolerance already absorbs it
#: silently, so there is no reason for the detection threshold to sit below
#: 10, and every reason for it to sit above the measured transient band. 10
#: leaves comfortable headroom under every measured clamp and above the
#: measured transient. ``ct_calibration.py``'s sweep deliberately reuses
#: this same constant rather than keeping its own: its echoes are read back
#: after ``SETTLE_SECONDS`` (2.5s) of settling, so it never sees a transient
#: this threshold would need to reject, and every real clamp it measures
#: clears 10 as comfortably as the passive path's does.
SHORTFALL_THRESHOLD_MIREDS = 10

#: Two shortfall readings count as "the same value" within this many mireds.
#: The in-range mireds↔Kelvin round trip is near-exact (see
#: ``export_handlers.CT_TOLERANCE_MIREDS``'s own docstring on this point), so
#: two readings a mired apart are the SAME hardware limit read twice, not two
#: different ones.
REPEAT_TOLERANCE_MIREDS = 1


@dataclasses.dataclass
class _Commanded:
    """The last CONFIRMED ``setColorTemp`` reference for one device."""
    mireds: int
    at: float


@dataclasses.dataclass
class _Pending:
    """One side's shortfall streak, one confirming observation away from adoption."""
    side: str    # "min" (cool shortfall) or "max" (warm shortfall)
    mireds: int  # the value the streak is converging on
    #: The `at` of the `_Commanded` reference that STARTED this streak — the
    #: dispatch the streak's first observation was a shortfall against. Kept
    #: so a later observation can tell "a second answer to a second question"
    #: (a different dispatch's `at`) from "the same answer heard twice" (see
    #: the module docstring's 2026-08-24 16:40 incident).
    commanded_at: float


class CTBoundsLearner:
    """Learns per-device physical CT bounds from commanded/echo pairs (#293).

    One instance per ``ExportBridge``, never per device — all per-device
    state lives in the two dicts below, keyed on ``indigo_device_id``.

    **Threading.** :meth:`record_commanded` runs on the command worker
    (``ExportBridge._push_commanded``, itself invoked from
    ``_apply_command``); :meth:`observe` runs on Indigo's device thread
    (``ExportBridge.device_updated``). The two dicts are therefore behind one
    lock. A plain ``Lock`` rather than the store's own ``RLock``: an adoption
    calls ``self._store.upsert(...)`` and ``self._republish(...)`` while
    still HOLDING this lock (see :meth:`_adopt`), but neither call re-enters
    THIS class — ``ExportStore`` has its own separate lock, and
    ``ExportBridge.upsert`` never calls back into the learner — so there is
    no re-entrancy for an ``RLock`` to provide for. The cost is a learner
    blocked on Indigo's own prefs I/O for the (rare) duration of an
    adoption, which is the same trade-off every other store write in this
    plugin already makes.
    """

    def __init__(self, store, logger, republish: Callable[[int], None],
                 now: Callable[[], float] = time.monotonic) -> None:
        self._store = store
        self._logger = logger
        self._republish = republish
        self._now = now
        self._lock = threading.Lock()
        #: device_id -> the last CONFIRMED `setColorTemp` dispatch.
        self._commanded: dict[int, _Commanded] = {}
        #: device_id -> the in-progress shortfall streak, if any.
        self._pending: dict[int, _Pending] = {}

    def forget(self, device_id: int) -> None:
        """Drop a removed device's in-progress learner state (#293).

        Called from ``ExportBridge.remove()`` alongside its other per-device
        dicts. Without this, ``_commanded``/``_pending`` entries for an
        un-exported device sit forever: a device_id later reused by a
        different Indigo device (deleted-and-recreated) would inherit a
        stale commanded reference or shortfall streak that has nothing to do
        with the new device's hardware. The learned bound itself is not
        touched here — that lives in the export's own ``options`` in the
        store, which ``ExportBridge.remove()`` already drops by dropping the
        export entry.
        """
        with self._lock:
            self._commanded.pop(int(device_id), None)
            self._pending.pop(int(device_id), None)

    def record_commanded(self, device_id: int, mireds: int) -> None:
        """Note a CONFIRMED ``setColorTemp`` dispatch as the reference point.

        Called from ``ExportBridge._push_commanded``, which only ever runs
        after Indigo has already accepted the dispatch (ADR-0013) — so this
        is Indigo-confirmed truth about what was ASKED, not merely what an
        ecosystem requested. Recorded unconditionally, even when the
        commanded push itself cannot reach the node (client not live): a
        later reconnect's echo still answers this same command, and the
        freshness window — not the bridge's connectivity — is what should
        decide whether it still counts.
        """
        with self._lock:
            self._commanded[int(device_id)] = _Commanded(mireds=int(mireds), at=self._now())

    def observe(self, entry, mireds: int) -> None:
        """Feed one fresh CT reading. May adopt or re-widen a learned bound.

        ``entry`` is the CALLER's own fresh read of the export (``device_
        updated`` and ``_push_commanded`` both read the store once per
        update and pass that same read through, rather than this method
        taking a second one that could disagree with it mid-call).
        """
        if entry.role not in ct_bounds.CT_ROLES:
            return
        with self._lock:
            self._observe_locked(entry, int(mireds))

    # ------------------------------------------------------------------
    def adopt_measured(self, entry, side: str, mireds: int, *, reason: str) -> None:
        """Persist one side's measurement from an EXPLICIT calibration sweep
        (``ct_calibration.CTCalibrationEngine``, issue #293's ADR-0014 Option
        C extension — active probing, rejected as an AUTOMATIC mechanism, now
        shipped as an operator-invoked action that feeds this same store).

        Deliberately thin: it funnels straight into :meth:`_adopt` under the
        SAME lock — so a sweep's measurement gets the identical collapse-
        refusal guard, re-read-before-write, one INFO log, and republish that
        passive shortfall/re-widen evidence already gets. No second adoption
        path is written; that would be exactly the "one fact said twice" this
        module's own docstring warns against.

        ``entry`` here is deliberately NOT used to compute the guard's
        current bounds — only its ``indigo_device_id``/``role`` matter, and
        :meth:`_adopt` re-reads the store itself for the rest (see its own
        docstring for why: a live incident showed this caller's own ``entry``
        can be a snapshot taken before a MULTI-SIDE sweep started, stale by
        the time the second side's call lands here).

        Unlike :meth:`observe`, there is no streak to build and no freshness
        window to check: the caller (the calibration engine) already knows
        it commanded ``mireds`` and read the echo back itself, in the same
        call — the sweep IS its own fresh, single-shot reference, so a
        second confirmation would only cost time for no better evidence.
        Role-gated the same way ``observe`` is, for the same reason: a
        non-CT role has no bounds to learn.
        """
        if entry.role not in ct_bounds.CT_ROLES:
            return
        with self._lock:
            self._adopt(entry, side, int(mireds), reason=reason)

    def _observe_locked(self, entry, mireds: int) -> None:
        device_id = entry.indigo_device_id
        current_min, current_max = ct_bounds.effective_ct_bounds(entry.options)
        # Re-widening outranks shortfall detection and needs no streak: a
        # reading outside the CURRENT bounds is its own proof the device
        # reaches there, whatever a commanded reference does or does not say
        # right now. This is what self-heals a seed or a stale learned value
        # that turns out to be too narrow.
        if mireds < current_min:
            self._adopt(entry, "min", mireds,
                        reason=f"a reading of {mireds} mired is past its {current_min}-mired "
                               "learned/seeded floor, proving the device reaches further")
            return
        if mireds > current_max:
            self._adopt(entry, "max", mireds,
                        reason=f"a reading of {mireds} mired is past its {current_max}-mired "
                               "learned/seeded ceiling, proving the device reaches further")
            return
        commanded = self._commanded.get(device_id)
        if commanded is None:
            self._pending.pop(device_id, None)
            return
        age = self._now() - commanded.at
        if age > FRESHNESS_WINDOW_SECONDS:
            # Stale: nothing recent enough to blame this reading on. This is
            # the guard against the measured trap — an Indigo-side scene
            # change (or any other unrelated edit) landing HOURS after the
            # last commanded write must never be read as a hardware clamp,
            # however closely its value happens to match a pending streak.
            self._pending.pop(device_id, None)
            return
        delta = mireds - commanded.mireds
        if abs(delta) < SHORTFALL_THRESHOLD_MIREDS:
            self._pending.pop(device_id, None)  # matched the ask: nothing to learn
            return
        # Warm shortfall (echo LOWER than the ask) proves the warm ceiling;
        # cool shortfall (echo HIGHER) proves the cool floor — see the
        # module docstring's mireds-are-reciprocal reminder if this reads
        # backwards: high mireds is WARM, low mireds is COOL.
        side = "max" if delta < 0 else "min"
        pending = self._pending.get(device_id)
        if pending is not None and pending.side == side \
                and abs(pending.mireds - mireds) <= REPEAT_TOLERANCE_MIREDS:
            if commanded.at == pending.commanded_at:
                # A duplicate callback answering the SAME dispatch that
                # started this streak, not a second confirming observation —
                # z2m (and others) publish several attributes per state
                # change, so one command can fire `deviceUpdated` more than
                # once with the identical lagged value (2026-08-24 16:40
                # incident, device 1894385558, on the 2026.27.1 build: Apple
                # re-asked at 241 mireds while the driver's Indigo state
                # still held the previous 227-mired target, and two
                # deviceUpdated callbacks off that ONE dispatch both echoed
                # the stale 227). Left pending, unchanged: two confirmations
                # must be two answers to two DISTINCT dispatches, not one
                # answer heard twice — a real hardware clamp echoes the same
                # value in response to two separate commands seconds apart
                # (the #281 storm re-asserts, so distinct dispatches keep
                # arriving); driver lag cannot do that, because by the next
                # dispatch the lagged value has moved with the ask.
                return
            self._adopt(entry, side, mireds,
                        reason=(f"two consecutive {'warm' if side == 'max' else 'cool'} "
                                f"shortfalls, answering two distinct commanded writes, both "
                                f"echoed back as {mireds}"))
            self._pending.pop(device_id, None)
            return
        # Either the first observation of a new streak, or one that
        # disagrees with the streak in progress — either way the streak
        # restarts on THIS reading; a mismatch is never averaged with what
        # came before it.
        self._pending[device_id] = _Pending(side=side, mireds=mireds, commanded_at=commanded.at)

    def _adopt(self, entry, side: str, candidate: int, *, reason: str) -> None:
        """Persist ``candidate`` as the learned bound for ``side``, if it says anything new.

        Re-reads the store FIRST and derives the current effective bounds
        from THAT — never from ``entry``, whatever bounds a caller happens to
        have computed for its own pre-check — because ``entry`` is only ever
        a snapshot, and the guard below has to be right about what is
        actually stored right now, not what it was when the caller read it.

        This is the issue #293 2026-08-24 15:39 incident, fixed at its root:
        a calibration sweep's ``adopt_measured`` reused ONE ``entry`` snapshot
        (options empty) across both the cool and warm extremes of the same
        device. The cool adoption wrote ``ctLearnedMinMireds: 215`` to the
        store; the warm call, still holding the stale empty-options snapshot,
        computed its guard against (153, 500) instead of the now-current
        (215, 500) — so a warm candidate of 215 sailed past a guard that
        should have refused it, and the write then merged the new
        ``ctLearnedMaxMireds: 215`` onto the FRESH options that already held
        ``ctLearnedMinMireds: 215``, persisting an invalid (215, 215) pair.
        Production self-healed four seconds later when a settled reading
        re-widened it back to (153, 500), but the invalid pair was genuinely
        stored in that window, and ``ExportStore.upsert`` had nothing that
        would have caught it either (see its own docstring on this point).
        Recomputing from a fresh re-read, right here, closes both gaps at
        once: no caller's bounds are trusted for the write decision, and the
        guard and the write always agree on what they are guarding.
        """
        device_id = entry.indigo_device_id
        fresh = self._store.get(device_id)
        if fresh is None:
            return  # the export was removed since this reading arrived
        current_min, current_max = ct_bounds.effective_ct_bounds(fresh.options)
        if side == "min":
            if candidate == current_min:
                return  # already the effective value — nothing to say twice
            new_min, new_max = candidate, current_max
            learned_key = ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS
        else:
            if candidate == current_max:
                return
            new_min, new_max = current_min, candidate
            learned_key = ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS
        if new_min >= new_max:
            # Bounds sanity (#293's own requirement): an adoption must never
            # collapse or invert the range. Refused rather than silently
            # widening the OTHER side to compensate — that would be this
            # module inventing evidence for a side that offered none.
            #
            # Both COLLAPSE and genuine INVERSION are reachable here now that
            # this guard runs against a FRESH re-read rather than the value
            # that produced `candidate`: a caller's own current-bounds
            # reasoning (the re-widen-vs-shortfall split in
            # `_observe_locked`, or a sweep's single-shot ask/echo pair in
            # `adopt_measured`) is computed against ITS OWN snapshot, which
            # can disagree with what the store holds by the time this runs —
            # exactly the gap the incident above exploited. A second
            # adoption landing on the OTHER side in between (two sides of
            # the same sweep, or two concurrent adoptions) can move
            # `current_min`/`current_max` past `candidate` in either
            # direction, so this is no longer provably collapse-only.
            self._logger.warning(
                "Matter export: device %s's observed colour-temperature bound (%s mireds, side "
                "%s) would make its learned range %s-%s invalid (min must be < max) — refusing "
                "to adopt it.", device_id, candidate, side, new_min, new_max)
            return
        updated = dataclasses.replace(fresh, options={**fresh.options, learned_key: candidate})
        try:
            self._store.upsert(updated)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter export: learned colour-temperature bound for device %s could not be "
                "saved — %s.", device_id, exc)
            self._logger.exception(exc)
            return
        self._logger.info(
            "Matter export: learned a new colour-temperature %s bound for device %s: %s mireds "
            "(%sK) — %s.",
            "cool" if side == "min" else "warm", device_id, candidate,
            mireds_to_kelvin(candidate), reason)
        try:
            self._republish(device_id)
        except Exception as exc:  # pylint: disable=broad-except
            # The learned value is already safely persisted; a republish
            # failure just means the fabric catches up on the next reconnect
            # or export edit, exactly like every other `upsert` failure in
            # this file's family — never a reason to lose or re-raise here.
            self._logger.error(
                "Matter export: republishing the learned %s bound for device %s could not be "
                "sent — %s. It is safely saved and will reach the fabric at the next reconnect "
                "or export edit.",
                "cool" if side == "min" else "warm", device_id, exc)
            self._logger.exception(exc)
