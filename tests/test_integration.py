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
        assert env.devices[dev_id].name == "Office Plug"

        # /status now reports the node
        scode, sbody = http.status()
        assert scode == 200 and sbody["nodeCount"] == 1

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
