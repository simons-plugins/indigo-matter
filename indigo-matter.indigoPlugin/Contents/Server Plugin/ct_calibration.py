"""The explicit "Calibrate Colour-Temperature Bounds" action (issue #293's
calibration extension; ADR-0014 Option C — rejected as an AUTOMATIC
mechanism, now shipped as an operator-invoked one).

**Why this action exists.** ``ct_learner.py``'s passive evidence has a real
gap: :meth:`ct_learner.CTBoundsLearner.record_commanded` only ever fires from
``ExportBridge._push_commanded``, which only runs after a §5 command arrives
FROM the fabric. Every Indigo-side colour-temperature change — a scene, a
schedule, a person turning a dial — feeds :meth:`ct_learner.CTBoundsLearner.
observe` too, but ``observe`` only ever ADOPTS on the re-widen path (a
reading outside the current bounds proves reach); it can never adopt a
SHORTFALL from an Indigo-side change, because shortfall adoption requires a
commanded reference to compare the echo against, and Indigo-side changes
never record one. So a lamp that is never driven by a paired ecosystem —
freshly exported, or one nobody's Home app has adaptively re-targeted yet —
can sit forever with no commanded reference at all, and NEVER learn its true
limits from Indigo traffic alone, however many times its colour temperature
changes.

A calibration sweep closes that gap because it is the one caller that can
supply what the shortfall path is missing: it knows exactly what it asked
for, because it asked. Command one extreme, wait, read the echo — an echo
that differs from the ask by :data:`ct_learner.SHORTFALL_THRESHOLD_MIREDS` or
more IS the hardware limit, with no separate "commanded reference" bookkeeping
needed at all, because the ask and the reference are the same call.

**The dispatch is the SAME code path a fabric command takes** —
``handler.dispatch(COMMAND_SET_COLOR_TEMP, ...)``, exactly what
``ExportBridge._apply_command`` calls for a real §5 command. That is
deliberate, not incidental: it means issue #281's off-lamp safety (a CT
write on an OFF lamp preserves the stored ``whiteLevel`` rather than
switching the lamp on) and the no-white-channel refusal (a reason string,
not a raised error) apply to a calibration sweep identically to a real
ecosystem command, with no second implementation to keep in sync.

**Side effect, and why it is harmless.** Every dispatch here is a real
Indigo write, so it lands on ``deviceUpdated`` like any other change: the
fabric sees the colour-temperature wiggle (the lamp is off throughout, by
construction — see :func:`_is_lit` — so nothing visibly happens), and
``ExportBridge._feed_ct_learner`` may independently adopt the very same
values through the RE-WIDEN path before this module's own
:meth:`ct_learner.CTBoundsLearner.adopt_measured` call gets there. That race
is benign: :meth:`ct_learner.CTBoundsLearner._adopt` is idempotent — adopting
a candidate equal to the already-effective value is a deliberate no-op (see
its own "already the effective value — nothing to say twice" guard) — so the
two paths cannot fight over the same measurement, only agree on it.

**Threading.** :meth:`CTCalibrationEngine.start` is called from the plugin's
Actions.xml callback (``plugin.Plugin.actionCalibrateCtBounds``) and returns
immediately, having handed the actual sweep to a DEDICATED background thread
— never Indigo's action-callback thread (a sweep can take tens of seconds
across several devices, and that thread is shared with every other queued
action/trigger), and never ``ExportBridge``'s own single command worker (a
sweep competing with real ecosystem commands for that one FIFO thread would
stall someone pressing a switch in the Home app behind a colour sweep nobody
but the sweep itself is waiting on). The default thread is a plain daemon
``threading.Thread``, matching ``plugin.Plugin.actionShareMatterNode``'s own
precedent for a slow, fire-and-forget action — ``daemon=True`` is what keeps
a plugin shutdown from hanging on a sweep still in flight, at the cost of
that sweep simply stopping mid-device if the host process exits; there is no
recovery to interrupt because a dispatch mid-flight cannot be cancelled
either.

No Indigo import: like ``ct_learner``/``export_bridge``, this module takes
injected ``store``/``device_getter``/``learner``/``logger``/``sleep`` and
unit-tests against fakes — the ``export_handlers`` import below pulls in
``indigo`` transitively (``handler.dispatch`` has to reach real Indigo calls
to be the same code path a fabric command takes), exactly as
``export_bridge.py`` itself does.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable, Optional, Union

import ct_bounds
import ct_learner
import export_handlers
from matter_handlers.color_control import mireds_to_kelvin

#: How long to wait after asking for an extreme before re-reading the echo.
#: Not a network round trip like ``ct_learner.FRESHNESS_WINDOW_SECONDS`` —
#: this is a local Indigo dispatch, so the wait only has to cover the
#: device's OWN driver settling (z2m publishing its confirmed state back
#: through Indigo). 2.5s is comfortably past every settle time seen in the
#: issue #293 measurement pass without turning a multi-device sweep into a
#: minutes-long one.
SETTLE_SECONDS = 2.5

#: The two extremes a sweep asks for, in the order asked. Labelled ``"min"``/
#: ``"max"`` to match ``ct_bounds.OPTION_CT_LEARNED_MIN_MIREDS``/``_MAX_``
#: and ``ct_learner``'s own side vocabulary — asking ``MIREDS_MIN`` and
#: getting a differing echo IS cool-floor evidence, asking ``MIREDS_MAX`` and
#: getting a differing echo IS warm-ceiling evidence, by construction: which
#: side the evidence is for is simply which extreme was asked, with no
#: sign-of-delta reasoning needed the way ``ct_learner._observe_locked``
#: needs one (it does not know in advance which extreme, if either, an
#: unprompted echo is answering).
_EXTREMES: tuple[tuple[str, int], ...] = (
    ("min", export_handlers.MIREDS_MIN),
    ("max", export_handlers.MIREDS_MAX),
)

#: One skipped/measured outcome per side, keyed the same way as ``_EXTREMES``.
_SIDE_LABELS = {"min": "cool", "max": "warm"}


def _is_lit(dev: Any) -> bool:
    """True if ``dev`` currently reports a positive brightness.

    Mirrors ``export_handlers.DimmableLightExport.states_for``'s own reading
    of ``brightness`` (not re-imported: that helper is module-private, and a
    two-line local reimplementation is simpler than acquiring a dependency on
    another module's underscore-prefixed name for it).
    """
    value = getattr(dev, "brightness", None)
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _default_thread_starter(run: Callable[[], None]) -> None:
    """Spawn ``run`` on a plain daemon thread. See the module docstring's
    Threading section for why daemon and why not the bridge's command
    worker."""
    threading.Thread(target=run, name="matter-ct-calibrate", daemon=True).start()


class CTCalibrationEngine:
    """Runs a colour-temperature calibration sweep and persists what it learns.

    :param store: the export allow-list (``export_store.ExportStore`` shape:
        ``.get(device_id)`` / ``.all()``).
    :param device_getter: ``id -> indigo device or None``. Injected so this
        module unit-tests without the Indigo runtime, the same reason
        ``export_bridge.ExportBridge`` takes one.
    :param learner: the bridge's ``ct_learner.CTBoundsLearner`` — measurements
        are persisted through :meth:`ct_learner.CTBoundsLearner.adopt_measured`,
        never through a second store-writing path (see the module docstring).
    :param logger: the plugin logger.
    :param sleep: settle-wait callable, injected so tests run the whole sweep
        with no real waiting — same seam as ``ct_learner``'s injected ``now``.
    :param thread_starter: ``run -> None``, injected so a test can execute a
        sweep INLINE (synchronously, on the calling thread) instead of racing
        a real background one — the same discipline
        ``tests/fakes.InlineExecutor`` gives ``ExportBridge``'s command
        worker.
    """

    def __init__(self, store, device_getter: Callable[[int], Any],
                 learner: "ct_learner.CTBoundsLearner", logger,
                 *, sleep: Callable[[float], None] = time.sleep,
                 thread_starter: Callable[[Callable[[], None]], None] = _default_thread_starter) -> None:
        self._store = store
        self._device_getter = device_getter
        self._learner = learner
        self._logger = logger
        self._sleep = sleep
        self._thread_starter = thread_starter
        #: Guards `_running` AND is the concurrency gate itself — one sweep
        #: at a time (issue #293's own requirement): a second sweep sharing
        #: devices with the first would race the same lamp's dispatch/restore
        #: sequence against itself, and there is no scenario where running
        #: two sweeps concurrently is faster in any way a user would notice.
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        """True while a sweep is in flight. Read-only; tests may still poke
        the underlying flag directly to exercise the refusal path without
        spinning up a real thread — see ``tests/test_ct_calibration.py``."""
        return self._running

    def start(self, device_ids: Optional[Iterable[Union[int, str]]] = None,
              skip_lit: bool = True) -> bool:
        """Begin a sweep. ``device_ids=None`` means every exported CT-role
        device; a non-empty iterable scopes the sweep to those ids (a stray
        non-int entry is silently dropped — the caller's job is to have
        already validated its scope selection, and this is the last, cheap
        line of defence rather than a second place to report the same bad
        input twice).

        Returns whether the sweep actually started. ``False`` means a sweep
        was already running and this request was refused — logged here, not
        left to the caller, because the caller (the Actions.xml callback)
        has already returned by the time this would otherwise be known.
        """
        with self._lock:
            if self._running:
                self._logger.warning(
                    "Matter export: a colour-temperature calibration sweep is already running — "
                    "ignoring this request until it finishes.")
                return False
            self._running = True
        resolved_ids: Optional[list[int]] = None
        if device_ids is not None:
            resolved_ids = []
            for raw in device_ids:
                try:
                    resolved_ids.append(int(raw))
                except (TypeError, ValueError):
                    continue
        self._thread_starter(lambda: self._run(resolved_ids, skip_lit))
        return True

    # ------------------------------------------------------------------
    def _run(self, device_ids: Optional[list[int]], skip_lit: bool) -> None:
        try:
            calibrated = 0
            skipped = 0
            for target in self._resolve_targets(device_ids):
                if self._process_target(target, skip_lit):
                    calibrated += 1
                else:
                    skipped += 1
            self._logger.info(
                "Matter export: colour-temperature calibration sweep finished — %d calibrated, "
                "%d skipped.", calibrated, skipped)
        finally:
            with self._lock:
                self._running = False

    def _resolve_targets(self, device_ids: Optional[list[int]]) -> list:
        """Every CT-role export entry, or one per requested id.

        A requested id absent from the store is kept as the bare int rather
        than dropped: :meth:`_process_target` still owes the user a line
        saying it was skipped and why (a stale picker selection — the device
        was un-exported since the dialog was opened — must not vanish
        without a trace).
        """
        if device_ids is None:
            return [entry for entry in self._store.all() if entry.role in ct_bounds.CT_ROLES]
        targets: list = []
        for device_id in device_ids:
            entry = self._store.get(device_id)
            targets.append(entry if entry is not None else device_id)
        return targets

    def _process_target(self, target, skip_lit: bool) -> bool:
        if isinstance(target, int):
            self._logger.info(
                "Matter export: device %s skipped calibration — it is not currently exported.",
                target)
            return False
        try:
            return self._calibrate_one(target, skip_lit)
        except Exception as exc:  # pylint: disable=broad-except
            # Defense in depth only: `_calibrate_one`'s own try/finally
            # already restores the device and reports a mid-sweep failure
            # for everything between the first dispatch and the last read.
            # This catches a failure OUTSIDE that window (role/device/handler
            # resolution) so one bad export can never take the whole sweep
            # down with it — the same promise `ExportBridge.device_updated`
            # makes per device.
            self._logger.error(
                "Matter export: could not calibrate device %s — %s.",
                getattr(target, "indigo_device_id", "?"), exc)
            self._logger.exception(exc)
            return False

    def _calibrate_one(self, entry, skip_lit: bool) -> bool:
        device_id = entry.indigo_device_id
        if entry.role not in ct_bounds.CT_ROLES:
            self._logger.info(
                "Matter export: device %s skipped calibration — not a colour-temperature-"
                "capable export.", device_id)
            return False
        dev = self._device_getter(device_id)
        if dev is None:
            self._logger.info(
                "Matter export: device %s skipped calibration — the Indigo device no longer "
                "exists.", device_id)
            return False
        handler = export_handlers.handler_for(entry.role)
        if handler is None:
            # Unreachable for a CT role in a released build (E4 completed the
            # handler table over the whole role enum) — kept as a guard, not
            # a documented outcome, the same way `ExportHandler.dispatch`
            # keeps its own `False` branch for a role an older/newer version
            # disagrees about.
            self._logger.info(
                "Matter export: device %s skipped calibration — its role has no export handler.",
                device_id)
            return False
        if skip_lit and _is_lit(dev):
            self._logger.info(
                'Matter export: device %s skipped calibration — the lamp is currently on and '
                '"Skip lamps that are currently on" is enabled.', device_id)
            return False

        original = self._read_mireds(handler, dev, entry.options)
        side_results: dict[str, Union[int, str]] = {}
        skip_reason: Optional[str] = None
        try:
            for side, extreme in _EXTREMES:
                outcome = handler.dispatch(
                    export_handlers.COMMAND_SET_COLOR_TEMP,
                    {export_handlers.STATE_COLOR_TEMP_MIREDS: extreme}, dev, entry.options)
                if isinstance(outcome, str):
                    # The same no-op reason a real ecosystem command would
                    # get (issue #281's no-white-channel refusal, today the
                    # only one `_set_color_temp` returns) — static per
                    # device, so the second extreme would only repeat it.
                    skip_reason = outcome
                    break
                self._sleep(SETTLE_SECONDS)
                fresh = self._device_getter(device_id)
                if fresh is None:
                    skip_reason = "the device stopped existing partway through the sweep"
                    break
                echo = self._read_mireds(handler, fresh, entry.options)
                if echo is None:
                    side_results[side] = ("no clamp observed — the device reported no readable "
                                          "colour temperature after the write")
                    continue
                if abs(echo - extreme) >= ct_learner.SHORTFALL_THRESHOLD_MIREDS:
                    side_results[side] = echo
                    self._learner.adopt_measured(
                        entry, side, echo,
                        reason=(f"a calibration sweep asked {extreme} mireds and the device "
                                f"echoed back {echo}"))
                else:
                    side_results[side] = ("no clamp observed — echo matched the ask; the driver "
                                          "may confirm optimistically")
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter export: the calibration sweep for device %s failed partway through — %s. "
                "Its original colour temperature is being restored.", device_id, exc)
            self._logger.exception(exc)
            skip_reason = f"an error interrupted the sweep ({exc})"
        finally:
            # Runs whether the sweep finished, broke out on a no-op reason,
            # or raised — a partially-swept lamp must never be left holding
            # a calibration extreme as its real colour temperature. Skipped
            # only when there was never a reading to restore (issue #281: a
            # device with no white channel typically reports no
            # `whiteTemperature` at all, so `original` is `None` and there
            # is nothing to put back).
            if original is not None:
                self._restore(handler, dev, device_id, entry.options, original)

        if skip_reason is not None:
            self._logger.info("Matter export: device %s skipped calibration — %s.",
                              device_id, skip_reason)
            return False
        self._log_measurement(device_id, side_results)
        return True

    @staticmethod
    def _read_mireds(handler, dev: Any, options: Optional[dict]) -> Optional[int]:
        value = handler.published_states(dev, options).get(export_handlers.STATE_COLOR_TEMP_MIREDS)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _restore(self, handler, dev: Any, device_id: int, options: Optional[dict],
                 original_mireds: int) -> None:
        """Put the device back where it was, via the same dispatch path.

        No re-read/verify afterwards: this is a best-effort courtesy restore
        of Indigo's own device, not a fabric-facing state the plugin has
        promised to keep converged — the ordinary `device_updated` push
        already reports whatever the device settles on next, the same as
        after any other Indigo-side colour change.
        """
        try:
            handler.dispatch(
                export_handlers.COMMAND_SET_COLOR_TEMP,
                {export_handlers.STATE_COLOR_TEMP_MIREDS: original_mireds}, dev, options)
        except Exception as exc:  # pylint: disable=broad-except
            self._logger.error(
                "Matter export: could not restore device %s's original colour temperature "
                "(%s mireds) after calibration — %s. It is left at whatever the last "
                "calibration write set.", device_id, original_mireds, exc)
            self._logger.exception(exc)

    def _log_measurement(self, device_id: int, side_results: dict[str, Union[int, str]]) -> None:
        parts = []
        for side, _extreme in _EXTREMES:
            result = side_results.get(side)
            if isinstance(result, int):
                parts.append(f"{_SIDE_LABELS[side]} {result} mireds ({mireds_to_kelvin(result)}K)")
            elif result is not None:
                parts.append(f"{_SIDE_LABELS[side]}: {result}")
            else:
                parts.append(f"{_SIDE_LABELS[side]}: no reading taken")
        self._logger.info("Matter export: device %s calibrated — %s.", device_id, "; ".join(parts))
