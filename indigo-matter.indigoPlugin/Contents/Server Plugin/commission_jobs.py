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
from protocol import Protocol, ProtocolError


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

# Operational Credentials cluster (0x003E), endpoint 0 — SupportedFabrics
# (0x0002) minus CommissionedFabrics (0x0003) is the remaining fabric capacity
# after this join, used for the API.md §3.2 `expectedFabricSlots` warning.
_OP_CREDS_CLUSTER = 0x003E
_ATTR_SUPPORTED_FABRICS = 0x0002
_ATTR_COMMISSIONED_FABRICS = 0x0003


def fabric_counts(node: dict) -> tuple[Optional[int], Optional[int]]:
    """Read (SupportedFabrics, CommissionedFabrics) from a node's interview
    payload — a named helper (issue #210) so a second caller (the "share with
    another ecosystem" menu/action) can read the same two numbers without
    reaching into commission_jobs' private fabric-slot arithmetic.

    Either or both come back None (unknown, never treated as zero) if the
    node's attribute snapshot doesn't include that Operational Credentials
    attribute — older firmware, or a partial read.
    """
    supported = commissioned = None
    for key, value in (node.get("attributes") or {}).items():
        try:
            endpoint, cluster, attribute = Protocol.parse_attr_key(str(key))
        except (ValueError, AttributeError):
            continue
        if endpoint != 0 or cluster != _OP_CREDS_CLUSTER:
            continue
        if attribute == _ATTR_SUPPORTED_FABRICS:
            supported = value
        elif attribute == _ATTR_COMMISSIONED_FABRICS:
            commissioned = value
    try:
        supported = int(supported) if supported is not None else None
        commissioned = int(commissioned) if commissioned is not None else None
    except (TypeError, ValueError):
        return None, None
    return supported, commissioned


def _fabric_slots_available(node: dict) -> Optional[int]:
    """Best-effort read of remaining fabric capacity from the interview payload.

    Returns None (unknown, never treated as zero) if the node's attribute
    snapshot doesn't include both Operational Credentials attributes.
    """
    supported, commissioned = fabric_counts(node)
    if supported is None or commissioned is None:
        return None
    return supported - commissioned


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _exc_message(exc: BaseException) -> str:
    """``str(exc)`` is empty for bare exceptions (e.g. ``TimeoutError()``) — fall
    back to the type name so error payloads are never blank (#17)."""
    text = str(exc).strip()
    return text or type(exc).__name__


def _node_key(value: Any) -> Any:
    """Normalise a node id (int or hex string on the wire) for comparison.

    Strings go through base-0 parsing first (handles both a ``0x``/``0X``-
    prefixed hex string and a plain decimal one) — but base-0 rejects a
    zero-padded decimal string ("0000000012344321": leading zeros are only
    legal for "0" itself or an "0x"/"0o"/"0b" prefix), so that ValueError
    falls back to a plain base-10 parse when the string is all digits
    (``.isdecimal()``, not ``.isdigit()`` — the latter accepts characters
    ``int(s, 10)`` rejects, e.g. the superscript "²", which would otherwise
    let a ValueError escape instead of falling back to a ``str`` key).
    An ALL-DIGIT unprefixed hex string ("1234" meaning hex, not decimal
    1234): an UNPADDED one parses as DECIMAL via the initial ``int(value, 0)``
    call and never even reaches this fallback; a ZERO-PADDED one ("01234")
    fails that first parse too (leading zeros are illegal there) and DOES
    reach this fallback, where it also parses as decimal. Either way it CAN
    in principle falsely collide with the equivalent decimal id; only a
    LETTER-bearing unprefixed hex string ("1abc") fails both parses and
    stays a ``str`` key — a one-directional no-match against the equivalent
    int, never a false collision. The mitigation is NOT that
    :func:`node_id_to_str` sanitises this — it only formats ``int`` node ids
    with the ``0x`` prefix, and a ``str`` node id passes straight through it
    unchanged — the mitigation is that the codebase never mints an
    unprefixed hex string by hand in the first place, so no caller
    constructs one for either function to collide against.
    """
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            stripped = value.strip()
            if stripped and stripped.isdecimal():
                return int(stripped, 10)
            return str(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _same_node(a: Any, b: Any) -> bool:
    return _node_key(a) == _node_key(b)


def _node_int(value: Any) -> Optional[int]:
    """Coerce a node id to ``int`` via :func:`_node_key`'s policy, or ``None``
    when it can't be — a LETTER-bearing unprefixed hex string is what
    ``_node_key`` deliberately leaves as ``str`` (an all-digit unprefixed
    string parses as decimal instead, per :func:`_node_key`'s own docstring)
    (#A1: the seam into ``device_sync.knows_node``, which does an unguarded
    ``int(node_id)``, must never see one of those raw)."""
    key = _node_key(value)
    return key if isinstance(key, int) else None


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


@dataclass(frozen=True)
class CommissionRequest:
    """The ``context`` :meth:`CommissionJobs._run_job` hands the transport for
    its commission RPC (issue #23) — both the correlation key a late response
    (``ws_json_client.LateResponse.context``) is matched back to a job with,
    and the human-readable note an un-awaited request's log line shows.
    """
    job_id: str

    def __str__(self) -> str:
        return f"commission job {self.job_id}"


@dataclass
class Job:
    job_id: str
    setup_code: str
    suggested_name: str
    suggested_room: Optional[str]
    discriminator: Optional[int] = None
    domio_node_id: Optional[str] = None
    expected_fabric_slots: Optional[int] = None
    # The matter-server node this job is holding — set the moment
    # commission_with_code returns one, again when a reconcile claims a node,
    # and (#23) when a late matter-server success names it for a job that
    # never itself commissioned one — the only writer that can leave a
    # bare-timeout job carrying a node_id for _remove_orphaned_node to find.
    # Distinct from result["nodeId"] (only exists after SUCCESS, and is a hex
    # string): this is the raw id, live from the moment the fabric has it, so
    # _fail can tell whether another job now owns it (#21).
    node_id: Optional[int] = None
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
        knows_node: Optional[Callable[[Any], bool]] = None,
        sleep: Callable[[float], Awaitable] = asyncio.sleep,
    ) -> None:
        self.matter = matter
        self._create_devices = create_devices
        self.logger = logger
        self._schedule = schedule
        self._clock = clock
        self._uuid = uuid_factory
        self._knows_node = knows_node
        self._sleep = sleep
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._by_code: dict[str, str] = {}
        # Strong refs to the #24 reconcile-window watchdogs so they aren't GC'd
        # mid-sleep (the same trap ws_json_client._reconcile_task guards
        # against) — discarded via their own done callback once each finishes.
        self._expiry_tasks: set = set()
        # _node_key values of nodes whose remove_node RPC is currently in
        # flight (from _fail or _remove_orphaned_node) — checked by
        # _claimable_locked so a node_added racing that RPC can't be claimed
        # onto a job the instant before the node is deleted off the fabric
        # (A2: TOCTOU between the "is this node free?" check and the await).
        # Populated/cleared under self._lock; never held across the await.
        self._removing: set = set()

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
                expected_fabric_slots=_opt_int(params.get("expectedFabricSlots")),
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

        Only jobs that went terminal with ``commissioning_timeout`` within
        RECONCILE_WINDOW qualify, and a candidate is claimed only when it can
        be attributed to this node unambiguously (see ``_claimable_locked``:
        exact node-id identity, or the sole unidentified retry of one setup
        code). Returns the claimed jobId, or None if no job qualified.
        """
        if not isinstance(raw_node, dict) or raw_node.get("node_id") is None:
            self._log_malformed_node_added()
            return None
        node_id = raw_node.get("node_id")
        stale: Optional[Job] = None
        removal_in_flight = False
        with self._lock:
            job, candidates, removal_in_flight = self._claimable_locked(
                node_id, self._clock() - RECONCILE_WINDOW)
            if job is None:
                if not removal_in_flight and not candidates:
                    expired = self._timeout_candidates_locked(None)
                    stale = expired[-1] if expired else None
            else:
                # Claim it inside the lock (a second node_added must not double-claim):
                # re-opening to CREATING_DEVICES is honest — that's the work left.
                # Keep the priors (one tuple, not five locals) so a failed schedule
                # can restore the terminal timeout state exactly (including the
                # original terminal_at).
                priors = (job.error, job.terminal_at, job.progress, job.message, job.node_id)
                job.error = None
                job.terminal_at = None
                job.progress = 0.85
                job.message = "Device joined after timeout; creating Indigo devices…"
                job.node_id = node_id
                job.status = CREATING_DEVICES
        if job is None:
            if removal_in_flight:
                # A2: this node's own remove_node RPC (from _fail or the #24
                # watchdog) is out on the loop right now — claiming it here
                # would flip a job to success out from under a node about to
                # be deleted off the fabric. R5: name the refused
                # candidate(s) so a compound outcome (this refusal, then a
                # later failed removal) is traceable back to who wanted the
                # node — and don't foreclose the future: once the removal
                # clears and this node's key is discarded, a later
                # node_added for it CAN still be claimed (reconcile window
                # permitting), so a fresh commission isn't the only way back.
                # Only name candidates that could actually have claimed this
                # node — an already-identified candidate waiting on a
                # DIFFERENT node was never eligible and was never refused;
                # naming it here would be misleading (mirrors the filter
                # _log_reconcile_refusal already applies).
                relevant = [c for c in candidates if c.node_id is None or _same_node(c.node_id, node_id)]
                if relevant:
                    who = "refused candidate job(s): " + ", ".join(c.job_id for c in relevant)
                else:
                    who = "no other candidate is currently in the reconcile window"
                self.logger.warning(
                    "node %s joined while its removal was already in flight "
                    "— not claimed (%s); if the removal succeeds the node is "
                    "gone from the fabric for now, but a later node_added for "
                    "it can still be claimed once the removal clears "
                    "(reconcile window permitting).",
                    node_id_to_str(node_id), who,
                )
            elif stale is not None:
                self.logger.warning(
                    "node %s joined after commission job %s timed out, but outside "
                    "the %.0f-minute reconcile window — suggestedName/suggestedRoom "
                    "were not applied", node_id_to_str(node_id), stale.job_id,
                    RECONCILE_WINDOW.total_seconds() / 60,
                )
            elif candidates:
                self._log_reconcile_refusal(node_id, candidates)
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
                job.error, job.terminal_at, job.progress, job.message, job.node_id = priors
            self.logger.error(
                "commission job %s: node %s joined but the reconcile could not be "
                "scheduled (%s); job restored to its timed-out state",
                job.job_id, node_id_to_str(node_id), exc,
            )
            return None
        return job.job_id

    def _log_malformed_node_added(self) -> None:
        """A node_added carried no usable node_id — log evidence if any
        timeout candidate was waiting, worded differently depending on
        whether it could have been named directly.

        A8: an UNIDENTIFIED candidate is named directly — this malformed
        event was its one possible shot. An IDENTIFIED candidate can't be
        named the same way (it's already attributed to a different node),
        but R4: the broken event COULD still have been exactly the join it
        awaits, so it's still worth a (softer, unnamed-node) warning rather
        than silence. (No candidates at all = routine restart sync; stay
        quiet.)
        """
        with self._lock:
            waiting = self._timeout_candidates_locked(self._clock() - RECONCILE_WINDOW)
            unidentified = [c for c in waiting if c.node_id is None]
        if unidentified:
            self.logger.warning(
                "node_added event without a usable node_id ignored while "
                "commission job %s awaits reconcile", unidentified[-1].job_id)
        elif waiting:
            self.logger.warning(
                "node_added event without a usable node_id ignored; "
                "commission job(s) %s await reconcile on known nodes — "
                "if this event was one of theirs it cannot be matched",
                ", ".join(c.job_id for c in waiting),
            )

    def _log_reconcile_refusal(self, node_id: Any, candidates: list[Job]) -> None:
        """Explain why none of ``candidates`` (all inside the reconcile window)
        could be claimed for ``node_id``. Called outside ``self._lock``."""
        unidentified = [c for c in candidates if c.node_id is None]
        if unidentified:
            # The final refusal branch of _claimable_locked: two or more
            # different setup codes are waiting unidentified — the event
            # names neither.
            self.logger.warning(
                "node %s joined while commission jobs %s were all waiting to "
                "reconcile — it cannot be told which one it belongs to, so none "
                "was claimed; suggestedName/suggestedRoom were not applied. "
                "Rename in Indigo, or commission one device at a time.",
                node_id_to_str(node_id), ", ".join(c.job_id for c in unidentified),
            )
        else:
            # Every candidate is already identified with a DIFFERENT node.
            self.logger.info(
                "node %s joined; no waiting commission job is waiting for it (%s)",
                node_id_to_str(node_id),
                ", ".join(
                    f"job {c.job_id} is waiting for node {node_id_to_str(c.node_id)}"
                    for c in candidates
                ),
            )

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

    def _claimable_locked(
        self, node_id: Any, cutoff: datetime
    ) -> tuple[Optional[Job], list[Job], bool]:
        """Which timeout candidate (if any) may claim ``node_id``, the full
        candidate list (for refusal logging), and whether the refusal is
        specifically because that node's own removal RPC is in flight (A2 —
        a distinct refusal shape callers must log differently: the node is
        being deleted, not merely unclaimed). Caller must hold ``self._lock``.
        """
        candidates = self._timeout_candidates_locked(cutoff)
        if _node_key(node_id) in self._removing:
            # A2: this node's remove_node RPC (via _fail or the #24 watchdog)
            # is out on the loop right now. Claiming it here — by ANY of the
            # branches below — would flip a job to success out from under a
            # node about to vanish off the fabric mid-await. Checked BEFORE
            # the empty-candidates return below (R1): in production timing
            # the #24 watchdog's own job has typically just aged out of the
            # reconcile window by the time its removal RPC is in flight, so
            # with no other candidates this gate must still win rather than
            # going unconsulted.
            return None, candidates, True
        if not candidates:
            return None, candidates, False
        # Exact identity wins outright: matter-server has already named this
        # job's node (e.g. a prior partial reconcile).
        exact = [c for c in candidates if c.node_id is not None and _same_node(c.node_id, node_id)]
        if exact:
            return exact[-1], candidates, False
        # A job whose node we KNOW, and it isn't this one, cannot claim this
        # node — only jobs with an unidentified node are still eligible.
        unidentified = [c for c in candidates if c.node_id is None]
        if not unidentified:
            return None, candidates, False
        # Same setup code among the unidentified = same physical device: retries
        # of one join, and the newest carries the user's latest name/room.
        if len({c.setup_code for c in unidentified}) == 1:
            return unidentified[-1], candidates, False
        # Two DIFFERENT devices are waiting and this event names neither.
        # Refusing costs a name and a room; guessing wrong flips a job to
        # success carrying another device's nodeId, which Domio then trusts.
        return None, candidates, False

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
    # Late RPC response (issue #23) — matter-server answers the commission
    # RPC after the plugin already gave up waiting on it.
    # ------------------------------------------------------------------
    def note_late_response(self, late: Any) -> Optional[str]:
        """Fold a late matter-server answer to a commission RPC into the job
        table. ``late`` is a :class:`ws_json_client.LateResponse`, delivered
        via ``MatterClient``'s ``on_late_response`` hook — ``late.context`` is
        the :class:`CommissionRequest` :meth:`_run_job` sent the request with.

        Runs on the loop thread: synchronous, no awaits, and deliberately
        never calls :meth:`_advance` — this is not a state transition, it is
        correcting what an ALREADY-terminal job says about why. matter-server
        has no "commissioning failed" *event* — the RPC's own answer, however
        late, is the only definitive word this plugin will ever get on a
        commission attempt's actual outcome, which is why a late error is
        worth rewriting a timed-out job's error for even after Domio has
        stopped polling it. A late success does NOT revive a failed job
        (that is ``reconcile_node_added``'s job, driven by the node_added
        event) — it only ever records the node id (P4). That id feeds #22's
        exact-identity attribution (``_claimable_locked``) the moment it
        lands, and later #24's cleanup (``_remove_orphaned_node``), where
        the #21 ownership guard (``_node_held_by_other_job``) is consulted
        WITH the id as its argument — but recording it does NOT itself make
        the job a #21 *holder*: that guard's holder set only counts
        non-FAILED jobs, and a job a late success lands on stays FAILED, so
        it never blocks another job's cleanup the way a timely commission's
        node_id would while its own job is still non-terminal.

        First-writer-wins (P5): if ``job.node_id`` is already set (a prior
        reconcile, or an earlier late success) and this late success names a
        DIFFERENT node, the recorded id is never overwritten — but the
        disagreement is not silently dropped either;
        :meth:`_note_late_success_locked` WARNs, so a real #22
        mis-attribution bug can't hide behind this policy.

        Returns the matched job's id, or ``None`` if ``late`` names no job
        this table is still tracking (or names no job at all).
        """
        if not isinstance(late.context, CommissionRequest):
            return None
        job_id = late.context.job_id
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                self.logger.debug(
                    "late matter-server commission response for job %s: no longer "
                    "tracked (already reaped)", job_id)
                return None
            if late.error is not None:
                self._note_late_error_locked(job, late.error)
            else:
                self._note_late_success_locked(job, late.result)
        return job_id

    def _note_late_error_locked(self, job: Job, error: tuple) -> None:
        """Caller must hold ``self._lock``. See :meth:`note_late_response`."""
        code, details = error
        if job.status is FAILED and (job.error or {}).get("code") == "commissioning_timeout":
            # The one case this exists for: the RPC gave up waiting (issue #16's
            # commissioning_timeout) and matter-server's late answer says the
            # attempt actually failed. terminal_at is deliberately NOT
            # re-stamped — the job has been terminal since the timeout; only
            # what it reports about WHY changes.
            new_error = {
                "code": "commissioning_failed",
                "message": (
                    "matter-server reported the commission failed after the "
                    f"plugin stopped waiting: {details}"
                ),
            }
            matter_error_code = _opt_int(code)
            if matter_error_code is not None:
                new_error["matterErrorCode"] = matter_error_code
            job.error = new_error
            self.logger.warning(
                "commission job %s: matter-server's late answer says the "
                "commission actually failed (%s: %s) — the recorded error has "
                "been updated even though the job already timed out",
                job.job_id, code, details,
            )
        elif job.status is SUCCESS:
            # The job already reached success by another path (a timely commission,
            # or reconcile_node_added) — a late error must not unwind that.
            self.logger.info(
                "commission job %s: matter-server's late answer reports an error "
                "(%s: %s), but the job already succeeded — leaving it alone",
                job.job_id, code, details,
            )
        else:
            self.logger.debug(
                "commission job %s: late matter-server error (%s: %s) does not "
                "apply to its current state (%s)", job.job_id, code, details, job.status,
            )

    def _note_late_success_locked(self, job: Job, result: Any) -> None:
        """Caller must hold ``self._lock``. See :meth:`note_late_response`."""
        node_id = result.get("node_id") if isinstance(result, dict) else result
        if node_id is None:
            self.logger.debug(
                "commission job %s: late matter-server success carried no "
                "node id — nothing to record", job.job_id,
            )
            return
        if job.node_id is not None:
            if not _same_node(job.node_id, node_id):
                # #22 mis-attribution signal: the RPC's own late answer and
                # whatever already set job.node_id (a prior reconcile, or an
                # earlier late success) disagree about which node this job
                # is. The original wins — see note_late_response's docstring
                # — but disagreeing silently would hide a real bug.
                self.logger.warning(
                    "commission job %s: late matter-server success names "
                    "node %s, but the job already recorded node %s — "
                    "keeping the original", job.job_id,
                    node_id_to_str(node_id), node_id_to_str(job.node_id),
                )
            return
        job.node_id = node_id
        self.logger.info(
            "commission job %s: matter-server's late answer names node %s — "
            "recording it (job status unchanged: %s)",
            job.job_id, node_id_to_str(node_id), job.status,
        )

    # ------------------------------------------------------------------
    # Async worker (runs on the loop)
    # ------------------------------------------------------------------
    async def _run_job(self, job: Job) -> None:
        node_id: Any = None
        try:
            self._advance(job, COMMISSIONING, 0.2, "Commissioning…")
            result = await self.matter.commission_with_code(
                job.setup_code, context=CommissionRequest(job.job_id))
            node_id = result.get("node_id") if isinstance(result, dict) else result
            if node_id is None:
                raise CommissionError("commissioning_failed", "matter-server returned no node_id")
            self._set_node_id(job, node_id)

            self._advance(job, READING_DESCRIPTORS, 0.6, "Discovering device capabilities…")
            node = await self.matter.get_node(node_id)
            self._warn_if_fabric_slots_short(job, node, node_id)

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
                self._watch_reconcile_window(job)
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
        # _node_held_by_other_job, when it returns False (not held), also
        # marks node_id in-flight in self._removing in the SAME lock
        # acquisition as the holder check (A2) — closing the gap a separate
        # add() call afterwards would leave open for a concurrent
        # reconcile_node_added to claim the node between the two.
        if node_id is not None and not self._node_held_by_other_job(job, node_id):
            try:
                await self.matter.remove_node(node_id)  # best-effort cleanup
            except Exception as exc:  # noqa: BLE001 - A7: loud, cleanup is still best-effort
                self.logger.warning(
                    "commission job %s: commissioning failed, and the "
                    "best-effort cleanup removing node %s from the fabric "
                    "also failed (%s); it remains on the fabric and may "
                    "occupy a fabric slot on the device — remove it via "
                    "matter-server or factory-reset the device before "
                    "retrying.", job.job_id, node_id_to_str(node_id), exc,
                )
            finally:
                with self._lock:
                    self._removing.discard(_node_key(node_id))
        error = {"code": code, "message": message}
        if matter_error_code is not None:
            error["matterErrorCode"] = matter_error_code
        job.error = error
        self._advance(job, FAILED, job.progress, "")

    # ------------------------------------------------------------------
    # Reconcile-window expiry (issue #24) — when RECONCILE_WINDOW closes with
    # no node_added claim, say so definitively instead of reaping in silence.
    # ------------------------------------------------------------------
    def _watch_reconcile_window(self, job: Job) -> None:
        """Schedule the RECONCILE_WINDOW watchdog for a job that just landed
        in a bare commissioning_timeout.

        Best-effort: if the loop is gone (``schedule`` raises ``RuntimeError``)
        the job is left exactly as ``_run_job`` just landed it — the timeout
        itself is already recorded and logged; losing only the later "window
        closed" log is preferable to disturbing that state.
        """
        coro = self._expire_reconcile_window(job)
        try:
            handle = self._schedule(coro)
        except RuntimeError:
            coro.close()
            self.logger.debug(
                "commission job %s: reconcile-window watchdog not armed "
                "(loop shutting down); the window-expiry log will not fire.",
                job.job_id,
            )
            return
        self._expiry_tasks.add(handle)
        handle.add_done_callback(self._expiry_tasks.discard)

    async def _expire_reconcile_window(self, job: Job) -> None:
        """Wait out RECONCILE_WINDOW and, if the job is still exactly where
        the bare timeout left it, log that the window closed for good — with
        different wording depending on whether a late #23 success ever named
        a node for this job (A4) — and best-effort clean up that node if it
        did.

        Any exception other than ``CancelledError`` is caught here and
        logged via ``self.logger.exception`` (A1/P2) — not because this is
        the only fire-and-forget coroutine in the module (``_run_job`` and
        ``_reconcile_job`` are scheduled the same way, with no caller ever
        retrieving their result either), but because THOSE are
        exception-total FOR ``Exception``-CLASS RAISES by construction:
        every one of their ``except`` clauses lands the job in a terminal
        state. That claim is narrower than "nothing can escape past them"
        at all: ``_run_job``'s own cancellation clause deliberately
        RE-RAISES ``CancelledError`` after landing FAILED, by design, and a
        cancellation landing INSIDE its subsequent ``await self._fail(...)``
        can still propagate the task out non-terminal — ``CancelledError``
        has been a ``BaseException`` subclass since Python 3.8, so it was
        never covered by the "exception-total" claim to begin with. This
        coroutine's body was not written that way, so an ``Exception``-class
        raise that escaped — e.g. from ``knows_node`` inside
        ``_remove_orphaned_node`` — would otherwise vanish silently.

        ``CancelledError`` is deliberately allowed to propagate — catching
        ``Exception`` doesn't touch it; it has been a ``BaseException``
        subclass since Python 3.8. At shutdown ``AsyncRuntime._drain_and_close``
        cancels every outstanding task and awaits them via
        ``asyncio.gather(..., return_exceptions=True)`` — the gather
        swallows the exception there, and the only reference to the
        ``Future`` this coroutine runs as is ``_expiry_tasks``' GC guard (it
        is fire-and-forget from ``_watch_reconcile_window``) — nothing ever
        calls ``.result()`` on it, so no caller sees it propagate. The job is
        already terminal; a cancellation here leaves no state to land.
        """
        try:
            await self._sleep(RECONCILE_WINDOW.total_seconds())
            with self._lock:
                current = self._jobs.get(job.job_id)
                if current is not job:
                    return  # reaped in the meantime
                if (job.status is not FAILED
                        or (job.error or {}).get("code") != "commissioning_timeout"):
                    return  # reconciled by node_added, or #23 re-coded the error
                node_id = job.node_id
            if node_id is not None:
                # A #23 late success named a node for this job AFTER it went
                # terminal on a bare timeout. This is only the ANNOUNCE that
                # the window closed with a fabric entry to check — it must
                # not claim an outcome, because _remove_orphaned_node's two
                # veto guards (holder check, Indigo-tracked check) are both
                # reachable from here via ordinary flows: the verdict
                # (held / tracked / removed / removal-failed) belongs to
                # that method's own per-outcome log lines. Distinct wording
                # (A4) from the bare-case text below, which docs/MATTER.md
                # quotes (minus the interpolated window length).
                self.logger.warning(
                    "commission job %s: the reconcile window closed; "
                    "matter-server's late answer had named node %s for "
                    "this job, so its fabric entry is checked and cleaned "
                    "up if nothing owns it.",
                    job.job_id, node_id_to_str(node_id),
                )
                await self._remove_orphaned_node(job, node_id)
            else:
                self.logger.warning(
                    "commission job %s: the %.0f-minute reconcile window closed and "
                    "the device never joined — commissioning did not complete. "
                    "matter-server has no way to cancel a commissioning attempt, so "
                    "nothing further will be done automatically; check the device is "
                    "in pairing mode and retry.",
                    job.job_id, RECONCILE_WINDOW.total_seconds() / 60,
                )
        except Exception as exc:  # noqa: BLE001 - A1: nothing else can ever see this raise
            self.logger.exception(exc)

    async def _remove_orphaned_node(self, job: Job, node_id: Any) -> None:
        """Best-effort cleanup of a node a late #23 success recorded for a job
        whose reconcile window has now closed unclaimed.

        ``knows_node`` is the right test HERE — unlike ``_fail``'s guard
        (#21), which deliberately does NOT consult it (device_sync knows
        about the node this very job just created, so it would suppress
        every normal-path removal). Here the question is different: did
        ANYTHING ever adopt this node? ``node_id`` can only be set on a
        bare-timeout job via #23's late success — a timely join sets it
        itself and the job reaches SUCCESS, never this path — so a node_id
        surviving to here is strictly the leftover of a join nobody
        completed.

        ``node_id`` may be a string straight off the wire (#23's late
        success never coerces it) — ``knows_node`` (``device_sync``) does an
        unguarded ``int(node_id)`` (A1), so it is only called with a value
        ``_node_int`` could actually coerce; an uncoercible id skips that
        check (logged) but the fabric-removal RPC below still runs — the
        other two guards (holder check, reconcile window closed) already
        cleared it.
        """
        if self._node_held_by_other_job(job, node_id):
            return  # already logs; nothing was marked in-flight
        # _node_held_by_other_job marks node_id in self._removing (A2) the
        # moment it returns False — from here on, discard it on every exit,
        # including an exception raised out of the knows_node check.
        try:
            node_int = _node_int(node_id)
            checked = node_int is not None
            if not checked:
                # R3: this skip means the destructive remove_node below runs
                # without the Indigo-tracked safety check ever having run —
                # worth a WARNING, not DEBUG.
                self.logger.warning(
                    "commission job %s: removing node %s without being "
                    "able to confirm Indigo doesn't track it — id not "
                    "coercible to an integer", job.job_id, node_id,
                )
            elif self._knows_node is not None and self._knows_node(node_int):
                self.logger.info(
                    "commission job %s: node %s is already tracked by "
                    "Indigo; leaving it on the fabric",
                    job.job_id, node_id_to_str(node_id),
                )
                return
            try:
                await self.matter.remove_node(node_id)
                if checked:
                    self.logger.warning(
                        "commission job %s: removed orphaned node %s — "
                        "matter-server reported it joined but it never "
                        "reached Indigo",
                        job.job_id, node_id_to_str(node_id),
                    )
                else:
                    self.logger.warning(
                        "commission job %s: removed orphaned node %s — "
                        "matter-server reported it joined; Indigo tracking "
                        "could not be checked for this id",
                        job.job_id, node_id_to_str(node_id),
                    )
            except Exception as exc:  # noqa: BLE001 - A5: loud, cleanup is still best-effort
                self.logger.warning(
                    "commission job %s: could not remove orphaned node %s "
                    "(%s); it remains on the fabric and may occupy a fabric "
                    "slot on the device — remove it via matter-server or "
                    "factory-reset the device before retrying.",
                    job.job_id, node_id_to_str(node_id), exc,
                )
        finally:
            with self._lock:
                self._removing.discard(_node_key(node_id))

    def _warn_if_fabric_slots_short(self, job: Job, node: dict, node_id: Any) -> None:
        """API.md §3.2 `expectedFabricSlots`: log a warning (never blocking) if
        the device's post-join fabric capacity is under what Domio expects.
        Silent when the hint is absent or the interview snapshot didn't include
        the Operational Credentials attributes (older firmware, partial read)."""
        if job.expected_fabric_slots is None:
            return
        available = _fabric_slots_available(node)
        if available is None or available >= job.expected_fabric_slots:
            return
        self.logger.warning(
            "commission job %s: node %s reports only %d fabric slot(s) available "
            "after joining, but Domio expected at least %d (expectedFabricSlots)",
            job.job_id, node_id_to_str(node_id), available, job.expected_fabric_slots,
        )

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

    def _set_node_id(self, job: Job, node_id: Any) -> None:
        # get_job reads Job fields under self._lock from the HTTP thread — every
        # write to node_id must go through the lock too.
        with self._lock:
            job.node_id = node_id

    def _node_held_by_other_job(self, job: Job, node_id: Any) -> bool:
        """Is ``node_id`` currently held by some OTHER (non-failed) job?

        Ownership beats cleanup: this is the #21 scenario — a commission job
        times out, a node_added reconciles it onto the node (job.node_id set),
        and then a LATER step in a retry/second job fails and would otherwise
        remove_node the very node the first job just claimed. If another job
        holds it and hasn't itself failed, leave it on the fabric.

        A FAILED holder has given the node up — its claim no longer counts,
        so removal proceeds as normal.

        Deliberately does NOT consult device_sync.knows_node: device_sync
        creates devices for every node_added, including the one this very job
        just commissioned, so knows_node is true immediately after every
        successful join — it would suppress remove_node on the entire normal
        failure path (a later step failing after a lone job's own commission
        succeeded), contradicting PRD §7's "clean up what we created."

        A2: when this returns ``False`` (not held — the caller is about to
        remove the node), the holder check and marking ``node_id`` in-flight
        in ``self._removing`` happen in the SAME lock acquisition, so there
        is no window between "confirmed free" and "marked in-flight" for a
        concurrent ``reconcile_node_added`` to slip through. The caller must
        discard the key (``self._removing.discard(_node_key(node_id))``,
        under the lock) once its own removal has settled, in a ``finally``.

        R7: this assumes at most one removal of a given node in flight at a
        time — ``self._removing`` is a plain set, not a refcount, so a
        SECOND concurrent removal of the same node would discard the key
        out from under the first when it (the second) finishes, reopening
        the A2 window early. This holder check never consults
        ``self._removing`` — only ``self._jobs``' non-FAILED node_id
        matches — so it does not interlock the two existing callers
        symmetrically, and the interlock it DOES provide is directional:
        if ``_fail`` runs first, its job is still non-FAILED with node_id
        set, so it IS a holder the second caller's own check would see and
        back off from; if ``_remove_orphaned_node`` runs first there is no
        holder-check interlock at all. What actually makes that second
        ordering unreachable today is not this method — it is that
        ``_remove_orphaned_node`` only ever runs on a node_id that arrived
        via a #23 late success, which requires matter-server to name the
        SAME node in two different commission RPC answers to even set up
        the race. A third caller of this method, or any change that lets
        two removals of the same node genuinely race, must not rely on
        that without upgrading ``_removing`` to a refcount.
        """
        with self._lock:
            holders = [
                j.job_id for j in self._jobs.values()
                if j is not job and j.node_id is not None
                and j.status is not FAILED and _same_node(j.node_id, node_id)
            ]
            if not holders:
                self._removing.add(_node_key(node_id))
        if not holders:
            return False
        self.logger.warning(
            "commission job %s failed, but node %s is held by commission job %s — "
            "leaving it on the fabric rather than removing a device another job "
            "just created", job.job_id, node_id_to_str(node_id), ", ".join(holders),
        )
        return True

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
