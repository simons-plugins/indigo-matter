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


def test_two_consistent_shortfalls_adopt_the_warm_bound(learner, store, republish):
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    updated = store.get(800)
    assert updated.options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    republish.assert_called_once_with(800)


def test_two_consistent_shortfalls_adopt_the_cool_bound(learner, store, republish):
    learner.record_commanded(800, 200)
    learner.observe(_entry(store), 230)  # cool shortfall: echo HIGHER than the ask
    learner.observe(_entry(store), 230)
    updated = store.get(800)
    assert updated.options[ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS] == 230
    republish.assert_called_once_with(800)


def test_a_repeat_within_tolerance_still_counts_as_the_same_value(learner, store):
    """Round-trip noise of +/-1 mired must not defeat the "same value twice" test."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 401)
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 401


def test_inconsistent_echo_values_do_not_adopt(learner, store, republish):
    """Two DIFFERENT shortfall values must restart the streak, not average it —
    an adoption on disagreeing evidence would invent a bound nothing proved."""
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 390)  # a different clamp value: restarts, does not adopt
    assert store.upserts == []
    republish.assert_not_called()
    # The restarted streak still adopts on ITS OWN second confirmation.
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
    threshold is 2, deliberately narrower than the diff path's own 30-mired
    suppression tolerance."""
    learner.record_commanded(800, 401)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    assert store.upserts == []


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
        learner, store, republish):
    """Bounds sanity (#293): an adoption may never produce ``min >= max``.

    A genuine STRICT inversion cannot arise from ``observe`` at all — the
    re-widen check above intercepts anything outside the current bounds
    before the shortfall path ever runs, so a shortfall candidate is always
    within ``[current_min, current_max]``, and replacing one side with it can
    at worst COLLAPSE the range to a point, never invert it. This is the
    reachable case, and the one the sanity check exists to catch.
    """
    store.upsert(_Entry(800, options={ct_bounds.OPTION_CT_MIN_MIREDS: 200,
                                      ct_bounds.OPTION_CT_MAX_MIREDS: 400}))
    store.upserts.clear()
    learner.observe(_entry(store), 400)  # NOT outside [200,400] — exactly at max, so no re-widen
    # Force the collapse case through the shortfall path instead: candidate
    # equal to the OTHER side.
    learner.record_commanded(800, 200)
    learner.observe(_entry(store), 400)  # cool shortfall candidate == current max
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
    learner.observe(_entry(store), 400)  # must not raise
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    logger.exception.assert_called()


def test_an_entry_removed_before_adoption_is_not_resurrected(learner, store, republish):
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    store._entries.pop(800)  # the export was removed between the two observations
    learner.observe(_Entry(800), 400)
    assert store.upserts == []
    republish.assert_not_called()


def test_adoption_logs_one_clear_info_line_naming_mireds_kelvin_and_reason(
        learner, store, republish):
    logger = Mock()
    learner._logger = logger
    learner.record_commanded(800, 426)
    learner.observe(_entry(store), 400)
    learner.observe(_entry(store), 400)
    logger.info.assert_called_once()
    message = logger.info.call_args[0][0] % logger.info.call_args[0][1:]
    assert "400" in message and "mireds" in message
    assert "2500" in message  # mireds_to_kelvin(400) == 2500K
