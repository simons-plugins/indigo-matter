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

import inspect
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

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


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def node_id_to_str(node_id: Any) -> str:
    """Represent a Matter node id as the hex string the API uses."""
    if isinstance(node_id, str):
        return node_id
    return f"0x{int(node_id):X}"


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
        except CommissionError as exc:
            await self._fail(job, exc.code, exc.message, node_id, exc.matter_error_code)
        except ProtocolError as exc:
            # matter-server rejected the commission (bad setup code, window closed,
            # PASE/CASE failure, …) — a device/commissioning failure, not a plugin
            # bug, so report it as such rather than internal_error.
            await self._fail(job, "commissioning_failed", str(exc), node_id,
                             _opt_int(getattr(exc, "code", None)))
        except Exception as exc:  # noqa: BLE001 - last-resort, mapped to internal_error
            self.logger.exception(exc)
            await self._fail(job, "internal_error", str(exc), node_id)

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
