"""The observed-clamp physical CT bounds learner (issue #293).

No Indigo import (matching ``export_store``/``ct_bounds``): a fake store and
a controllable clock are enough to pin the whole state machine, including
the degradation-path cases the workspace convention requires — a stale
commanded reference, a single observation, and inconsistent echoes must all
refuse to adopt, exactly as loudly as a real adoption succeeds.
"""
from __future__ import annotations

import dataclasses
from unittest.mock import Mock

import pytest

import ct_bounds
import ct_learner
import export_store


@dataclasses.dataclass(frozen=True)
class _Entry:
    """A minimal duck-typed export entry — everything the learner reads."""
    indigo_device_id: int
    role: str = "colorTemperatureLight"
    options: dict = dataclasses.field(default_factory=dict)


class FakeStore:
    """`.get`/`.upsert` over a plain dict, mirroring `ExportStore`'s shape."""

    def __init__(self, entries=()):
        self._entries = {e.indigo_device_id: e for e in entries}
        self.upserts: list = []
        self.raise_on_upsert: Exception | None = None

    def get(self, device_id):
        return self._entries.get(device_id)

    def upsert(self, entry):
        if self.raise_on_upsert is not None:
            raise self.raise_on_upsert
        self._entries[entry.indigo_device_id] = entry
        self.upserts.append(entry)
        return entry


class Clock:
    """A settable monotonic clock the tests advance by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store():
    return FakeStore([_Entry(800)])


@pytest.fixture
def republish():
    return Mock()


@pytest.fixture
def learner(store, republish, clock):
    return ct_learner.CTBoundsLearner(store, Mock(), republish, now=clock)


def _entry(store, device_id=800):
    return store.get(device_id)


# ---------------------------------------------------------------------------
# Role gating
# ---------------------------------------------------------------------------
def test_a_non_ct_role_is_ignored_entirely(learner, store, republish):
    entry = _Entry(800, role="onOffLight")
    learner.observe(entry, 400)
    assert store.upserts == []
    republish.assert_not_called()


# ---------------------------------------------------------------------------
# Shortfall adoption — the core two-consecutive-observations mechanism
# ---------------------------------------------------------------------------
def test_a_single_shortfall_does_not_adopt(learner, store, republish):
    """One observation is a candidate, not evidence — a transient (a driver
    hiccup, a read mid-transition) must not become a learned bound."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)  # warm shortfall
    assert store.upserts == []
    republish.assert_not_called()


def test_two_consistent_shortfalls_adopt_the_warm_bound(learner, store, republish, clock):
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)
    updated = store.get(800)
    assert updated.options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    republish.assert_called_once_with(800)


def test_two_consistent_shortfalls_adopt_the_cool_bound(learner, store, republish, clock):
    learner.record_commanded(800, 200)
    learner.observe(_entry(store), 230)  # cool shortfall: echo HIGHER than the ask
    clock.advance(2.0)
    learner.record_commanded(800, 200)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 230)
    updated = store.get(800)
    assert updated.options[ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS] == 230
    republish.assert_called_once_with(800)


def test_a_repeat_within_tolerance_still_counts_as_the_same_value(learner, store, clock):
    """Round-trip noise of +/-1 mired must not defeat the "same value twice" test."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 401)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 401


def test_inconsistent_echo_values_do_not_adopt(learner, store, republish, clock):
    """Two DIFFERENT shortfall values must restart the streak, not average it —
    an adoption on disagreeing evidence would invent a bound nothing proved."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 390)  # a different clamp value: restarts, does not adopt
    assert store.upserts == []
    republish.assert_not_called()
    # The restarted streak still adopts on ITS OWN second confirmation, from
    # a second, distinct dispatch answering the same ask.
    clock.advance(2.0)
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 390)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 390


def test_an_echo_matching_the_command_is_not_a_shortfall(learner, store, republish):
    learner.record_commanded(800, 400)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []
    republish.assert_not_called()


def test_a_shortfall_below_the_detection_threshold_is_not_a_candidate(learner, store):
    """A 1-mired gap is conversion noise, not evidence — the detection
    threshold is 10, well clear of round-trip noise this small."""
    learner.record_commanded(800, 401)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []


def test_a_sub_tolerance_clamp_is_deliberately_not_learned(learner, store, republish):
    """A genuine 8-mired clamp is real, repeatable evidence — but it is
    smaller than ``export_handlers.CT_TOLERANCE_MIREDS`` (30), so ADR-0013's
    own diff tolerance already absorbs it silently and no published bound is
    needed at all. This is the deliberate reason the detection threshold
    lives at 10, not just above measured transients (see the module's own
    ``SHORTFALL_THRESHOLD_MIREDS`` docstring): a clamp this small has no
    reason to ever become a learned bound."""
    learner.record_commanded(800, 408)
    learner.observe(_entry(store), 400)  # 8-mired warm shortfall, repeatable
    learner.observe(_entry(store), 400)
    assert store.upserts == []
    republish.assert_not_called()


def test_the_2026_08_24_transient_echo_incident_does_not_adopt(learner, store, republish):
    """The live incident that forced the threshold from 2 to 10 (2026-08-24
    15:07, device 1894385558): Apple adaptive lighting commanded 206 mireds
    to a MiBoxer FUT035Z+ strip, and the z2m driver echoed a mid-transition/
    quantized 202 TWICE inside the freshness window — a 4-mired gap that
    used to clear the old threshold (2) and get adopted as a false warm
    bound, before the settled 206 reading re-widened it back moments later
    (75ms of pure store-write/republish churn for nothing learned). At the
    new threshold (10), a 4-mired gap is never even a candidate."""
    learner.record_commanded(1894385558, 206)
    learner.observe(_Entry(1894385558), 202)
    learner.observe(_Entry(1894385558), 202)
    assert store.upserts == []
    republish.assert_not_called()


# ---------------------------------------------------------------------------
# Two distinct dispatches, not one dispatch heard twice — the 2026-08-24
# 16:40 incident (#293, device 1894385558, 2026.27.1 build).
# ---------------------------------------------------------------------------
def test_the_2026_08_24_1640_duplicate_callback_incident_does_not_adopt(
        learner, store, republish):
    """The live incident this fix exists for, pinned verbatim: Apple's
    adaptive lighting stepped its ask to 241 mireds while the z2m driver's
    Indigo state still held the PREVIOUS 227-mired target (a 14-mired
    adaptive step, clearing the raised 10-mired threshold). z2m publishes
    several attributes per state change, so ``deviceUpdated`` fired MORE
    THAN ONCE for that ONE dispatch, both times with the same lagged 227 —
    and under the old rule those duplicate callbacks alone satisfied "two
    consecutive observations". They must not: only a SECOND, distinct
    dispatch's matching echo may complete the streak."""
    learner.record_commanded(1894385558, 241)
    learner.observe(_Entry(1894385558), 227)  # first callback off the one dispatch
    learner.observe(_Entry(1894385558), 227)  # a duplicate callback off the SAME dispatch
    assert store.upserts == []
    republish.assert_not_called()


def test_the_pr295_review_late_duplicate_race_does_not_adopt_early(
        learner, store, republish, clock):
    """PR #295 review (2026-08-24) — the race variant of the 16:40 incident
    above. The distinct-dispatch rule compares the LIVE ``commanded.at``
    against the streak's own reference, so a DELAYED duplicate echo of
    dispatch N, delivered after a same-valued dispatch N+1 has already been
    recorded, reads its ``commanded.at`` as N+1's and looks like "a
    different dispatch" even though it is still an echo of N — timing alone
    cannot prove which dispatch it is answering. A minimum confirmation gap
    closes it: a duplicate that clears the distinct-dispatch check but
    arrives too soon after the streak started is treated exactly like a
    same-dispatch duplicate — inert, and the streak survives unchanged for a
    later, genuine confirmation to complete."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)  # starts the streak at T
    clock.advance(1.0)
    learner.record_commanded(800, 426)  # dispatch N+1 recorded
    clock.advance(0.05)  # the late duplicate of dispatch N arrives moments later
    learner.observe(_entry(store), 400)  # distinct commanded.at, but only 1.05s into the streak
    assert store.upserts == []
    republish.assert_not_called()

    clock.advance(3.0)  # a genuine confirmation, riding the storm's re-assert cadence
    learner.record_commanded(800, 426)  # a further, distinct dispatch
    learner.observe(_entry(store), 400)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    republish.assert_called_once_with(800)


def test_a_real_clamp_still_learns_across_two_distinct_dispatches(learner, store, republish, clock):
    """THE discriminator (#293): a real hardware clamp echoes the SAME value
    in answer to TWO DISTINCT dispatches seconds apart (the #281 storm
    re-asserts, so a real command keeps being re-issued) — driver lag cannot
    do that, because by the next dispatch the lagged value has moved with
    the ask. Two separate commands, each answered with a matching shortfall,
    must still adopt."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)  # "seconds apart" — a distinct, later dispatch
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    republish.assert_called_once_with(800)


def test_lag_across_distinct_dispatches_never_completes_a_streak(learner, store, republish, clock):
    """A lagging driver's echo tracks the PREVIOUS ask, not the current one —
    so as Apple's adaptive lighting keeps stepping its command, each new
    dispatch's echo disagrees with the streak the last one started, and the
    streak keeps resetting instead of ever completing."""
    learner.record_commanded(1894385558, 206)
    learner.observe(_Entry(1894385558), 202)  # lagged echo of an earlier ask; starts a streak
    clock.advance(1.0)
    learner.record_commanded(1894385558, 241)  # a new, distinct dispatch — the ask moved on
    learner.observe(_Entry(1894385558), 227)  # lagged echo of THIS ask — disagrees, resets
    assert store.upserts == []
    republish.assert_not_called()


def test_duplicate_callbacks_are_inert_until_a_second_dispatch_confirms(
        learner, store, republish, clock):
    """Any number of duplicate callbacks off the SAME dispatch leave the
    streak pending, unchanged — never completing and never resetting it —
    until a second, distinct dispatch's matching echo arrives."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)  # starts the streak
    for _ in range(5):
        learner.observe(_entry(store), 400)  # duplicate callbacks off the same dispatch
    assert store.upserts == []
    republish.assert_not_called()

    clock.advance(2.0)
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    republish.assert_called_once_with(800)


def test_adoption_no_ops_when_the_value_already_equals_the_learned_one(
        learner, store, republish):
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    store.upserts.clear()  # the setup write above is not what this test asserts on
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []  # no redundant persist
    republish.assert_not_called()


# ---------------------------------------------------------------------------
# The freshness window — the degradation-path requirement
# ---------------------------------------------------------------------------
def test_no_commanded_reference_at_all_is_ignored(learner, store, republish):
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []
    republish.assert_not_called()


def test_a_stale_commanded_reference_is_never_read_as_a_clamp(learner, store, republish, clock):
    """THE degradation-path pin: an Indigo-side scene change (or anything
    else) landing on a value that coincidentally matches an old commanded
    write, long after it was issued, must never be mistaken for hardware
    evidence — however many times it repeats."""
    learner.record_commanded(800, 426)
    clock.advance(3600)  # an hour later — nothing to do with that command
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []
    republish.assert_not_called()


def test_a_reading_just_inside_the_window_still_counts(learner, store, republish, clock):
    learner.record_commanded(800, 426)
    clock.advance(14.0)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)  # clear MIN_CONFIRMATION_GAP_SECONDS between the two observations
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400


def test_a_reading_just_outside_the_window_is_ignored(learner, store, republish, clock):
    learner.record_commanded(800, 426)
    clock.advance(15.01)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []


def test_a_pending_streak_does_not_survive_the_reference_going_stale(
        learner, store, republish, clock):
    """The first observation starts a streak; the SECOND arrives after the
    window has expired relative to the (unchanged) commanded reference — it
    must not complete the streak."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)   # pending: max, 400
    clock.advance(20)
    learner.observe(_entry(store), 400)   # stale now — must not adopt
    assert store.upserts == []


# ---------------------------------------------------------------------------
# Re-widening — self-healing a wrong seed or a wrong learned value
# ---------------------------------------------------------------------------
def test_a_reading_past_the_seeded_ceiling_immediately_re_widens(learner, store, republish):
    """No streak needed: the device just proved it reaches further than the
    seed claimed, which is its own evidence."""
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_MIN_MIREDS: 153,
                                      ct_bounds.OPTION_CT_MAX_MIREDS: 400}))
    learner.observe(_entry(store), 450)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 450
    republish.assert_called_once_with(800)


def test_a_reading_past_the_seeded_floor_immediately_re_widens(learner, store, republish):
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_MIN_MIREDS: 250,
                                      ct_bounds.OPTION_CT_MAX_MIREDS: 500}))
    learner.observe(_entry(store), 200)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS] == 200
    republish.assert_called_once_with(800)


def test_re_widening_does_not_require_a_commanded_reference(learner, store, republish):
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_MAX_MIREDS: 400,
                                      ct_bounds.OPTION_CT_MIN_MIREDS: 153}))
    learner.observe(_entry(store), 450)  # no record_commanded call at all
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 450


def test_re_widening_a_learned_bound_that_already_matches_is_a_no_op(
        learner, store, republish):
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS: 450}))
    store.upserts.clear()
    learner.observe(_entry(store), 450)  # exactly at the current effective bound
    assert store.upserts == []
    republish.assert_not_called()


# ---------------------------------------------------------------------------
# Bounds sanity — an adoption may never invert or collapse the range
# ---------------------------------------------------------------------------
def test_an_adoption_that_would_collapse_the_range_to_a_point_is_refused(
        learner, store, republish, clock):
    """Bounds sanity (#293): an adoption may never produce ``min >= max``.

    This test pins the COLLAPSE case: a candidate equal to the OTHER side,
    reached through the shortfall path here. ``_adopt``'s guard now catches
    genuine INVERSION too, not just collapse (see its own docstring) — it
    re-reads the store FRESH rather than trusting a caller's own current-
    bounds reasoning, and a second adoption landing on the OTHER side in
    between (two sides of the same sweep, or two concurrent adoptions) can
    move ``current_min``/``current_max`` past a caller's ``candidate`` in
    either direction, so the guard is no longer provably collapse-only —
    it just happens to be a collapse in THIS scenario.
    """
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_MIN_MIREDS: 200,
                                      ct_bounds.OPTION_CT_MAX_MIREDS: 400}))
    store.upserts.clear()
    learner.observe(_entry(store), 400)  # NOT outside [200,400] — exactly at max, so no re-widen
    # Force the collapse case through the shortfall path instead: candidate
    # equal to the OTHER side.
    learner.record_commanded(800, 200)
    learner.observe(_entry(store), 400)  # cool shortfall candidate == current max
    clock.advance(2.0)
    learner.record_commanded(800, 200)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)
    assert store.upserts == []
    learner._logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# Persistence / republish plumbing
# ---------------------------------------------------------------------------
def test_a_store_write_failure_is_logged_and_does_not_raise(store, republish, clock):
    logger = Mock()
    learner = ct_learner.CTBoundsLearner(store, logger, republish, now=clock)
    store.raise_on_upsert = RuntimeError("prefs write failed")
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)  # must not raise
    logger.error.assert_called()
    republish.assert_not_called()


def test_a_republish_failure_does_not_raise_and_the_learned_value_stays_saved(
        store, clock):
    logger = Mock()
    republish = Mock(side_effect=RuntimeError("bridge not live"))
    learner = ct_learner.CTBoundsLearner(store, logger, republish, now=clock)
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)  # must not raise
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    logger.exception.assert_called()
    # Issue #294 review — the contextual line ahead of `logger.exception`
    # must actually name the device: a bare `logger.exception(exc)` with no
    # context is useless once more than one device is exporting.
    logger.error.assert_called_once()
    error_message = logger.error.call_args[0][0] % logger.error.call_args[0][1:]
    assert "800" in error_message


# ---------------------------------------------------------------------------
# forget() — dropping per-device learner state on export removal (#294 review)
# ---------------------------------------------------------------------------
def test_forget_drops_the_pending_streak_so_a_stale_echo_cannot_complete_it(
        learner, store, republish, clock):
    """A device un-exported mid-streak, then re-exported (or its id reused by
    a deleted-and-recreated Indigo device), must start with a clean slate —
    not adopt on a streak that has nothing to do with the new hardware.

    Two DISTINCT dispatches, with the clock cleared past
    ``MIN_CONFIRMATION_GAP_SECONDS`` between them: without ``forget()`` this
    exact sequence completes the streak and adopts (see
    ``test_a_real_clamp_still_learns_across_two_distinct_dispatches``) — a
    one-dispatch version of this test would pass even with a no-op
    ``forget()``, now that completion always requires two distinct
    dispatches, so it would no longer be testing ``forget`` at all.
    """
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)  # pending: max, 400, streak starts at T
    clock.advance(2.0)  # clear MIN_CONFIRMATION_GAP_SECONDS
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.forget(800)  # the export is removed mid-streak, between the two dispatches
    learner.observe(_entry(store), 400)  # would complete the OLD streak if not forgotten
    assert store.upserts == []
    republish.assert_not_called()


def test_forget_drops_the_commanded_reference_too(learner, store, republish, clock):
    learner.record_commanded(800, 426)
    learner.forget(800)
    # No commanded reference survives `forget` — a shortfall-shaped reading
    # now has nothing to be a shortfall AGAINST, so even two consecutive
    # matching echoes must not adopt.
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []
    republish.assert_not_called()


def test_forget_on_an_unknown_device_does_not_raise(learner):
    learner.forget(999999)  # never seen — must be a quiet no-op


def test_a_reading_outside_the_generic_domain_that_the_store_refuses_does_not_crash_or_adopt(
        store, clock):
    """Degradation path (#294 review): a raw reading outside 153-500 mireds
    (a driver glitch, not a real clamp) still passes `_adopt`'s own
    min-vs-max sanity check — it only compares the two SIDES, never the
    §4.2 domain — and reaches `self._store.upsert`. The REAL `export_store`
    is the one place that domain is enforced; simulate its refusal the same
    way `test_a_store_write_failure_is_logged_and_does_not_raise` does,
    to pin that `_adopt`'s guarded upsert survives a validation refusal
    exactly like it survives any other store failure: no crash, nothing
    persisted, the save-failure error logged.
    """
    logger = Mock()
    store.raise_on_upsert = ValueError("ctLearnedMinMireds option (90) is outside the 153-500 "
                                        "mired domain")
    learner = ct_learner.CTBoundsLearner(store, logger, republish=Mock(), now=clock)
    # No seed on the entry, so the effective floor is the generic 153 —
    # 90 sits below it, so this is a re-widen candidate, not a shortfall.
    learner.observe(_entry(store), 90)  # must not raise
    assert store.upserts == []
    logger.error.assert_called_once()


def test_a_reading_outside_the_domain_is_refused_by_the_real_store_end_to_end(clock):
    """The companion to the test above, run against the REAL
    ``export_store.ExportStore`` rather than a simulated refusal — pinning
    the defence-in-depth chain issue #293's follow-up (a7b80b0) actually
    built: ``_adopt``'s own min-vs-max guard only compares the two SIDES,
    never the §4.2 domain, so a raw out-of-domain reading (a driver glitch,
    not a real clamp) sails past it and reaches ``ExportStore.upsert`` —
    which is the one place that domain IS enforced (issue #294 review). No
    crash, nothing persisted, and the save-failure error names the reason.
    """
    logger = Mock()
    prefs: dict = {}
    real_store = export_store.ExportStore(lambda: prefs, logger)
    # Seeded with no options: generic (153, 500) bounds.
    real_store.upsert(export_store.ExportEntry(800, "colorTemperatureLight"))
    learner = ct_learner.CTBoundsLearner(real_store, logger, republish=Mock(), now=clock)
    # No seed on the entry, so the effective floor is the generic 153 —
    # 90 sits below it, so this is a re-widen candidate, not a shortfall.
    learner.observe(_Entry(800), 90)  # must not raise
    assert real_store.get(800).options == {}, "nothing was persisted from the refused write"
    logger.error.assert_called_once()
    error_message = logger.error.call_args[0][0] % logger.error.call_args[0][1:]
    assert "800" in error_message and "could not be saved" in error_message


def test_an_entry_removed_before_adoption_is_not_resurrected(learner, store, republish, clock):
    """Two DISTINCT dispatches, the clock advanced past the confirmation gap,
    so the streak genuinely COMPLETES and reaches ``_adopt`` — not merely
    "never completes at all", which would pass this assertion for the wrong
    reason (issue #293 review: the original version of this test predates
    the two-distinct-dispatch rule and never actually exercised ``_adopt``'s
    own ``fresh is None`` guard). The debug log that guard now leaves is the
    proof ``_adopt`` genuinely ran."""
    logger = Mock()
    learner._logger = logger
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)  # starts the streak
    clock.advance(2.0)  # clear MIN_CONFIRMATION_GAP_SECONDS
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    store._entries.pop(800)  # the export was removed between the two observations
    learner.observe(_Entry(800), 400)  # completes the streak -> reaches `_adopt`
    assert store.upserts == []
    republish.assert_not_called()
    logger.debug.assert_called_once()
    message = logger.debug.call_args[0][0] % logger.debug.call_args[0][1:]
    assert "800" in message and "400" in message


def test_adoption_logs_one_clear_info_line_naming_mireds_kelvin_and_reason(
        learner, store, republish, clock):
    logger = Mock()
    learner._logger = logger
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    clock.advance(2.0)
    learner.record_commanded(800, 426)  # a second, distinct dispatch of the same ask
    learner.observe(_entry(store), 400)
    logger.info.assert_called_once()
    message = logger.info.call_args[0][0] % logger.info.call_args[0][1:]
    assert "400" in message and "mireds" in message
    assert "2500" in message  # mireds_to_kelvin(400) == 2500K


# ---------------------------------------------------------------------------
# adopt_measured — the calibration action's own path into the SAME adoption
# machinery (issue #293's ADR-0014 Option C extension, ct_calibration.py)
# ---------------------------------------------------------------------------
def test_adopt_measured_persists_through_the_same_adopt_path(learner, store, republish):
    learner.adopt_measured(_entry(store), "min", 200, reason="calibration sweep")
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS] == 200
    republish.assert_called_once_with(800)


def test_adopt_measured_ignores_a_non_ct_role(store, republish):
    learner_mod = ct_learner
    non_ct_learner = learner_mod.CTBoundsLearner(store, Mock(), republish)
    entry = _Entry(800, role="onOffLight")
    non_ct_learner.adopt_measured(entry, "min", 200, reason="calibration sweep")
    assert store.upserts == []
    republish.assert_not_called()


def test_adopt_measured_needs_no_commanded_reference_or_streak(learner, store, republish):
    """Unlike ``observe``'s shortfall path, one call is enough — the sweep
    IS its own single-shot reference (ct_calibration.py's own docstring)."""
    assert learner._commanded == {}
    assert learner._pending == {}
    learner.adopt_measured(_entry(store), "max", 400, reason="calibration sweep")
    assert store.upserts, "a single adopt_measured call must adopt immediately"
    assert learner._commanded == {}  # untouched — this path never records one
    assert learner._pending == {}


def test_adopt_measured_collapse_refusal_reuses_adopt_s_own_guard(learner, store, republish):
    """Same guard ``observe``'s shortfall path already relies on
    (``test_an_adoption_that_would_collapse_the_range_to_a_point_is_refused``):
    a measured side that would make ``min >= max`` is refused, warned, and
    never persisted or republished."""
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_MIN_MIREDS: 200,
                                      ct_bounds.OPTION_CT_MAX_MIREDS: 400}))
    store.upserts.clear()
    learner.adopt_measured(_entry(store), "min", 400, reason="calibration sweep")  # == current max
    assert store.upserts == []
    republish.assert_not_called()
    learner._logger.warning.assert_called_once()


# ---------------------------------------------------------------------------
# The 2026-08-24 15:39 incident — a stale caller snapshot must never gate
# the write. `_adopt` re-reads the store itself for its guard; the `entry`
# a caller passes in is used only for `indigo_device_id`/role, never trusted
# for what "current" means (see `_adopt`'s own docstring for the mechanism).
# ---------------------------------------------------------------------------
def test_the_2026_08_24_15_39_incident_is_refused_via_the_fresh_re_read(
        learner, store, republish):
    """THE incident this fix exists for, pinned verbatim (issue #293,
    2026-08-24 15:39, device 1894385558 — a MiBoxer strip): a calibration
    sweep's cool-extreme adoption already wrote ``ctLearnedMinMireds: 215``
    to the store, but the SAME sweep's warm-extreme call still holds its own
    ``entry`` snapshot from BEFORE the sweep started — options still empty,
    so its own idea of the current bounds is the unlearned (153, 500).
    Before this fix, ``_adopt``'s collapse guard trusted that stale snapshot
    and let a warm candidate of 215 sail straight past it, merging onto the
    FRESH options that already held min=215 and persisting an invalid
    (215, 215) pair. The guard must refuse it instead."""
    stale_entry = _Entry(800)  # options={} — the sweep's own pre-loop snapshot
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 215}))
    store.upserts.clear()  # the setup write above is not what this test asserts on

    learner.adopt_measured(stale_entry, "max", 215, reason="calibration sweep")

    assert store.upserts == [], "no invalid (215, 215) pair was ever persisted"
    assert store.get(800).options == {ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 215}, \
        "the store is left exactly as valid as it was — a lone learned min"
    republish.assert_not_called()
    learner._logger.warning.assert_called_once()


def test_observe_with_a_stale_snapshot_is_also_refused_via_the_fresh_re_read(
        learner, store, republish, clock):
    """The mirrored case through :meth:`observe`/:meth:`_observe_locked`:
    that path takes its ``entry`` from whatever the CALLER last read (its own
    docstring says so), so a caller that reuses one stale snapshot across two
    calls — exactly the shape the incident above shows for a multi-side
    sweep — can build a shortfall streak whose OWN current-bounds reasoning
    (153, 500) disagrees with what the store now holds (215, 500). The
    streak's completion still has to go through ``_adopt``'s fresh re-read,
    which is what actually refuses the resulting collapse."""
    stale_entry = _Entry(800)  # options={} — read before the store gained a learned min
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 215}))
    store.upserts.clear()
    learner.record_commanded(800, 500)   # asked for the warm extreme
    learner.observe(stale_entry, 215)    # warm shortfall candidate, by the STALE (153, 500)
    clock.advance(2.0)
    learner.record_commanded(800, 500)   # a second, distinct dispatch of the same ask
    learner.observe(stale_entry, 215)    # second, matching echo -> completes the streak

    assert store.upserts == []
    assert store.get(800).options == {ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 215}
    republish.assert_not_called()
    learner._logger.warning.assert_called_once()
