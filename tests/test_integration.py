"""M4 end-to-end: a full commission flow and an action round-trip.

Wires the real MatterClient + CommissionJobs + DeviceSync + HttpApi together,
backed by the in-process FakeWebSocket and a fake Indigo. This is the closest
the suite gets to the live M4 acceptance (Tapo plug) without hardware.
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

import protocol
from protocol import Protocol
from matter_client import MatterClient
from commission_jobs import CommissionJobs
from http_handlers import HttpApi

from fakes import FakeWebSocket, returns, scripted_responder
from test_handlers import RELAY_NODE
from test_device_sync import FakeDev, FakeDeviceFactory, FakeDevices


@pytest.fixture
def env(mock_indigo_base, mock_logger):
    indigo = mock_indigo_base
    devices = FakeDevices()
    indigo.devices = devices
    indigo.device = FakeDeviceFactory(devices)
    indigo.kProtocol = SimpleNamespace(Plugin="plugin")
    import device_sync
    importlib.reload(device_sync)
    from matter_handlers.registry import HandlerRegistry
    ds = device_sync.DeviceSync(HandlerRegistry(), mock_logger)
    return SimpleNamespace(indigo=indigo, devices=devices, ds=ds, logger=mock_logger)


def test_full_commission_flow_creates_indigo_device(env):
    async def scenario():
        fake = FakeWebSocket(responder=scripted_responder({
            protocol.CMD_COMMISSION: {"node_id": 42},
            protocol.CMD_GET_NODE: lambda f: RELAY_NODE,
        }))
        matter = MatterClient(Protocol(), env.logger, {}, connect=lambda uri: returns(fake))
        task = asyncio.create_task(matter.run())
        await matter.wait_connected(timeout=2)

        jobs = CommissionJobs(matter, env.ds.create_from_raw, env.logger,
                              schedule=asyncio.ensure_future)
        http = HttpApi(
            jobs, env.logger,
            status_provider=lambda: {"ready": matter.connected, "nodeCount": env.ds.node_count()},
            decommission_provider=lambda nid: None,
            diagnostics_provider=lambda nid: None,
        )

        code, body = http.commission("POST", [], {
            "setupCode": "12345678901", "suggestedName": "Office Plug", "suggestedRoom": "Office",
        })
        assert code == 202
        job_id = body["jobId"]

        final = None
        for _ in range(200):
            _, status = http.commission("GET", [job_id], {})
            if status.get("status") in ("success", "failed"):
                final = status
                break
            await asyncio.sleep(0.005)

        assert final is not None and final["status"] == "success", final
        assert final["result"]["nodeId"] == "0x2A"
        dev_id = final["result"]["primaryDeviceId"]
        assert env.devices[dev_id].deviceTypeId == "matterRelay"
        # The endpoint device carries its role suffix since issue #204 stage 2;
        # the bare "Office Plug" is the node device's name (ADR-0008 option B).
        assert env.devices[dev_id].name == "Office Plug - Switch"

        # /status now reports the node
        scode, sbody = http.status()
        assert scode == 200 and sbody["nodeCount"] == 1

        await matter.close()
        task.cancel()
    asyncio.run(scenario())


def test_late_commission_failure_surfaces_as_commissioning_failed(env, monkeypatch):
    # #23 end-to-end: the real MatterClient + CommissionJobs, wired exactly as
    # plugin.py wires them, over a matter-server that never answers the
    # commission RPC in time — then answers it late, with an error. A client
    # still polling must see the corrected, definitive failure.
    async def scenario():
        mid_holder: dict = {}

        def responder(frame):
            command = frame.get(protocol.KEY_COMMAND)
            if command in (protocol.CMD_COMMISSION, protocol.CMD_START_LISTENING):
                if command == protocol.CMD_COMMISSION:
                    mid_holder["mid"] = frame[protocol.KEY_MESSAGE_ID]
                return []  # withheld — matter-server never answers in time
            return [{protocol.KEY_MESSAGE_ID: frame[protocol.KEY_MESSAGE_ID], protocol.KEY_RESULT: None}]

        fake = FakeWebSocket(responder=responder)
        matter = MatterClient(Protocol(), env.logger, {}, connect=lambda uri: returns(fake))
        task = asyncio.create_task(matter.run())
        await matter.wait_connected(timeout=2)

        jobs = CommissionJobs(matter, env.ds.create_from_raw, env.logger,
                              schedule=asyncio.ensure_future)
        matter._on_late_response = jobs.note_late_response  # plugin.py's wiring, minus Plugin
        http = HttpApi(
            jobs, env.logger,
            status_provider=lambda: {"ready": matter.connected, "nodeCount": env.ds.node_count()},
            decommission_provider=lambda nid: None,
            diagnostics_provider=lambda nid: None,
        )

        # Patch the 300s commission deadline down to something a test can wait
        # out — commission_with_code's own default, since _run_job never passes
        # an explicit timeout.
        monkeypatch.setattr(MatterClient.commission_with_code, "__defaults__", (0.05, None))

        code, body = http.commission("POST", [], {
            "setupCode": "12345678901", "suggestedName": "Office Plug",
        })
        assert code == 202
        job_id = body["jobId"]

        timed_out = None
        for _ in range(200):
            _, status = http.commission("GET", [job_id], {})
            if status.get("status") == "failed":
                timed_out = status
                break
            await asyncio.sleep(0.005)
        assert timed_out is not None and timed_out["error"]["code"] == "commissioning_timeout", timed_out

        # matter-server's late answer arrives: the commission actually failed.
        await fake.push_frame({"message_id": mid_holder["mid"],
                               "error_code": 50, "details": "PASE failed"})

        final = None
        for _ in range(200):
            _, status = http.commission("GET", [job_id], {})
            if status.get("error", {}).get("code") == "commissioning_failed":
                final = status
                break
            await asyncio.sleep(0.005)
        assert final is not None, "GET never reported the late, definitive failure"
        assert final["status"] == "failed"
        assert "PASE failed" in final["error"]["message"]
        assert final["error"]["matterErrorCode"] == 50

        await matter.close()
        task.cancel()
    asyncio.run(scenario())


def test_action_command_round_trip(env):
    async def scenario():
        seen = {}

        def responder(frame):
            if frame.get(protocol.KEY_COMMAND) == protocol.CMD_DEVICE:
                seen["command"] = frame["args"][protocol.ARG_COMMAND]
                seen["node"] = frame["args"][protocol.ARG_NODE_ID]
            return [{protocol.KEY_MESSAGE_ID: frame["message_id"], protocol.KEY_RESULT: None}]

        fake = FakeWebSocket(responder=responder)
        matter = MatterClient(Protocol(), env.logger, {}, connect=lambda uri: returns(fake))
        task = asyncio.create_task(matter.run())
        await matter.wait_connected(timeout=2)

        dev = FakeDev(1, "Plug", "matterRelay", {"nodeId": "42", "endpointId": "1"})

        class Action:
            deviceAction = env.indigo.kDeviceAction.TurnOn

        command = env.ds.build_command(dev, Action())
        assert command is not None
        await matter.send_command(command)

        assert seen["command"] == "On"
        assert seen["node"] == 42

        await matter.close()
        task.cancel()
    asyncio.run(scenario())
