"""M3: HTTP API routing (API.md v1.1) — method/path dispatch, status codes."""
from __future__ import annotations

from http_handlers import HttpApi, parse_node_id


class FakeJobs:
    def __init__(self):
        self.created = []
        self.polled = []

    def create_job(self, params):
        self.created.append(params)
        return 202, {"jobId": "job-1", "estimatedDurationSeconds": 30}

    def get_job(self, job_id):
        self.polled.append(job_id)
        if job_id == "job-1":
            return 200, {"jobId": "job-1", "status": "success"}
        return 404, {"error": "job_not_found"}


def _api(ready=True, decommission=None, diagnostics=None, mock_logger=None):
    import unittest.mock as m
    jobs = FakeJobs()
    api = HttpApi(
        jobs, mock_logger or m.Mock(),
        status_provider=lambda: {"ready": ready, "controllerVersion": "2026.0.1"},
        decommission_provider=decommission or (lambda nid: None),
        diagnostics_provider=diagnostics or (lambda nid: None),
    )
    return api, jobs


def test_parse_node_id():
    assert parse_node_id("0xABCDEF0123456789") == 0xABCDEF0123456789
    assert parse_node_id("42") == 42
    assert parse_node_id("nope") is None


def test_status_ready_is_200():
    api, _ = _api(ready=True)
    code, body = api.status()
    assert code == 200 and body["ready"] is True


def test_status_not_ready_is_503():
    api, _ = _api(ready=False)
    code, _ = api.status()
    assert code == 503


def test_commission_post_creates_job():
    api, jobs = _api()
    code, body = api.commission("POST", [], {"setupCode": "12345678901", "suggestedName": "X"})
    assert code == 202 and body["jobId"] == "job-1"
    assert jobs.created and jobs.created[0]["setupCode"] == "12345678901"


def test_commission_get_polls_job():
    api, jobs = _api()
    code, body = api.commission("GET", ["job-1"], {})
    assert code == 200 and body["status"] == "success"
    assert jobs.polled == ["job-1"]


def test_commission_get_unknown_job_404():
    api, _ = _api()
    code, body = api.commission("GET", ["ghost"], {})
    assert code == 404


def test_commission_post_without_body_path_requires_post():
    api, _ = _api()
    code, _ = api.commission("GET", [], {})
    assert code == 405


def test_decommission_routes_to_provider():
    seen = {}

    def decom(node_id):
        seen["nid"] = node_id
        return {"nodeId": "0x2A", "removedIndigoDeviceIds": [111], "fabricRemoved": True}

    api, _ = _api(decommission=decom)
    code, body = api.decommission("POST", ["0x2A"])
    assert code == 200
    assert seen["nid"] == 0x2A
    assert body["fabricRemoved"] is True


def test_decommission_reads_nodeid_from_query():
    # IWS doesn't deliver path components on POST → nodeId must work via query.
    seen = {}

    def decom(node_id):
        seen["nid"] = node_id
        return {"nodeId": "0x2A", "removedIndigoDeviceIds": [111], "fabricRemoved": True}

    api, _ = _api(decommission=decom)
    code, body = api.decommission("POST", [], {"nodeId": "0x2A"})
    assert code == 200
    assert seen["nid"] == 0x2A
    assert body["fabricRemoved"] is True


def test_decommission_missing_nodeid_400():
    api, _ = _api()
    code, body = api.decommission("POST", [], {})
    assert code == 400 and body["error"] == "invalid_request"


def test_decommission_requires_post():
    api, _ = _api()
    code, _ = api.decommission("GET", ["0x2A"])
    assert code == 405


def test_decommission_bad_node_id_400():
    api, _ = _api()
    code, _ = api.decommission("POST", ["zzz"])
    assert code == 400


def test_decommission_unknown_node_404():
    api, _ = _api(decommission=lambda nid: None)
    code, _ = api.decommission("POST", ["0x2A"])
    assert code == 404


def test_decommission_matter_unavailable_is_503():
    from http_handlers import MatterUnavailable

    def decom(node_id):
        raise MatterUnavailable("matter-server timed out")

    api, _ = _api(decommission=decom)
    code, body = api.decommission("POST", ["0x2A"])
    assert code == 503 and body["error"] == "matter_server_unreachable"


def test_decommission_unexpected_error_is_500():
    def decom(node_id):
        raise RuntimeError("kaboom")

    api, _ = _api(decommission=decom)
    code, body = api.decommission("POST", ["0x2A"])
    assert code == 500 and body["error"] == "internal_error"


def test_diagnostics_matter_unavailable_is_503():
    from http_handlers import MatterUnavailable

    def diag(node_id):
        raise MatterUnavailable("down")

    api, _ = _api(diagnostics=diag)
    code, body = api.diagnostics(["0x2A"])
    assert code == 503


def test_diagnostics_routes_to_provider():
    api, _ = _api(diagnostics=lambda nid: {"nodeId": "0x2A", "reachable": True})
    code, body = api.diagnostics(["0x2A"])
    assert code == 200 and body["reachable"] is True


def test_diagnostics_missing_node_arg_400():
    api, _ = _api()
    code, _ = api.diagnostics([])
    assert code == 400


def test_diagnostics_reads_nodeid_from_query():
    api, _ = _api(diagnostics=lambda nid: {"nodeId": "0x2A", "reachable": True})
    code, body = api.diagnostics([], {"nodeId": "0x2A"})
    assert code == 200 and body["reachable"] is True
