"""Commissioning job state machine (API.md §3.2/§3.3).

``POST …/commission`` returns 202 + jobId immediately; the actual work runs as an
async task on the plugin's asyncio loop, advancing a job through
pending → commissioning → reading_descriptors → creating_devices → success/failed.
``GET …/commission/{jobId}`` polls it.

Jobs are held in memory keyed by jobId, with a secondary index by setupCode for
dedup, guarded by a lock (the create path may run on an Indigo thread; the async
worker runs on the loop). Terminal jobs are retained 15 minutes then reaped.

Device creation is an injected async callback so this module is testable without
the device-sync / cluster-handler layers (M4).
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from matter_client import COMMISSION_TIMEOUT
from matter_model import node_id_to_str  # noqa: F401 - canonical home; re-exported for callers
from protocol import ProtocolError


class JobStatus(str, Enum):
    """Commissioning job status (API.md §3.3).

    A ``str`` subclass so it serialises to its wire value via ``json.dumps`` and
    compares equal to the raw string, while making the legal set explicit and
    typos a lint/IDE error.
    """
    PENDING = "pending"
    COMMISSIONING = "commissioning"
    READING_DESCRIPTORS = "reading_descriptors"
    CREATING_DEVICES = "creating_devices"
    SUCCESS = "success"
    FAILED = "failed"


# Module-level aliases keep call sites terse and unchanged.
PENDING = JobStatus.PENDING
COMMISSIONING = JobStatus.COMMISSIONING
READING_DESCRIPTORS = JobStatus.READING_DESCRIPTORS
CREATING_DEVICES = JobStatus.CREATING_DEVICES
SUCCESS = JobStatus.SUCCESS
FAILED = JobStatus.FAILED
TERMINAL = frozenset({JobStatus.SUCCESS, JobStatus.FAILED})

RETENTION = timedelta(minutes=15)

# How long after a commissioning_timeout failure a node_added may still claim
# the job (issue #16): matter-server keeps commissioning in the background after
# our RPC gives up (the node was observed joining ~64s after the job died), so a
# recently-timed-out job is flipped back to success when its node arrives.
# Bounded so a node added much later (dashboard, manual) can't resurrect stale
# jobs. Must be < RETENTION or the job may be reaped before it can reconcile.
RECONCILE_WINDOW = timedelta(minutes=5)

TIMEOUT_MESSAGE = (
    f"matter-server did not finish commissioning within {COMMISSION_TIMEOUT:.0f}s; "
    "the device may still join — check Indigo before retrying"
)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _exc_message(exc: BaseException) -> str:
    """``str(exc)`` is empty for bare exceptions (e.g. ``TimeoutError()``) — fall
    back to the type name so error payloads are never blank (#17)."""
    text = str(exc).strip()
    return text or type(exc).__name__


def is_valid_setup_code(code: str) -> bool:
    if not code:
        return False
    if code.startswith("MT:"):
        return len(code) > 3
    return code.isdigit() and len(code) in (11, 21)


class CommissionError(Exception):
    """Carries a structured API error code (API.md §3.3 table)."""

    def __init__(self, code: str, message: str, matter_error_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.matter_error_code = matter_error_code


@dataclass
class Job:
    job_id: str
    setup_code: str
    suggested_name: str
    suggested_room: Optional[str]
    discriminator: Optional[int] = None
    domio_node_id: Optional[str] = None
    status: JobStatus = PENDING
    progress: float = 0.0
    message: str = ""
    started_at: datetime = field(default_factory=_now_utc)
    terminal_at: Optional[datetime] = None
    result: Optional[dict] = None
    error: Optional[dict] = None

    def serialize(self) -> dict:
        out: dict[str, Any] = {
            "jobId": self.job_id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "startedAt": self.started_at.isoformat(),
        }
        if self.message:
            out["message"] = self.message
        if self.status not in TERMINAL:
            elapsed = (_now_utc() - self.started_at).total_seconds()
            out["elapsedSeconds"] = int(elapsed)
        if self.result is not None:
            out["result"] = self.result
        if self.error is not None:
            out["error"] = self.error
        return out


class CommissionJobs:
    """Owns the job table and the async commissioning worker."""

    def __init__(
        self,
        matter: Any,
        create_devices: Callable[[dict, str, Optional[str]], Awaitable[dict]],
        logger: Any,
        *,
        schedule: Callable[[Awaitable], Any],
        clock: Callable[[], datetime] = _now_utc,
        uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.matter = matter
        self._create_devices = create_devices
        self.logger = logger
        self._schedule = schedule
        self._clock = clock
        self._uuid = uuid_factory
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._by_code: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API (called from the IWS handler thread)
    # ------------------------------------------------------------------
    def create_job(self, params: dict) -> tuple[int, dict]:
        """Validate + create (or dedup) a job. Returns (http_status, body)."""
        setup_code = (params.get("setupCode") or "").strip()
        if not is_valid_setup_code(setup_code):
            return 400, {
                "error": "invalid_setup_code",
                "message": "Setup code must be an MT:... QR payload or an 11- or 21-digit numeric pairing code",
            }
        name = (params.get("suggestedName") or "").strip()
        if not name:
            return 400, {"error": "invalid_request", "message": "suggestedName is required"}

        if not getattr(self.matter, "connected", False):
            # Fail fast per API.md: accepting the job now (202) would only have the
            # worker fail with a generic internal_error at its first WebSocket call.
            return 503, {
                "error": "matter_server_unreachable",
                "message": "Not connected to matter-server; check it is running and reachable, then retry",
            }

        with self._lock:
            self._reap_locked()
            existing_id = self._by_code.get(setup_code)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and existing.status not in TERMINAL:
                    return 409, {
                        "error": "duplicate",
                        "existingJobId": existing_id,
                        "message": "Commissioning for this setup code is already in progress",
                    }
            job = Job(
                job_id=self._uuid(),
                setup_code=setup_code,
                suggested_name=name,
                suggested_room=(params.get("suggestedRoom") or None),
                discriminator=_opt_int(params.get("discriminator")),
                domio_node_id=(params.get("domioNodeId") or None),
                started_at=self._clock(),
            )
            self._jobs[job.job_id] = job
            self._by_code[setup_code] = job.job_id

        coro = self._run_job(job)
        try:
            self._schedule(coro)
        except RuntimeError as exc:
            # The loop isn't running. Don't leave a stranded PENDING job: it would
            # never reach a terminal state, never be reaped, and so lock this setup
            # code out of every future commission (409) until a plugin restart.
            coro.close()
            with self._lock:
                self._jobs.pop(job.job_id, None)
                if self._by_code.get(setup_code) == job.job_id:
                    self._by_code.pop(setup_code, None)
            return 503, {"error": "matter_server_unreachable", "message": str(exc)}
        return 202, {"jobId": job.job_id, "estimatedDurationSeconds": 30}

    def get_job(self, job_id: str) -> tuple[int, dict]:
        with self._lock:
            self._reap_locked()
            job = self._jobs.get(job_id)
            if job is None:
                return 404, {"error": "job_not_found"}
            return 200, job.serialize()

    # ------------------------------------------------------------------
    # Late-join reconcile (issue #16; called from the WS event path)
    # ------------------------------------------------------------------
    def reconcile_node_added(self, raw_node: dict) -> Optional[str]:
        """A node joined out-of-band — if a commission job recently failed by
        timeout, the join is (almost certainly) that job completing in the
        background: re-open it, apply the user's suggestedName/suggestedRoom,
        and flip it to success so a still-polling client gets the real outcome.

        Candidates are matched by recency: only jobs that went terminal with
        ``commissioning_timeout`` within RECONCILE_WINDOW qualify, and the most
        recent one wins (a single rehearsal/retry flow never has two). Returns
        the claimed jobId, or None if no job qualified.
        """
        if not isinstance(raw_node, dict) or raw_node.get("node_id") is None:
            with self._lock:
                waiting = self._timeout_candidates_locked(self._clock() - RECONCILE_WINDOW)
            if waiting:
                # A claimable job is waiting and this event was its one shot —
                # leave evidence. (No candidates = routine restart sync; stay quiet.)
                self.logger.warning(
                    "node_added event without a usable node_id ignored while "
                    "commission job %s awaits reconcile", waiting[-1].job_id)
            return None
        node_id = raw_node.get("node_id")
        stale: Optional[Job] = None
        with self._lock:
            cutoff = self._clock() - RECONCILE_WINDOW
            candidates = self._timeout_candidates_locked(cutoff)
            if not candidates:
                expired = self._timeout_candidates_locked(None)
                stale = expired[-1] if expired else None
            else:
                job = candidates[-1]
                # Claim it inside the lock (a second node_added must not double-claim):
                # re-opening to CREATING_DEVICES is honest — that's the work left.
                # Keep the priors so a failed schedule can restore the terminal
                # timeout state exactly (including the original terminal_at).
                prior_error, job.error = job.error, None
                prior_terminal_at, job.terminal_at = job.terminal_at, None
                prior_progress, job.progress = job.progress, 0.85
                prior_message, job.message = job.message, "Device joined after timeout; creating Indigo devices…"
                job.status = CREATING_DEVICES
        if not candidates:
            if stale is not None:
                self.logger.warning(
                    "node %s joined after commission job %s timed out, but outside "
                    "the %.0f-minute reconcile window — suggestedName/suggestedRoom "
                    "were not applied", node_id_to_str(node_id), stale.job_id,
                    RECONCILE_WINDOW.total_seconds() / 60,
                )
            return None
        coro = self._reconcile_job(job, raw_node)
        try:
            self._schedule(coro)
        except RuntimeError as exc:  # loop down — restore the terminal timeout state
            coro.close()
            with self._lock:
                # One critical section: a poller must never observe a half-restored
                # job. Restore the ORIGINAL terminal_at (not a fresh stamp) so the
                # documented reconcile-window bound is preserved.
                job.status = FAILED
                job.error = prior_error
                job.progress = prior_progress
                job.message = prior_message
                job.terminal_at = prior_terminal_at
            self.logger.error(
                "commission job %s: node %s joined but the reconcile could not be "
                "scheduled (%s); job restored to its timed-out state",
                job.job_id, node_id_to_str(node_id), exc,
            )
            return None
        return job.job_id

    def _timeout_candidates_locked(self, cutoff: Optional[datetime]) -> list[Job]:
        """Jobs that failed with commissioning_timeout, oldest→newest.

        ``cutoff=None`` means any age (used to detect a stale, outside-window
        candidate worth a log line); otherwise only jobs whose terminal_at is
        within the reconcile window qualify. Caller must hold ``self._lock``.
        """
        return sorted(
            (
                job for job in self._jobs.values()
                if job.status is FAILED
                and (job.error or {}).get("code") == "commissioning_timeout"
                and job.terminal_at is not None
                and (cutoff is None or job.terminal_at >= cutoff)
            ),
            key=lambda j: j.terminal_at,
        )

    async def _reconcile_job(self, job: Job, raw_node: dict) -> None:
        node_id = raw_node.get("node_id")
        try:
            created = self._create_devices(raw_node, job.suggested_name, job.suggested_room)
            if inspect.isawaitable(created):
                created = await created
            job.result = {"nodeId": node_id_to_str(node_id), **created}
            self._advance(job, SUCCESS, 1.0, "Done (completed after timeout)")
            self.logger.info(
                "commission job %s reconciled: node %s joined after the commission "
                "request timed out", job.job_id, node_id_to_str(node_id),
            )
        except Exception as exc:  # noqa: BLE001 - the job must reach a terminal state
            self.logger.exception(exc)
            job.error = {"code": "internal_error", "message": _exc_message(exc)}
            self._advance(job, FAILED, job.progress, "")

    # ------------------------------------------------------------------
    # Async worker (runs on the loop)
    # ------------------------------------------------------------------
    async def _run_job(self, job: Job) -> None:
        node_id: Any = None
        try:
            self._advance(job, COMMISSIONING, 0.2, "Commissioning…")
            result = await self.matter.commission_with_code(job.setup_code)
            node_id = result.get("node_id") if isinstance(result, dict) else result
            if node_id is None:
                raise CommissionError("commissioning_failed", "matter-server returned no node_id")

            self._advance(job, READING_DESCRIPTORS, 0.6, "Discovering device capabilities…")
            node = await self.matter.get_node(node_id)

            self._advance(job, CREATING_DEVICES, 0.85, "Creating Indigo devices…")
            created = self._create_devices(node, job.suggested_name, job.suggested_room)
            if inspect.isawaitable(created):
                created = await created

            job.result = {"nodeId": node_id_to_str(node_id), **created}
            self._advance(job, SUCCESS, 1.0, "Done")
        except asyncio.CancelledError:
            # Plugin shutdown cancelled the worker task. Land the terminal state
            # synchronously — _fail awaits remove_node, and any await inside a
            # cancelled task can re-raise CancelledError before the state is set —
            # then re-raise so the task actually cancels. Without this the job
            # strands in COMMISSIONING forever (never reaped) and its setup code
            # 409s every retry for the life of the process.
            job.error = {"code": "internal_error", "message": "commissioning cancelled by plugin shutdown"}
            self._advance(job, FAILED, job.progress, "")
            raise
        except CommissionError as exc:
            await self._fail(job, exc.code, exc.message, node_id, exc.matter_error_code)
        except (asyncio.TimeoutError, TimeoutError):
            if node_id is not None:
                # Commissioning itself SUCCEEDED — this timeout came from a later
                # step (get_node's short descriptor read). Calling it a
                # commissioning_timeout would be wrong on every clause ("did not
                # finish commissioning" / "may still join" about a node that
                # already joined), and reconcile couldn't repair it: the job was
                # not yet terminal when node_added fired. Do NOT remove_node —
                # the node is on the fabric and node_added may already have
                # created its Indigo devices.
                message = (
                    f"timed out reading node descriptors for "
                    f"{node_id_to_str(node_id)} after commissioning succeeded"
                )
                self.logger.error("commission job %s: %s", job.job_id, message)
                job.error = {"code": "internal_error", "message": message}
                self._advance(job, FAILED, job.progress, "")
            else:
                # The commission RPC gave up, but matter-server keeps commissioning
                # in the background (issue #16) — the node may still join. Do NOT
                # remove_node (it could tear down an about-to-succeed join), and use
                # a specific code/message: str(TimeoutError()) is "" so the generic
                # branch would report internal_error with an empty message (#17).
                # If the node arrives within RECONCILE_WINDOW, reconcile_node_added
                # flips this job back to success. Logged so the event log holds
                # evidence even after Domio has stopped polling the job.
                self.logger.warning("commission job %s: %s", job.job_id, TIMEOUT_MESSAGE)
                job.error = {"code": "commissioning_timeout", "message": TIMEOUT_MESSAGE}
                self._advance(job, FAILED, job.progress, "")
        except ProtocolError as exc:
            # matter-server rejected the commission (bad setup code, window closed,
            # PASE/CASE failure, …) — a device/commissioning failure, not a plugin
            # bug, so report it as such rather than internal_error.
            await self._fail(job, "commissioning_failed", str(exc), node_id,
                             _opt_int(getattr(exc, "code", None)))
        except Exception as exc:  # noqa: BLE001 - last-resort, mapped to internal_error
            self.logger.exception(exc)
            await self._fail(job, "internal_error", _exc_message(exc), node_id)

    async def _fail(self, job: Job, code: str, message: str,
                    node_id: Any, matter_error_code: Optional[int] = None) -> None:
        if node_id is not None:
            try:
                await self.matter.remove_node(node_id)  # best-effort cleanup
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("best-effort remove_node failed: %s", exc)
        error = {"code": code, "message": message}
        if matter_error_code is not None:
            error["matterErrorCode"] = matter_error_code
        job.error = error
        self._advance(job, FAILED, job.progress, "")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _advance(self, job: Job, status: JobStatus, progress: float, message: str) -> None:
        with self._lock:
            job.status = status
            job.progress = progress
            job.message = message
            if status in TERMINAL:
                job.terminal_at = self._clock()

    def _reap_locked(self) -> None:
        cutoff = self._clock() - RETENTION
        expired = [
            jid for jid, job in self._jobs.items()
            if job.terminal_at is not None and job.terminal_at < cutoff
        ]
        for jid in expired:
            job = self._jobs.pop(jid)
            if self._by_code.get(job.setup_code) == jid:
                self._by_code.pop(job.setup_code, None)


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
