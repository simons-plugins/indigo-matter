"""E1: every golden bridge-protocol frame is what ``bridge_protocol`` builds/parses.

The Python half of the BRIDGE_PROTOCOL §7 testing contract: the same
`tests/fixtures/bridge_protocol/frames.json` the bridge node's TypeScript suite
asserts against is replayed here through the builders and parsers. A frame that
changes on one side fails the other side's suite, which is the whole point of
sharing the file rather than restating shapes in each language.

References to ``§N`` are BRIDGE_PROTOCOL.md sections.
"""
from __future__ import annotations

import pytest

import bridge_protocol
from bridge_protocol import BridgeProtocol, BridgeProtocolError, EndpointSpec

from conftest import load_bridge_frames

FRAMES = load_bridge_frames()
PENDING = {k: v for k, v in FRAMES["pending"].items() if not k.startswith("_")}


def _exchanges() -> dict:
    """Every request/response pair in the file, live and pending alike."""
    live = {k: v for k, v in FRAMES.items()
            if isinstance(v, dict) and "request" in v and "response" in v}
    return {**live, **{f"pending:{k}": v for k, v in PENDING.items()}}


def _events() -> dict:
    return {k: v for k, v in FRAMES.items() if isinstance(v, dict) and "event" in v}


EXCHANGES = _exchanges()
EVENTS = _events()

# The plugin cannot build a frame that declares a protocol version other than its
# own — that fixture exists to pin the NODE's rejection of a future plugin (§2).
UNBUILDABLE = {"attach_version_mismatch"}


def _build(proto: BridgeProtocol, request: dict):
    """Rebuild a request frame from its own args, through the public builders."""
    command = request["command"]
    args = request.get("args") or {}
    mid = request["message_id"]
    if command == bridge_protocol.CMD_ATTACH:
        return proto.build_attach(
            args["pluginVersion"],
            [EndpointSpec.from_wire(spec) for spec in args["endpoints"]],
            replace_all=args.get("intent") == bridge_protocol.INTENT_REPLACE_ALL,
            message_id=mid,
        )
    if command == bridge_protocol.CMD_UPSERT_ENDPOINT:
        return proto.build_upsert_endpoint(EndpointSpec.from_wire(args["endpoint"]), mid)
    if command == bridge_protocol.CMD_REMOVE_ENDPOINT:
        return proto.build_remove_endpoint(args["indigoDeviceId"], mid)
    if command == bridge_protocol.CMD_SET_STATE:
        return proto.build_set_state(args["indigoDeviceId"], args["states"], mid)
    if command == bridge_protocol.CMD_SET_REACHABLE:
        return proto.build_set_reachable(args["indigoDeviceId"], args["reachable"], mid)
    if command == bridge_protocol.CMD_GET_STATUS:
        return proto.build_get_status(mid)
    if command == bridge_protocol.CMD_GET_PAIRING:
        return proto.build_get_pairing(mid)
    if command == bridge_protocol.CMD_OPEN_WINDOW:
        return proto.build_open_window(args["durationSeconds"], mid)
    if command == bridge_protocol.CMD_REMOVE_FABRIC:
        return proto.build_remove_fabric(args["fabricIndex"], mid)
    if command == bridge_protocol.CMD_FACTORY_RESET:
        return proto.build_factory_reset(args["preserveEndpointNumbers"], mid)
    if command == bridge_protocol.CMD_REBUILD_ENDPOINT_MAP:
        return proto.build_rebuild_endpoint_map(mid)
    raise AssertionError(f"no builder for command {command!r}")


class TestRequests:
    """Every golden request is byte-identical to what the builders produce."""

    @pytest.mark.parametrize("name", sorted(EXCHANGES))
    def test_request_round_trips_through_the_builder(self, name):
        request = EXCHANGES[name]["request"]
        if name in UNBUILDABLE:
            assert request["args"]["protocolVersion"] != bridge_protocol.PROTOCOL_VERSION
            return
        assert _build(BridgeProtocol(), request) == request

    @pytest.mark.parametrize("name", sorted(EXCHANGES))
    def test_request_and_response_share_a_message_id(self, name):
        exchange = EXCHANGES[name]
        assert exchange["request"]["message_id"] == exchange["response"]["message_id"]

    def test_intent_is_absent_unless_replace_all(self):
        # §3.1: the mass-removal opt-in must never appear by default.
        plain = BridgeProtocol().build_attach("2026.8.1", [], message_id="x")
        assert bridge_protocol.ARG_INTENT not in plain["args"]
        deliberate = BridgeProtocol().build_attach("2026.8.1", [], replace_all=True, message_id="x")
        assert deliberate["args"][bridge_protocol.ARG_INTENT] == bridge_protocol.INTENT_REPLACE_ALL
        assert deliberate == PENDING["attach_replace_all"]["request"] | {"message_id": "x"}

    def test_factory_reset_preserves_endpoint_numbers_by_default(self):
        # §3.10: a reset must not scramble identities unless asked to.
        frame = BridgeProtocol().build_factory_reset(message_id="x")
        assert frame["args"][bridge_protocol.ARG_PRESERVE_ENDPOINT_NUMBERS] is True


class TestResponses:
    """Every golden response parses into the normalised dataclasses."""

    @pytest.mark.parametrize("name", sorted(EXCHANGES))
    def test_response_parses_or_raises(self, name):
        proto = BridgeProtocol()
        response = EXCHANGES[name]["response"]
        if bridge_protocol.KEY_ERROR_CODE in response:
            code = response[bridge_protocol.KEY_ERROR_CODE]
            assert code in bridge_protocol.ERROR_CODES, f"{code} is outside the §1.1 domain"
            with pytest.raises(BridgeProtocolError) as excinfo:
                proto.parse_result(response)
            assert excinfo.value.code == code
            assert excinfo.value.details == response["details"]
            return
        assert proto.parse_result(response) == response["result"]

    def test_status_report_is_normalised(self):
        report = bridge_protocol.parse_status(
            PENDING["attach_with_endpoints"]["response"]["result"])
        assert report.commissioned is True
        assert report.endpoint_count == 2
        assert [ep.indigo_device_id for ep in report.endpoints] == [123456789, 123456790]
        assert [ep.endpoint_number for ep in report.endpoints] == [2, 3]
        assert report.endpoints[1].role == "doorLock"
        assert report.fabrics[0].fabric_index == 1
        assert report.fabrics[0].label == "Apple Home"
        assert report.fabrics[0].vendor_id == 4937
        assert report.drift == []

    def test_pairing_states(self):
        never = bridge_protocol.parse_pairing(
            FRAMES["get_pairing_uncommissioned"]["response"]["result"])
        assert (never.commissioned, never.window_open) == (False, True)
        assert never.manual_pairing_code and never.qr_pairing_code
        assert never.window_expires_at is None

        # §3.7: once commissioned with no window, the codes are gone and stay gone.
        closed = bridge_protocol.parse_pairing(
            FRAMES["get_pairing_commissioned"]["response"]["result"])
        assert (closed.commissioned, closed.window_open) == (True, False)
        assert closed.manual_pairing_code is None
        assert closed.qr_pairing_code is None

        reopened = bridge_protocol.parse_pairing(
            FRAMES["get_pairing_commissioned_window_open"]["response"]["result"])
        assert (reopened.commissioned, reopened.window_open) == (True, True)
        assert reopened.window_expires_at is not None
        assert reopened.manual_pairing_code

    def test_commissioning_window(self):
        window = bridge_protocol.parse_window(
            FRAMES["open_commissioning_window"]["response"]["result"])
        assert window.manual_pairing_code == "34970112332"
        assert window.qr_pairing_code.startswith("MT:")
        assert window.window_expires_at.endswith("Z")

    def test_removal_results(self):
        assert PENDING["remove_endpoint"]["response"]["result"]["removed"] is True
        # §3.3: removing an absent endpoint SUCCEEDS — idempotent, not an error.
        absent = PENDING["remove_endpoint_absent"]["response"]
        assert bridge_protocol.KEY_ERROR_CODE not in absent
        assert absent["result"]["removed"] is False


class TestHandshake:
    def test_hello_parses(self):
        hello = bridge_protocol.parse_hello(FRAMES["handshake"])
        assert hello.protocol_version == bridge_protocol.PROTOCOL_VERSION
        assert hello.bridge_version and hello.matter_js_version

    @pytest.mark.parametrize("frame", [
        {}, None, "hello", {"bridgeVersion": "1"}, {"protocolVersion": "1"}, {"protocolVersion": True},
    ])
    def test_non_hello_first_frame_is_refused(self, frame):
        with pytest.raises(BridgeProtocolError):
            bridge_protocol.parse_hello(frame)


class TestEvents:
    @pytest.mark.parametrize("name", sorted(EVENTS))
    def test_event_name_is_in_the_v1_domain(self, name):
        frame = EVENTS[name]
        assert frame["event"] in bridge_protocol.EVENT_NAMES
        assert bridge_protocol.KEY_MESSAGE_ID not in frame  # §1: events carry no id

    def test_command_events_normalise(self):
        for key, expected in [
            ("command_on_off", ("onOff", {"value": True})),
            ("command_set_level", ("setLevel", {"level": 60})),
            ("command_lock", ("lock", {})),
        ]:
            command = bridge_protocol.parse_command(EVENTS[key]["data"])
            assert (command.command, command.args) == expected
            assert command.indigo_device_id == EVENTS[key]["data"]["indigoDeviceId"]

    def test_fabrics_changed_normalises(self):
        data = EVENTS["fabrics_changed_added"]["data"]
        fabrics = bridge_protocol.parse_fabrics(data["fabrics"])
        assert data["change"] == "added"
        assert fabrics[0].label == "Apple Home"

    def test_drift_normalises(self):
        drift = bridge_protocol.parse_drift(EVENTS["drift_detected"]["data"]["drift"])
        assert (drift[0].unique_id, drift[0].expected, drift[0].actual) == ("indigo-123456789", 2, 5)

    def test_window_closed_reasons(self):
        reasons = {EVENTS[key]["data"]["reason"]
                   for key in ("window_closed_expired", "window_closed_commissioned")}
        assert reasons == {"expired", "commissioned"}


class TestCoverage:
    """The fixture file is the contract; it has to cover all of it."""

    def test_every_command_has_a_golden_frame(self):
        covered = {exchange["request"]["command"] for exchange in EXCHANGES.values()}
        expected = {
            getattr(bridge_protocol, name) for name in dir(bridge_protocol)
            if name.startswith("CMD_")
        }
        assert expected - covered == set()

    def test_every_event_has_a_golden_frame(self):
        covered = {frame["event"] for frame in EVENTS.values()}
        assert bridge_protocol.EVENT_NAMES - covered == set()

    def test_every_endpoint_spec_uses_a_v1_role(self):
        roles = set()
        for exchange in EXCHANGES.values():
            args = exchange["request"].get("args") or {}
            for spec in args.get("endpoints", []):
                roles.add(spec["role"])
            if "endpoint" in args:
                roles.add(args["endpoint"]["role"])
        assert roles and roles <= bridge_protocol.ROLES

    def test_endpoint_spec_round_trips(self):
        wire = PENDING["upsert_endpoint"]["request"]["args"]["endpoint"]
        assert EndpointSpec.from_wire(wire).to_wire() == wire
