"""The explicit "Calibrate Colour-Temperature Bounds" action engine (issue
#293's calibration extension; ADR-0014 Option C, shipped as an operator-
invoked action rather than the rejected automatic mechanism).

Dispatches go through the REAL ``export_handlers`` code path (``indigo`` is
mocked at the module boundary, the same discipline ``test_export_bridge.py``
uses for its ``bridge_mod`` fixture) so issue #281's off-lamp safety and
no-white-channel refusal are exercised for real, not re-implemented as test
doubles. Bounds are persisted through a REAL ``ct_learner.CTBoundsLearner``
against a fake store, matching the degradation-path convention: an
adversarial "when could this be wrong" test per promise, not just a happy
path.
"""
from __future__ import annotations

import dataclasses
import importlib
from unittest.mock import Mock

import pytest

from fakes import DimmerDevice


@dataclasses.dataclass
class _Entry:
    """A minimal duck-typed export entry — matches ``ExportStore``'s shape
    (``ct_learner``'s own test fake, mirrored here rather than imported —
    each test module keeps its own small fakes, per this repo's convention)."""
    indigo_device_id: int
    role: str = "colorTemperatureLight"
    options: dict = dataclasses.field(default_factory=dict)


class FakeStore:
    """``.get``/``.upsert``/``.all`` over a plain dict, mirroring ``ExportStore``."""

    def __init__(self, entries=()):
        self._entries = {e.indigo_device_id: e for e in entries}
        self.upserts: list = []

    def get(self, device_id):
        return self._entries.get(int(device_id))

    def upsert(self, entry):
        self._entries[entry.indigo_device_id] = entry
        self.upserts.append(entry)
        return entry

    def all(self):
        return tuple(self._entries.values())


class ScriptedDevices:
    """``device_getter`` double: one live device object per id, with a queue
    of Kelvin values applied on the SECOND-and-later call for that id.

    The first call for a device always returns it UNCHANGED — that is the
    engine's own "read the original CT before touching anything" call — so
    only calls after that simulate a driver's echo settling to a new value.
    """

    def __init__(self):
        self._devices: dict[int, object] = {}
        self._echoes: dict[int, list] = {}
        self._calls: dict[int, int] = {}

    def add(self, dev, echoes_kelvin=()):
        self._devices[dev.id] = dev
        self._echoes[dev.id] = list(echoes_kelvin)
        self._calls[dev.id] = 0
        return dev

    def remove(self, device_id):
        self._devices.pop(device_id, None)

    def calls_for(self, device_id) -> int:
        return self._calls.get(device_id, 0)

    def __call__(self, device_id):
        device_id = int(device_id)
        dev = self._devices.get(device_id)
        if dev is None:
            return None
        self._calls[device_id] = self._calls.get(device_id, 0) + 1
        if self._calls[device_id] > 1 and self._echoes.get(device_id):
            dev.whiteTemperature = self._echoes[device_id].pop(0)
        return dev


def _inline(run) -> None:
    """``thread_starter`` stand-in that runs the sweep on the calling thread
    — deterministic, no real background thread to join or race."""
    run()


@pytest.fixture
def calibration_mod(mock_indigo_base):
    """``ct_calibration`` (and the ``export_handlers`` it dispatches through)
    bound to a freshly mocked ``indigo`` — same discipline as
    ``test_export_bridge.py``'s ``bridge_mod`` fixture."""
    import export_handlers
    import ct_calibration as module
    importlib.reload(export_handlers)
    importlib.reload(module)
    return module


@pytest.fixture
def ct_learner_mod(calibration_mod):  # noqa: ARG001 - ordering: reload export_handlers first
    import ct_learner
    return ct_learner


@pytest.fixture
def logger():
    return Mock()


@pytest.fixture
def republish():
    return Mock()


def _mireds_to_kelvin(mireds: int) -> int:
    return round(1_000_000 / mireds)


def _engine(calibration_mod, store, devices, learner, logger, sleep=None):
    return calibration_mod.CTCalibrationEngine(
        store, devices, learner, logger,
        sleep=sleep or Mock(), thread_starter=_inline)


# ---------------------------------------------------------------------------
# 1. Off lamp, echo clamps both ends
# ---------------------------------------------------------------------------
def test_off_lamp_both_clamps_learned_republished_and_restored(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):
    original_mireds = 300
    store = FakeStore([_Entry(800)])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    dev = devices.add(
        DimmerDevice(dev_id=800, name="Test Lamp", onState=False, brightness=0,
                     whiteLevel=60, whiteTemperature=_mireds_to_kelvin(original_mireds)),
        echoes_kelvin=[_mireds_to_kelvin(200), _mireds_to_kelvin(400)])
    engine = _engine(calibration_mod, store, devices, learner, logger)

    assert engine.start(device_ids=[800], skip_lit=True) is True

    entry = store.get(800)
    import ct_bounds
    assert entry.options[ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS] == 200
    assert entry.options[ct_bounds.OPTION_CT_LEARNED_MAX_MIREDS] == 400
    assert republish.call_count == 2
    assert republish.call_args_list[0].args == (800,)
    # Lamp state (on/off) is never touched — issue #281's off-lamp-safe write.
    assert not mock_indigo_base.device.turnOn.called
    assert not mock_indigo_base.device.turnOff.called
    # Restored via the SAME dispatch path, as the LAST setColorLevels call.
    last_call = mock_indigo_base.dimmer.setColorLevels.call_args_list[-1]
    assert last_call.args[0] is dev
    assert last_call.kwargs["whiteTemperature"] == _mireds_to_kelvin(original_mireds)


# ---------------------------------------------------------------------------
# 2. Ask == echo -> no evidence, existing learned value left untouched
# ---------------------------------------------------------------------------
def test_echo_matching_the_ask_adopts_nothing_and_leaves_existing_bound_untouched(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):  # noqa: ARG001
    import ct_bounds
    entry = _Entry(800, options={ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS: 180})
    store = FakeStore([entry])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    devices.add(
        DimmerDevice(dev_id=800, name="Test Lamp", onState=False, brightness=0,
                     whiteLevel=60, whiteTemperature=_mireds_to_kelvin(300)),
        # Both echoes match exactly what was asked — no shortfall either side.
        echoes_kelvin=[_mireds_to_kelvin(153), _mireds_to_kelvin(500)])
    engine = _engine(calibration_mod, store, devices, learner, logger)

    assert engine.start(device_ids=[800], skip_lit=True) is True

    assert store.upserts == []  # nothing new persisted
    assert store.get(800).options[ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS] == 180  # untouched
    republish.assert_not_called()
    messages = [c.args[0] % c.args[1:] for c in logger.info.call_args_list]
    device_line = next(m for m in messages if "device 800 calibrated" in m)
    assert "no clamp observed" in device_line
    assert "echo matched the ask" in device_line


# ---------------------------------------------------------------------------
# 3. skipLit
# ---------------------------------------------------------------------------
def test_a_lit_lamp_is_skipped_when_skip_lit_is_true(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):
    store = FakeStore([_Entry(800)])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    devices.add(DimmerDevice(dev_id=800, name="Test Lamp", onState=True, brightness=50,
                             whiteLevel=60, whiteTemperature=_mireds_to_kelvin(300)))
    engine = _engine(calibration_mod, store, devices, learner, logger)

    assert engine.start(device_ids=[800], skip_lit=True) is True

    assert devices.calls_for(800) == 1  # only the initial fetch — never dispatched
    assert not mock_indigo_base.dimmer.setColorLevels.called
    assert store.upserts == []
    logged = str(logger.info.call_args_list)
    assert "currently on" in logged


def test_a_lit_lamp_is_calibrated_when_skip_lit_is_false(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):  # noqa: ARG001
    store = FakeStore([_Entry(800)])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    devices.add(
        DimmerDevice(dev_id=800, name="Test Lamp", onState=True, brightness=50,
                     whiteLevel=60, whiteTemperature=_mireds_to_kelvin(300)),
        echoes_kelvin=[_mireds_to_kelvin(200), _mireds_to_kelvin(400)])
    engine = _engine(calibration_mod, store, devices, learner, logger)

    assert engine.start(device_ids=[800], skip_lit=False) is True

    assert mock_indigo_base.dimmer.setColorLevels.called
    assert store.upserts  # at least one bound adopted


# ---------------------------------------------------------------------------
# 4. No white channel -> skipped with the reason, no writes
# ---------------------------------------------------------------------------
def test_no_white_channel_is_skipped_with_the_reason_and_writes_nothing(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):
    store = FakeStore([_Entry(800)])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    devices.add(DimmerDevice(dev_id=800, name="Test Lamp", onState=False, brightness=0,
                             whiteLevel=None, whiteTemperature=None))
    engine = _engine(calibration_mod, store, devices, learner, logger)

    assert engine.start(device_ids=[800], skip_lit=True) is True

    assert not mock_indigo_base.dimmer.setColorLevels.called
    assert store.upserts == []
    republish.assert_not_called()
    logged = str(logger.info.call_args_list)
    assert "no white channel" in logged


# ---------------------------------------------------------------------------
# 5. Exception mid-sweep -> restored, other devices still processed
# ---------------------------------------------------------------------------
def test_an_exception_mid_sweep_still_restores_and_other_devices_still_run(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):
    original_a = 300
    store = FakeStore([_Entry(800), _Entry(801)])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    dev_a = devices.add(
        DimmerDevice(dev_id=800, name="Wedged Lamp", onState=False, brightness=0,
                     whiteLevel=60, whiteTemperature=_mireds_to_kelvin(original_a)),
        echoes_kelvin=[_mireds_to_kelvin(153)])  # min side: no evidence, then max raises
    devices.add(
        DimmerDevice(dev_id=801, name="Fine Lamp", onState=False, brightness=0,
                     whiteLevel=60, whiteTemperature=_mireds_to_kelvin(300)),
        echoes_kelvin=[_mireds_to_kelvin(153), _mireds_to_kelvin(500)])

    calls_per_device: dict[int, int] = {}

    def flaky_set_color_levels(dev, **_levels):
        calls_per_device[dev.id] = calls_per_device.get(dev.id, 0) + 1
        if dev is dev_a and calls_per_device[dev.id] == 2:
            raise RuntimeError("driver wedged")
        return None

    mock_indigo_base.dimmer.setColorLevels.side_effect = flaky_set_color_levels
    engine = _engine(calibration_mod, store, devices, learner, logger)

    assert engine.start(device_ids=None, skip_lit=True) is True  # scope: all

    assert logger.error.called  # the mid-sweep failure was reported
    # dev_a's LAST setColorLevels call (the restore, in `finally`) put it
    # back at its original colour temperature despite the exception.
    a_calls = [c for c in mock_indigo_base.dimmer.setColorLevels.call_args_list
               if c.args[0] is dev_a]
    assert a_calls[-1].kwargs["whiteTemperature"] == _mireds_to_kelvin(original_a)
    # dev_b was still processed — the sweep summary counts it as calibrated,
    # and its own upsert (both echoes match the ask, so no bound is adopted
    # here — the point is only that it RAN, not that it produced evidence).
    summary = logger.info.call_args_list[-1]
    message = summary.args[0] % summary.args[1:]
    assert "1 calibrated, 1 skipped" in message


# ---------------------------------------------------------------------------
# 6. Concurrency guard
# ---------------------------------------------------------------------------
def test_a_second_sweep_is_refused_while_one_is_in_flight(
        calibration_mod, ct_learner_mod, mock_indigo_base, logger, republish):  # noqa: ARG001
    store = FakeStore([_Entry(800)])
    learner = ct_learner_mod.CTBoundsLearner(store, logger, republish)
    devices = ScriptedDevices()
    engine = _engine(calibration_mod, store, devices, learner, logger)
    # Poke the guard flag directly rather than racing a real thread — the
    # guard is a plain checked-and-set flag, and this pins its refusal
    # behaviour deterministically (see CTCalibrationEngine.running's own
    # docstring on this being the sanctioned shortcut).
    engine._running = True  # pylint: disable=protected-access

    assert engine.start(device_ids=[800], skip_lit=True) is False

    logger.warning.assert_called_once()
    logged = str(logger.warning.call_args)
    assert "already running" in logged
    assert not mock_indigo_base.dimmer.setColorLevels.called


# ---------------------------------------------------------------------------
# adopt_measured collapse refusal — see tests/test_ct_learner.py's own
# test_adopt_measured_collapse_refusal_reuses_adopt_s_own_guard for the
# learner-level pin; the engine never bypasses that guard, it only calls in.
# ---------------------------------------------------------------------------
