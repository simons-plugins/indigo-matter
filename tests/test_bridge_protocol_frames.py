"""E1: every golden bridge-protocol frame is what ``bridge_protocol`` builds/parses.

The Python half of the BRIDGE_PROTOCOL §7 testing contract: the same
`tests/fixtures/bridge_protocol/frames.json` the bridge node's TypeScript suite
asserts against is replayed here through the builders and parsers. A frame that
changes on one side fails the other side's suite, which is the whole point of
sharing the file rather than restating shapes in each language.

References to ``§N`` are BRIDGE_PROTOCOL.md sections.
"""
from __future__ import annotations

import json

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
#: The same exchanges keyed by bare name, for tests that name one frame. A frame
#: moves out of "pending" when the NODE implements it (E3 moved the endpoint-CRUD
#: family); the plugin-side shapes asserted here do not change when it does.
BY_NAME = {name.removeprefix("pending:"): exchange for name, exchange in EXCHANGES.items()}
EVENTS = _events()

#: Fixtures with no public builder, and the assertion that replaces the
#: round-trip. Both are frames the plugin is INCAPABLE of producing — which is
#: the point of them: they pin what the node does with a client that is not this
#: one. Anything else missing a builder is a hole, so the default is still to
#: fail with "no builder for command".
UNBUILDABLE = {
    # §2: the plugin only ever declares its own protocol version.
    "attach_version_mismatch": lambda request:
        request["args"]["protocolVersion"] != bridge_protocol.PROTOCOL_VERSION,
    # §1: a name outside the §3 domain — there is no builder because there is no
    # command. (It used to be `upsert_endpoint`, "unknown" only until the node
    # grew the handler, which made the fixture expire on its own.)
    "unknown_command": lambda request: request["command"] not in bridge_protocol.COMMANDS,
}

#: Endpoint specs that deliberately carry a role outside the §4.2 enum, to pin
#: the node's ``unknown_role`` refusal. Everything else must be a v1 role.
#: (No ``pending:`` prefix any more — the node's ``upsert_endpoint`` handler
#: rejects this frame today, so it graduated to a live exchange.)
UNLAWFUL_ROLE_FIXTURES = {"upsert_endpoint_unknown_role"}


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
        unbuildable = UNBUILDABLE.get(name)
        if unbuildable is not None:
            assert unbuildable(request), f"{name} is buildable after all — drop the exemption"
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
        assert deliberate == BY_NAME["attach_replace_all"]["request"] | {"message_id": "x"}

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
            BY_NAME["attach_with_endpoints"]["response"]["result"])
        assert report.commissioned is True
        assert report.endpoint_count == 2
        assert [ep.indigo_device_id for ep in report.endpoints] == [123456789, 123456790]
        assert [ep.endpoint_number for ep in report.endpoints] == [2, 3]
        assert report.endpoints[1].role == "dimmableLight"
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
        assert BY_NAME["remove_endpoint"]["response"]["result"]["removed"] is True
        # §3.3: removing an absent endpoint SUCCEEDS — idempotent, not an error.
        absent = BY_NAME["remove_endpoint_absent"]["response"]
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


def _endpoint_specs(name: str, exchange: dict):
    """Every ``EndpointSpec`` in a request, whichever arg carries it (§3.1/§3.2)."""
    args = exchange["request"].get("args") or {}
    yield from args.get("endpoints", [])
    if "endpoint" in args:
        yield args["endpoint"]


def _covered_roles() -> set:
    return {spec["role"]
            for name, exchange in EXCHANGES.items() if name not in UNLAWFUL_ROLE_FIXTURES
            for spec in _endpoint_specs(name, exchange)}


def _covered_state_keys() -> set:
    """Keys carried by a real ``set_state`` frame — §3.4 is the only push path."""
    keys = set()
    for exchange in EXCHANGES.values():
        request = exchange["request"]
        if request["command"] != bridge_protocol.CMD_SET_STATE:
            continue
        keys |= set((request.get("args") or {}).get("states") or {})
    return keys


def _covered_command_names() -> set:
    return {frame["data"]["command"] for frame in EVENTS.values()
            if frame["event"] == bridge_protocol.EVT_COMMAND}


def _covered_error_codes() -> set:
    return {exchange["response"][bridge_protocol.KEY_ERROR_CODE]
            for exchange in EXCHANGES.values()
            if bridge_protocol.KEY_ERROR_CODE in exchange["response"]}


class TestCoverage:
    """The fixture file is the contract; it has to cover all of it.

    Every expectation below is ENUMERATED from ``bridge_protocol`` — the §3
    command set, the §1.1 error domain, the §4.2 role vocabularies — never from
    a list restated here. Adding a role, a command name, a state key or an error
    code therefore fails this suite until a golden frame carries it, and
    deleting the frame that carries one fails it again.
    """

    def test_the_pending_section_is_empty(self):
        """E5 graduated the last five frames; both suites must say so.

        The TS side has asserted this since E5 and the Python side did not,
        which meant a frame quietly pushed back into ``pending`` would have
        failed exactly one of the two suites that share this file — and §7's
        whole point is that a one-sided change fails the side that made it.
        """
        assert PENDING == {}

    def test_every_status_report_carries_the_warnings_field(self):
        """§4.3 ``warnings``. Empty is normal; absent is a client parsing a
        report from a node that could not tell it about a failed write."""
        for name, exchange in EXCHANGES.items():
            result = exchange["response"].get("result")
            if not isinstance(result, dict) or "endpointCount" not in result:
                continue
            assert "warnings" in result, f"{name} is a StatusReport without warnings"
            assert isinstance(result["warnings"], list)

    def test_every_command_has_a_golden_frame(self):
        covered = {exchange["request"]["command"] for exchange in EXCHANGES.values()}
        assert bridge_protocol.COMMANDS - covered == set()

    def test_every_event_has_a_golden_frame(self):
        covered = {frame["event"] for frame in EVENTS.values()}
        assert bridge_protocol.EVENT_NAMES - covered == set()

    def test_every_error_code_has_a_golden_frame(self):
        # §1.1 is "the complete domain for protocol version 1"; a code the client
        # has never once been handed is a code nobody has thought about handling.
        assert bridge_protocol.ERROR_CODES - _covered_error_codes() == set()

    def test_every_role_appears_in_an_endpoint_spec(self):
        assert bridge_protocol.ROLES - _covered_roles() == set()

    def test_every_state_key_appears_in_a_set_state_frame(self):
        # SHARED_STATE_KEYS (issue #220's `batteryLevel`) is unioned in
        # deliberately: it is role-independent by design, so it is not in any
        # ROLE_STATE_KEYS tuple, but the fixture file is still required to
        # carry it in a real `set_state` frame — `set_state_contact_sensor`
        # does, one of the frames added for issue #220.
        expected = ({key for keys in bridge_protocol.ROLE_STATE_KEYS.values() for key in keys}
                    | set(bridge_protocol.SHARED_STATE_KEYS))
        assert expected - _covered_state_keys() == set()

    def test_every_role_command_appears_in_a_command_event(self):
        expected = {name for names in bridge_protocol.ROLE_COMMANDS.values() for name in names}
        assert expected - _covered_command_names() == set()

    def test_sensor_roles_have_no_commands(self):
        # §4.2: sensors are read-only in Matter. An empty tuple there is a
        # decision, and this is what stops it being read as an oversight and
        # "fixed" by inventing a command the node would never emit.
        for role in ("occupancySensor", "contactSensor", "temperatureSensor",
                     "humiditySensor", "lightSensor", "pressureSensor", "flowSensor"):
            assert bridge_protocol.ROLE_COMMANDS[role] == ()

    def test_the_two_role_vocabularies_agree_on_the_role_set(self):
        assert set(bridge_protocol.ROLE_COMMANDS) == bridge_protocol.ROLES

    def test_every_endpoint_spec_uses_a_v1_role(self):
        for name, exchange in EXCHANGES.items():
            for spec in _endpoint_specs(name, exchange):
                lawful = spec["role"] in bridge_protocol.ROLES
                assert lawful is (name not in UNLAWFUL_ROLE_FIXTURES), \
                    f"{name} carries role {spec['role']!r}"

    def test_the_rebuild_fixture_depicts_a_post_storage_loss_node(self):
        # The fixture's numbers differ from attach's because it depicts a node
        # whose lost Matter storage reallocated them — §3.11 itself records
        # what exists and reallocates nothing. A rebuild after mere map-file
        # damage would echo attach's numbers, so the fixture has to show the
        # case where the witness actually changes.
        before = {ep["indigoDeviceId"]: ep["endpointNumber"]
                  for ep in BY_NAME["attach_with_endpoints"]["response"]["result"]["endpoints"]}
        after = {ep["indigoDeviceId"]: ep["endpointNumber"]
                 for ep in BY_NAME["rebuild_endpoint_map"]["response"]["result"]["endpoints"]}
        assert set(before) == set(after)
        assert all(after[dev] != before[dev] for dev in before), (before, after)

    def test_endpoint_spec_round_trips(self):
        wire = BY_NAME["upsert_endpoint"]["request"]["args"]["endpoint"]
        assert EndpointSpec.from_wire(wire).to_wire() == wire

    def test_every_role_spec_round_trips(self):
        for spec in BY_NAME["attach_all_roles"]["request"]["args"]["endpoints"]:
            assert EndpointSpec.from_wire(spec).to_wire() == spec

    def test_endpoint_spec_round_trips_with_battery(self):
        # The contactSensor entry in `attach_all_roles` (added for issue #220)
        # is the one spec in the whole file that carries `"battery": true` —
        # pinned here by name so a future edit that moves the battery flag to
        # a different spec still exercises this round trip.
        specs = {s["indigoDeviceId"]: s for s in BY_NAME["attach_all_roles"]["request"]["args"]["endpoints"]}
        contact_sensor = specs[900009]
        assert contact_sensor["battery"] is True
        assert EndpointSpec.from_wire(contact_sensor).to_wire() == contact_sensor

    def test_endpoint_spec_round_trips_without_battery(self):
        # §4.1: absent on the wire, not `false` — `to_wire()` must reproduce
        # that (its own guard is exercised by every OTHER spec round trip too,
        # but this one names the rule).
        wire = BY_NAME["upsert_endpoint"]["request"]["args"]["endpoint"]
        assert "battery" not in wire
        spec = EndpointSpec.from_wire(wire)
        assert spec.battery is False
        assert "battery" not in spec.to_wire()

    def test_no_golden_frame_spells_battery_false(self):
        """§4.1/rail: `to_wire()` omits `battery` unless it is `True` — a
        literal `"battery": false` anywhere in this file would mean a fixture
        was hand-written rather than produced by the code it is meant to pin.
        """
        raw = json.dumps(FRAMES)
        assert '"battery": false' not in raw
        assert '"battery":false' not in raw
