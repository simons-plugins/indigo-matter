"""M3: commissioning job state machine — validation, dedup, worker, retention."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import commission_jobs
from commission_jobs import (
    TIMEOUT_MESSAGE,
    CommissionError,
    CommissionJobs,
    _exc_message,
    is_valid_setup_code,
    node_id_to_str,
)


class FakeMatter:
    def __init__(self, node_id=0xAB, node=None, connected=True):
        self.node_id = node_id
        self.node = node or {"endpoints": {}}
        self.removed: list = []
        self.connected = connected

    async def commission_with_code(self, code):
        return {"node_id": self.node_id}

    async def get_node(self, node_id):
        return self.node

    async def remove_node(self, node_id):
        self.removed.append(node_id)


async def _good_create(node, name, room):
    return {"indigoDeviceIds": [111], "primaryDeviceId": 111, "endpointCount": 1}


def _jobs(matter, mock_logger, create=_good_create, schedule=None, clock=None):
    return CommissionJobs(
        matter, create, mock_logger,
        schedule=schedule if schedule is not None else (lambda c: c.close()),
        clock=clock or (lambda: datetime.now(timezone.utc)),
    )


# ----------------------------------------------------------------------
# Validation (sync)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("code,ok", [
    ("MT:Y.ABCDEFG123456789", True),
    ("12345678901", True),
    ("123456789012345678901", True),
    ("1234", False),
    ("notdigits", False),
    ("", False),
])
def test_is_valid_setup_code(code, ok):
    assert is_valid_setup_code(code) is ok


def test_node_id_to_str():
    assert node_id_to_str(0xAB) == "0xAB"
    assert node_id_to_str("0xCD") == "0xCD"


def test_create_job_rejects_bad_setup_code(mock_logger):
    jobs = _jobs(FakeMatter(), mock_logger)
    code, body = jobs.create_job({"setupCode": "bad", "suggestedName": "Plug"})
    assert code == 400 and body["error"] == "invalid_setup_code"


def test_create_job_requires_name(mock_logger):
    jobs = _jobs(FakeMatter(), mock_logger)
    code, body = jobs.create_job({"setupCode": "12345678901"})
    assert code == 400


def test_create_job_dedups_in_flight_by_setup_code(mock_logger):
    # schedule = close() leaves the job pending (non-terminal) → dedup
    jobs = _jobs(FakeMatter(), mock_logger)
    code1, body1 = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
    code2, body2 = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
    assert code1 == 202
    assert code2 == 409
    assert body2["existingJobId"] == body1["jobId"]


# ----------------------------------------------------------------------
# Worker (async, driven on a real loop)
# ----------------------------------------------------------------------
def _await_terminal(jobs, job_id):
    async def _poll():
        for _ in range(100):
            _, body = jobs.get_job(job_id)
            if body.get("status") in ("success", "failed"):
                return body
            await asyncio.sleep(0.005)
        return jobs.get_job(job_id)[1]
    return _poll


def test_commission_success_path(mock_logger):
    async def scenario():
        matter = FakeMatter(node_id=0xABCDEF)
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        code, body = jobs.create_job({"setupCode": "12345678901",
                                      "suggestedName": "Office Fan", "suggestedRoom": "Office"})
        assert code == 202
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "success"
        assert final["progress"] == 1.0
        assert final["result"]["nodeId"] == "0xABCDEF"
        assert final["result"]["primaryDeviceId"] == 111
    asyncio.run(scenario())


def test_commission_failure_removes_node_and_reports_code(mock_logger):
    async def scenario():
        matter = FakeMatter(node_id=99)

        async def failing_create(node, name, room):
            raise CommissionError("interview_failed", "could not read descriptors")

        jobs = _jobs(matter, mock_logger, create=failing_create, schedule=asyncio.ensure_future)
        code, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "interview_failed"
        # best-effort cleanup removed the node from the fabric
        assert matter.removed == [99]
    asyncio.run(scenario())


def test_create_job_schedule_failure_returns_503_and_frees_code(mock_logger):
    # loop down → schedule raises; must 503 and NOT strand a pending job that
    # would lock the setup code out of all future commissions.
    def boom(coro):
        raise RuntimeError("asyncio runtime is not running")

    jobs = _jobs(FakeMatter(), mock_logger, schedule=boom)
    code, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
    assert code == 503 and body["error"] == "matter_server_unreachable"
    # a retry is treated as new (503 again), not 409 duplicate
    code2, _ = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
    assert code2 == 503


def test_create_job_rejected_503_when_matter_server_disconnected(mock_logger):
    # WS to matter-server down → fail fast with the contract envelope, do NOT
    # accept a job that can only die with a generic internal_error.
    jobs = _jobs(FakeMatter(connected=False), mock_logger)
    code, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
    assert code == 503
    assert body["error"] == "matter_server_unreachable"
    assert body["message"]  # actionable, non-empty


def test_cancelled_worker_fails_job_and_frees_setup_code(mock_logger):
    # Plugin shutdown cancels the worker mid-commission: the job must land in
    # FAILED (terminal → reapable) and free the setupCode for a retry, while the
    # task itself still cancels.
    async def scenario():
        started = asyncio.Event()

        class HangingMatter(FakeMatter):
            async def commission_with_code(self, code):
                started.set()
                await asyncio.Event().wait()  # blocks forever until cancelled

        tasks: list[asyncio.Task] = []

        def schedule(coro):
            task = asyncio.ensure_future(coro)
            tasks.append(task)
            return task

        jobs = _jobs(HangingMatter(), mock_logger, schedule=schedule)
        code, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
        assert code == 202
        await asyncio.wait_for(started.wait(), timeout=1)

        _, mid = jobs.get_job(body["jobId"])
        assert mid["status"] == "commissioning"

        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]  # the task really cancelled (CancelledError re-raised)

        _, final = jobs.get_job(body["jobId"])
        assert final["status"] == "failed"
        assert final["error"]["code"] == "internal_error"
        assert "cancelled" in final["error"]["message"]
        job = jobs._jobs[body["jobId"]]
        assert job.terminal_at is not None  # reapable

        # the setup code is freed: a retry is a NEW job, not a 409 duplicate
        code2, body2 = jobs.create_job({"setupCode": "12345678901", "suggestedName": "Plug"})
        assert code2 == 202 and body2["jobId"] != body["jobId"]
        tasks[1].cancel()
    asyncio.run(scenario())


def test_protocol_error_maps_to_commissioning_failed(mock_logger):
    from protocol import ProtocolError

    async def scenario():
        class PEMatter(FakeMatter):
            async def commission_with_code(self, code):
                raise ProtocolError(50, "PASE failed")

        jobs = _jobs(PEMatter(), mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "commissioning_failed"
        assert final["error"]["matterErrorCode"] == 50
    asyncio.run(scenario())


def test_missing_node_id_maps_to_commissioning_failed(mock_logger):
    async def scenario():
        class NoIdMatter(FakeMatter):
            async def commission_with_code(self, code):
                return {}  # server returned no node_id (PASE/CASE failure)

        jobs = _jobs(NoIdMatter(), mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "commissioning_failed"
    asyncio.run(scenario())


def test_generic_exception_maps_to_internal_error(mock_logger):
    async def scenario():
        class BoomMatter(FakeMatter):
            async def commission_with_code(self, code):
                raise RuntimeError("kaboom")

        jobs = _jobs(BoomMatter(), mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "internal_error"
    asyncio.run(scenario())


def test_exc_message_falls_back_to_type_name():
    # str(exc) is "" for bare exceptions — the payload must never be blank (#17)
    assert _exc_message(RuntimeError("kaboom")) == "kaboom"
    assert _exc_message(TimeoutError()) == "TimeoutError"
    assert _exc_message(ValueError("   ")) == "ValueError"


def test_bare_exception_failure_has_nonempty_message(mock_logger):
    async def scenario():
        class BareBoomMatter(FakeMatter):
            async def commission_with_code(self, code):
                raise ValueError  # str() == "" — same class of bug as #17

        jobs = _jobs(BareBoomMatter(), mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "internal_error"
        assert final["error"]["message"] == "ValueError"
    asyncio.run(scenario())


def test_recommission_allowed_after_terminal(mock_logger):
    async def scenario():
        jobs = _jobs(FakeMatter(node_id=0x1), mock_logger, schedule=asyncio.ensure_future)
        _, body1 = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body1["jobId"])()
        # same code after a terminal job is a NEW job, not a 409 duplicate
        code2, body2 = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        assert code2 == 202 and body2["jobId"] != body1["jobId"]
    asyncio.run(scenario())


class TimeoutMatter(FakeMatter):
    async def commission_with_code(self, code):
        raise TimeoutError  # asyncio.wait_for expiring (str() == "")


def test_commission_timeout_maps_to_commissioning_timeout(mock_logger):
    # The single most likely real-world failure (observed live: 60s timeout vs
    # ~124s actual commission) must NOT report internal_error/"" (#17), and must
    # NOT remove the node — matter-server may still be joining it (#16).
    async def scenario():
        matter = TimeoutMatter()
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "commissioning_timeout"
        assert "may still join" in final["error"]["message"]
        assert "300" in final["error"]["message"]  # names the actual deadline
        assert matter.removed == []  # never tear down an in-flight join
        # the timeout is logged so the event log holds evidence even after
        # Domio stops polling the job
        mock_logger.warning.assert_called()
        logged = str(mock_logger.warning.call_args)
        assert body["jobId"] in logged and "may still join" in logged
    asyncio.run(scenario())


def test_get_node_timeout_after_commission_is_internal_error(mock_logger):
    # Commissioning SUCCEEDED but the follow-up get_node (10s) timed out: this
    # must NOT be labeled commissioning_timeout ("may still join" is wrong — it
    # already joined, and reconcile can't repair a job that wasn't terminal when
    # node_added fired). The node stays on the fabric: no remove_node.
    async def scenario():
        class SlowDescriptorMatter(FakeMatter):
            async def get_node(self, node_id):
                raise TimeoutError  # the short descriptor-read deadline expired

        matter = SlowDescriptorMatter(node_id=0xAB)
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"
        assert final["error"]["code"] == "internal_error"
        assert "descriptors" in final["error"]["message"]
        assert "0xAB" in final["error"]["message"]
        assert "commissioning succeeded" in final["error"]["message"]
        assert matter.removed == []  # the node joined; don't tear it down
    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Late-join reconcile (#16): node_added after a commissioning_timeout
# ----------------------------------------------------------------------
def test_late_node_added_reconciles_timed_out_job(mock_logger):
    async def scenario():
        calls: list = []

        async def recording_create(node, name, room):
            calls.append((node, name, room))
            return {"indigoDeviceIds": [222], "primaryDeviceId": 222, "endpointCount": 1}

        jobs = _jobs(TimeoutMatter(), mock_logger, create=recording_create,
                     schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901",
                                   "suggestedName": "Hall Lamp", "suggestedRoom": "Hallway"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["error"]["code"] == "commissioning_timeout"

        raw_node = {"node_id": 0xAB, "attributes": {}}
        claimed = jobs.reconcile_node_added(raw_node)
        assert claimed == body["jobId"]

        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "success"
        assert final["progress"] == 1.0
        assert "error" not in final
        assert final["result"]["nodeId"] == "0xAB"
        assert final["result"]["primaryDeviceId"] == 222
        # the user's choices were applied to the late-created devices
        assert calls == [(raw_node, "Hall Lamp", "Hallway")]
        # reconciled job is terminal again → reapable, and won't double-claim
        assert jobs.reconcile_node_added(raw_node) is None
    asyncio.run(scenario())


def test_reconcile_ignores_timeouts_outside_window(mock_logger):
    async def scenario():
        clock = {"t": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)}
        jobs = _jobs(TimeoutMatter(), mock_logger,
                     schedule=asyncio.ensure_future, clock=lambda: clock["t"])
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body["jobId"])()
        # node arrives 6 minutes later — outside the bounded reconcile window
        clock["t"] += timedelta(minutes=6)
        mock_logger.warning.reset_mock()
        assert jobs.reconcile_node_added({"node_id": 1}) is None
        _, final = jobs.get_job(body["jobId"])
        assert final["status"] == "failed"  # untouched
        # …but no longer silent: the log names the job, the node, and that the
        # user's suggestedName/suggestedRoom were not applied
        mock_logger.warning.assert_called_once()
        logged = str(mock_logger.warning.call_args)
        assert body["jobId"] in logged and "0x1" in logged and "not applied" in logged
    asyncio.run(scenario())


def test_reconcile_ignores_non_timeout_failures(mock_logger):
    async def scenario():
        class PEMatter(FakeMatter):
            async def commission_with_code(self, code):
                from protocol import ProtocolError
                raise ProtocolError(50, "PASE failed")

        jobs = _jobs(PEMatter(), mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body["jobId"])()
        # a genuine commissioning failure is final — node_added must not flip it
        assert jobs.reconcile_node_added({"node_id": 1}) is None
    asyncio.run(scenario())


def test_reconcile_claims_most_recent_timeout(mock_logger):
    async def scenario():
        clock = {"t": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)}
        jobs = _jobs(TimeoutMatter(), mock_logger,
                     schedule=asyncio.ensure_future, clock=lambda: clock["t"])
        _, body1 = jobs.create_job({"setupCode": "12345678901", "suggestedName": "A"})
        await _await_terminal(jobs, body1["jobId"])()
        clock["t"] += timedelta(minutes=2)
        _, body2 = jobs.create_job({"setupCode": "98765432109", "suggestedName": "B"})
        await _await_terminal(jobs, body2["jobId"])()

        assert jobs.reconcile_node_added({"node_id": 7}) == body2["jobId"]
        await _await_terminal(jobs, body2["jobId"])()
        _, old = jobs.get_job(body1["jobId"])
        assert old["status"] == "failed"  # older timeout left alone
    asyncio.run(scenario())


def test_reconcile_create_failure_lands_job_failed(mock_logger):
    async def scenario():
        async def failing_create(node, name, room):
            raise RuntimeError("indigo down")

        jobs = _jobs(TimeoutMatter(), mock_logger, create=failing_create,
                     schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body["jobId"])()
        assert jobs.reconcile_node_added({"node_id": 1}) == body["jobId"]
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "failed"  # terminal again, never stranded
        assert final["error"]["code"] == "internal_error"
    asyncio.run(scenario())


def test_reconcile_ignores_malformed_node(mock_logger):
    jobs = _jobs(FakeMatter(), mock_logger)
    assert jobs.reconcile_node_added({}) is None        # no node_id
    assert jobs.reconcile_node_added(None) is None      # not a dict
    # no candidate was waiting — routine restart syncs must not spam the log
    mock_logger.warning.assert_not_called()


def test_malformed_node_warns_when_timeout_candidate_waiting(mock_logger):
    # A claimable timed-out job exists and the malformed node_added was its one
    # shot at reconciling — leave evidence in the log.
    async def scenario():
        jobs = _jobs(TimeoutMatter(), mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body["jobId"])()
        mock_logger.warning.reset_mock()
        assert jobs.reconcile_node_added({"weird": True}) is None
        mock_logger.warning.assert_called_once()
        assert body["jobId"] in str(mock_logger.warning.call_args)
    asyncio.run(scenario())


def test_reconcile_schedule_failure_restores_timeout_state(mock_logger):
    # Loop down when the reconcile is scheduled: the restore must be atomic and
    # exact — original error payload back, terminal_at NOT re-stamped (a fresh
    # stamp would silently extend the documented reconcile window) — and logged.
    async def scenario():
        clock = {"t": datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)}
        sched = {"fail": False}

        def schedule(coro):
            if sched["fail"]:
                raise RuntimeError("asyncio runtime is not running")
            return asyncio.ensure_future(coro)

        jobs = _jobs(TimeoutMatter(), mock_logger, schedule=schedule,
                     clock=lambda: clock["t"])
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["error"]["code"] == "commissioning_timeout"
        original_terminal_at = jobs._jobs[body["jobId"]].terminal_at

        clock["t"] += timedelta(minutes=2)  # later, but still inside the window
        sched["fail"] = True
        assert jobs.reconcile_node_added({"node_id": 1}) is None

        job = jobs._jobs[body["jobId"]]
        assert job.status == "failed"
        assert job.error == {"code": "commissioning_timeout", "message": TIMEOUT_MESSAGE}
        assert job.message == ""  # claim-time message rolled back
        assert job.terminal_at == original_terminal_at  # NOT refreshed to clock now
        mock_logger.error.assert_called()
        assert body["jobId"] in str(mock_logger.error.call_args)

        # once the loop is back the job is still claimable — and succeeds
        sched["fail"] = False
        assert jobs.reconcile_node_added({"node_id": 1}) == body["jobId"]
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "success"
    asyncio.run(scenario())


def test_unknown_job_is_404(mock_logger):
    jobs = _jobs(FakeMatter(), mock_logger)
    code, body = jobs.get_job("nope")
    assert code == 404 and body["error"] == "job_not_found"


# ----------------------------------------------------------------------
# expectedFabricSlots (API.md §3.2): warn on low post-join fabric capacity
# ----------------------------------------------------------------------
def _node_with_fabrics(supported, commissioned, node_id=0x1):
    return {
        "node_id": node_id,
        "attributes": {
            "0/62/2": supported,      # SupportedFabrics
            "0/62/3": commissioned,   # CommissionedFabrics
        },
    }


def test_expected_fabric_slots_warns_when_fewer_available(mock_logger):
    async def scenario():
        matter = FakeMatter(node_id=0x1, node=_node_with_fabrics(5, 4))
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({
            "setupCode": "12345678901", "suggestedName": "X", "expectedFabricSlots": "3",
        })
        final = await _await_terminal(jobs, body["jobId"])()
        assert final["status"] == "success"  # diagnostic only, never blocking
        mock_logger.warning.assert_called_once()
        logged = str(mock_logger.warning.call_args)
        assert body["jobId"] in logged and "fabric slot" in logged
        assert ", 1, 3)" in logged  # available=1, expected=3
    asyncio.run(scenario())


def test_expected_fabric_slots_silent_when_sufficient(mock_logger):
    async def scenario():
        matter = FakeMatter(node_id=0x1, node=_node_with_fabrics(5, 2))
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({
            "setupCode": "12345678901", "suggestedName": "X", "expectedFabricSlots": "3",
        })
        await _await_terminal(jobs, body["jobId"])()
        mock_logger.warning.assert_not_called()
    asyncio.run(scenario())


def test_expected_fabric_slots_silent_when_absent_from_node(mock_logger):
    async def scenario():
        # Interview snapshot has no Operational Credentials attributes at all —
        # unknown capacity must never be treated as zero / trigger a warning.
        matter = FakeMatter(node_id=0x1, node={"node_id": 0x1, "attributes": {}})
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({
            "setupCode": "12345678901", "suggestedName": "X", "expectedFabricSlots": "3",
        })
        await _await_terminal(jobs, body["jobId"])()
        mock_logger.warning.assert_not_called()
    asyncio.run(scenario())


def test_expected_fabric_slots_silent_when_not_requested(mock_logger):
    async def scenario():
        # Low capacity, but Domio didn't send the hint — no warning.
        matter = FakeMatter(node_id=0x1, node=_node_with_fabrics(5, 5))
        jobs = _jobs(matter, mock_logger, schedule=asyncio.ensure_future)
        _, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body["jobId"])()
        mock_logger.warning.assert_not_called()
    asyncio.run(scenario())


def test_terminal_jobs_are_reaped_after_retention(mock_logger):
    async def scenario():
        clock = {"t": datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)}
        jobs = _jobs(FakeMatter(), mock_logger,
                     schedule=asyncio.ensure_future, clock=lambda: clock["t"])
        code, body = jobs.create_job({"setupCode": "12345678901", "suggestedName": "X"})
        await _await_terminal(jobs, body["jobId"])()
        # still present immediately after success
        assert jobs.get_job(body["jobId"])[0] == 200
        # advance past 15-minute retention → reaped
        clock["t"] = clock["t"] + timedelta(minutes=16)
        assert jobs.get_job(body["jobId"])[0] == 404
    asyncio.run(scenario())
