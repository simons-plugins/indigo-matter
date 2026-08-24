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
  guards against.
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
#: mireds to count as a candidate shortfall at all. Deliberately smaller
#: than ``export_handlers.CT_TOLERANCE_MIREDS`` (30): that constant is a
#: SUPPRESSION band (how much drift the diff path lets through as noise
#: before reporting it), and this one is a DETECTION threshold (how much of
#: a gap is worth treating as possible hardware evidence) — a shortfall as
#: small as 2 mireds is still real once it repeats twice, and using the
#: larger constant here would make the learner blind to exactly the
#: shortfalls smaller than 30 that ADR-0013's tolerance already hides from
#: the ordinary diff.
SHORTFALL_THRESHOLD_MIREDS = 2

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

        Deliberately thin: it computes the two inputs :meth:`_adopt` needs
        that :meth:`_observe_locked` would otherwise compute for it
        (``current_min``/``current_max``) and then funnels straight into
        :meth:`_adopt` under the SAME lock — so a sweep's measurement gets
        the identical collapse-refusal guard, re-read-before-write, one INFO
        log, and republish that passive shortfall/re-widen evidence already
        gets. No second adoption path is written; that would be exactly the
        "one fact said twice" this module's own docstring warns against.

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
            current_min, current_max = ct_bounds.effective_ct_bounds(entry.options)
            self._adopt(entry, side, int(mireds), current_min, current_max, reason=reason)

    def _observe_locked(self, entry, mireds: int) -> None:
        device_id = entry.indigo_device_id
        current_min, current_max = ct_bounds.effective_ct_bounds(entry.options)
        # Re-widening outranks shortfall detection and needs no streak: a
        # reading outside the CURRENT bounds is its own proof the device
        # reaches there, whatever a commanded reference does or does not say
        # right now. This is what self-heals a seed or a stale learned value
        # that turns out to be too narrow.
        if mireds < current_min:
            self._adopt(entry, "min", mireds, current_min, current_max,
                        reason=f"a reading of {mireds} mired is past its {current_min}-mired "
                               "learned/seeded floor, proving the device reaches further")
            return
        if mireds > current_max:
            self._adopt(entry, "max", mireds, current_min, current_max,
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
            self._adopt(entry, side, mireds, current_min, current_max,
                        reason=(f"two consecutive {'warm' if side == 'max' else 'cool'} "
                                f"shortfalls confirmed a commanded {commanded.mireds}-mired "
                                f"write echoed back as {mireds}"))
            self._pending.pop(device_id, None)
            return
        # Either the first observation of a new streak, or one that
        # disagrees with the streak in progress — either way the streak
        # restarts on THIS reading; a mismatch is never averaged with what
        # came before it.
        self._pending[device_id] = _Pending(side=side, mireds=mireds)

    def _adopt(self, entry, side: str, candidate: int, current_min: int, current_max: int,
              *, reason: str) -> None:
        """Persist ``candidate`` as the learned bound for ``side``, if it says anything new."""
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
            # In practice only COLLAPSE (equality) is reachable through
            # `observe`: the re-widen branch above intercepts anything
            # outside the current bounds before this shortfall path ever
            # runs, so `candidate` here is always within
            # `[current_min, current_max]`, and replacing one side with it
            # can shrink the range to a point but never invert it. The `>=`
            # check still covers strict inversion too, on the chance a
            # future caller of `_adopt` is less constrained than this one.
            self._logger.warning(
                "Matter export: device %s's observed colour-temperature bound (%s mireds, side "
                "%s) would make its learned range %s-%s invalid (min must be < max) — refusing "
                "to adopt it.", entry.indigo_device_id, candidate, side, new_min, new_max)
            return
        device_id = entry.indigo_device_id
        # Re-read immediately before writing: `entry` may be the caller's
        # snapshot from moments ago, and another thread's `store.upsert`
        # (a role change, a name edit, a concurrent adoption on the other
        # side) landing in between must not be clobbered by a stale copy.
        fresh = self._store.get(device_id)
        if fresh is None:
            return  # the export was removed since this reading arrived
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
