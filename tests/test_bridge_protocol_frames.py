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
import ct_bounds
from bridge_protocol import (
    BridgeProtocol,
    BridgeProtocolError,
    EndpointSpec,
    PublishedId,
    next_generation,
    parse_published_id,
    published_id_for,
)

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
    if command == bridge_protocol.CMD_LIST_ORPHANS:
        return proto.build_list_orphans(mid)
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

    def test_subscription_churn_healthy(self):
        """§4.3 issue #286 — ``checked: true, active: false`` parses to an
        empty, non-active verdict."""
        report = bridge_protocol.parse_status(FRAMES["get_status"]["response"]["result"])
        assert report.subscription_churn.checked is True
        assert report.subscription_churn.active is False
        assert report.subscription_churn.peers == []

    def test_subscription_churn_active_golden_frame(self):
        """The `get_status_churning` golden frame (§7) — a peer over threshold."""
        report = bridge_protocol.parse_status(
            FRAMES["get_status_churning"]["response"]["result"])
        churn = report.subscription_churn
        assert churn.checked is True
        assert churn.active is True
        assert len(churn.peers) == 1
        peer = churn.peers[0]
        assert peer.peer_node_id == "41869fbd537ef01"
        assert peer.fabric_index == 2
        assert peer.live_sessions == 5
        assert peer.invalid_deletions == 3
        assert peer.window_minutes == 30
        assert peer.since == "2026-08-23T09:12:00.000Z"

    def test_subscription_churn_absent_defaults_to_unchecked(self):
        """A pre-0.15.0 node's ``StatusReport`` has no ``subscriptionChurn`` key
        at all. Absence must default to ``checked=False`` — "never looked",
        never "healthy" — the same tolerant-default idiom ``driftChecked``
        and ``warnings`` already use for an old node."""
        result = dict(FRAMES["get_status"]["response"]["result"])
        del result["subscriptionChurn"]
        report = bridge_protocol.parse_status(result)
        assert report.subscription_churn.checked is False
        assert report.subscription_churn.active is False
        assert report.subscription_churn.peers == []

    def test_subscription_churn_malformed_peer_degrades_the_list_not_the_report(self):
        """review finding 4: one bad peer entry must not detonate the whole
        ``StatusReport`` — the old comprehension's ``.get``/``int()`` escaping
        failed the entire status poll at DEBUG, silenced `warnings` (the churn
        warning INCLUDED) for the whole report, and could fail an `attach`."""
        result = dict(FRAMES["get_status_churning"]["response"]["result"])
        good_peer = result["subscriptionChurn"]["peers"][0]
        result = {
            **result,
            "subscriptionChurn": {
                "checked": True, "active": True,
                "peers": [
                    "not even a dict",
                    {"peerNodeId": "ok-peer", "fabricIndex": "not-a-number",
                     "liveSessions": 5, "invalidDeletions": 3, "windowMinutes": 30,
                     "since": "2026-08-23T09:12:00.000Z"},
                    good_peer,
                ],
            },
        }
        report = bridge_protocol.parse_status(result)  # must not raise
        churn = report.subscription_churn
        assert churn.checked is True
        assert churn.active is True
        # Both malformed entries dropped; the one good peer survives.
        assert len(churn.peers) == 1
        assert churn.peers[0].peer_node_id == good_peer["peerNodeId"]

    def test_session_hygiene_healthy(self):
        """§4.3 issue #283 "Finding 2" — a bridge whose hygiene sweep is wired
        and has closed nothing parses to a checked, empty, all-zero verdict."""
        report = bridge_protocol.parse_status(FRAMES["get_status"]["response"]["result"])
        hygiene = report.session_hygiene
        assert hygiene.checked is True
        assert hygiene.peers == []
        assert hygiene.closed.superseded == 0
        assert hygiene.closed.dead == 0
        assert hygiene.closed.rotated == 0

    def test_session_hygiene_golden_frame(self):
        """The `get_status_churning` golden frame (§7) also carries a
        `sessionHygiene` peer count for the same piled peer — issue #283's own
        "diagnostic to run first" recipe."""
        report = bridge_protocol.parse_status(
            FRAMES["get_status_churning"]["response"]["result"])
        hygiene = report.session_hygiene
        assert hygiene.checked is True
        assert len(hygiene.peers) == 1
        peer = hygiene.peers[0]
        assert peer.peer_node_id == "41869fbd537ef01"
        assert peer.fabric_index == 2
        assert peer.live_sessions == 5

    def test_session_hygiene_absent_defaults_to_unchecked(self):
        """A pre-0.17.0 node's ``StatusReport`` has no ``sessionHygiene`` key
        at all. Absence must default to ``checked=False`` — "never looked and
        never acted", never "healthy" — the same tolerant-default idiom
        ``subscription_churn`` already uses for an old node."""
        result = dict(FRAMES["get_status"]["response"]["result"])
        del result["sessionHygiene"]
        report = bridge_protocol.parse_status(result)
        assert report.session_hygiene.checked is False
        assert report.session_hygiene.peers == []
        assert report.session_hygiene.closed.superseded == 0
        assert report.session_hygiene.closed.dead == 0
        assert report.session_hygiene.closed.rotated == 0

    def test_session_hygiene_malformed_peer_degrades_the_list_not_the_report(self):
        """One bad peer entry must not detonate the whole ``StatusReport`` —
        same degradation rule ``_parse_churn_peer`` established (issue #286
        review finding 4)."""
        result = dict(FRAMES["get_status_churning"]["response"]["result"])
        good_peer = result["sessionHygiene"]["peers"][0]
        result = {
            **result,
            "sessionHygiene": {
                "checked": True,
                "peers": [
                    "not even a dict",
                    {"peerNodeId": "ok-peer", "fabricIndex": "not-a-number", "liveSessions": 2},
                    good_peer,
                ],
                "closed": {"superseded": 1, "dead": 0, "rotated": 0},
            },
        }
        report = bridge_protocol.parse_status(result)  # must not raise
        hygiene = report.session_hygiene
        assert hygiene.checked is True
        assert len(hygiene.peers) == 1
        assert hygiene.peers[0].peer_node_id == good_peer["peerNodeId"]
        # The degraded peer list must not thin `closed` — they are unrelated.
        assert hygiene.closed.superseded == 1

    def test_session_hygiene_malformed_closed_block_degrades_to_zero(self):
        """A malformed ``closed`` object must not fail the whole report —
        the counts are informational, and a status poll must never fail
        over them."""
        result = dict(FRAMES["get_status"]["response"]["result"])
        result = {**result, "sessionHygiene": {"checked": True, "peers": [], "closed": "not a dict"}}
        report = bridge_protocol.parse_status(result)  # must not raise
        assert report.session_hygiene.checked is True
        assert report.session_hygiene.closed.superseded == 0
        assert report.session_hygiene.closed.dead == 0
        assert report.session_hygiene.closed.rotated == 0

    def test_subscription_churn_all_peers_malformed_is_active_with_no_peers(self):
        """A degraded-to-empty peers list must not silently flip `active` to
        False — the node still said it was active; only the DETAIL is thin."""
        result = dict(FRAMES["get_status_churning"]["response"]["result"])
        result = {**result, "subscriptionChurn": {
            "checked": True, "active": True, "peers": [None, "garbage", 42],
        }}
        report = bridge_protocol.parse_status(result)  # must not raise
        assert report.subscription_churn.active is True
        assert report.subscription_churn.peers == []

    def test_malformed_fabric_degrades_the_list_not_the_report(self):
        """review finding D: one bad fabric entry (missing `fabricIndex`, a
        JSON null, a non-dict) must not detonate the whole `StatusReport` —
        which, since issue #288, is load-bearing for the per-fabric slot
        plan, not merely descriptive. The old comprehension's bare `item[...]`
        escaping failed the entire status poll, and on the attach path the
        attach itself."""
        result = dict(FRAMES["get_status_churning"]["response"]["result"])
        good_fabric = result["fabrics"][0]
        result = {
            **result,
            "fabrics": [
                None,
                "not even a dict",
                {"label": "no fabricIndex at all", "vendorId": 4937},
                good_fabric,
            ],
        }
        report = bridge_protocol.parse_status(result)  # must not raise
        assert len(report.fabrics) == 1
        assert report.fabrics[0].fabric_index == good_fabric["fabricIndex"]
        assert report.fabrics[0].label == good_fabric["label"]

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

    def test_orphan_records_are_normalised(self):
        orphans = bridge_protocol.parse_orphans(FRAMES["list_orphans"]["response"]["result"])
        assert len(orphans) == 3
        full, partial, bare = orphans
        assert (full.unique_id, full.number) == ("indigo-223456791", 7)
        assert full.role == "dimmableLight"
        assert full.label == "Kitchen Lamp"
        assert full.orphaned_at == "2026-08-12T09:15:00Z"
        assert full.device_id == 223456791
        assert partial.orphaned_at is None and partial.device_id is None
        # PR5 design E4 — the pre-2026.16.2 bare orphan: nothing beyond number/uniqueId.
        assert bare.role is None and bare.label is None and bare.device_id is None

    def test_empty_orphan_list_normalises_to_empty(self):
        assert bridge_protocol.parse_orphans(
            FRAMES["list_orphans_empty"]["response"]["result"]) == []

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

    def test_every_status_report_carries_the_subscription_churn_field(self):
        """§4.3 issue #286 ``subscriptionChurn``. Every golden StatusReport is
        from a 0.15.0+ node, so it must carry the field — absence is the
        pre-0.15.0 case ``parse_subscription_churn`` tolerates, not a shape any
        current fixture should model."""
        for name, exchange in EXCHANGES.items():
            result = exchange["response"].get("result")
            if not isinstance(result, dict) or "endpointCount" not in result:
                continue
            assert "subscriptionChurn" in result, f"{name} is a StatusReport without subscriptionChurn"
            churn = result["subscriptionChurn"]
            assert isinstance(churn, dict) and {"checked", "active", "peers"} <= churn.keys()

    def test_every_status_report_carries_the_session_hygiene_field(self):
        """§4.3 issue #283 "Finding 2" ``sessionHygiene``. Every golden
        StatusReport is from a 0.17.0+ node, so it must carry the field —
        absence is the pre-0.17.0 case ``parse_session_hygiene`` tolerates,
        not a shape any current fixture should model."""
        for name, exchange in EXCHANGES.items():
            result = exchange["response"].get("result")
            if not isinstance(result, dict) or "endpointCount" not in result:
                continue
            assert "sessionHygiene" in result, f"{name} is a StatusReport without sessionHygiene"
            hygiene = result["sessionHygiene"]
            assert isinstance(hygiene, dict) and {"checked", "peers", "closed"} <= hygiene.keys()
            assert {"superseded", "dead", "rotated"} <= hygiene["closed"].keys()

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
        for role in ("occupancySensor", "contactSensor", "waterLeakDetector",
                     "waterFreezeDetector", "rainSensor", "smokeAlarm", "coAlarm",
                     "temperatureSensor",
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

    def test_endpoint_spec_omits_published_as_at_the_default(self):
        """Issues #219/#240 — the wire is byte-identical to today's when a
        spec has never moved off the default derivation, so a fixture that
        predates `publishedAs` still round-trips unchanged."""
        wire = BY_NAME["upsert_endpoint"]["request"]["args"]["endpoint"]
        assert "publishedAs" not in wire
        spec = EndpointSpec.from_wire(wire)
        assert spec.indigo_device_id == 123456789
        assert spec.published_as == ""
        assert "publishedAs" not in spec.to_wire()

    def test_endpoint_spec_omits_published_as_when_it_equals_the_default(self):
        spec = EndpointSpec(indigo_device_id=123456789, role="onOffLight", label="x",
                            published_as="indigo-123456789")
        assert "publishedAs" not in spec.to_wire()

    def test_endpoint_spec_round_trips_a_non_default_published_as(self):
        """A role-changed or re-adopted export sends its identity on the wire,
        and it survives a round trip untouched."""
        wire = dict(BY_NAME["upsert_endpoint"]["request"]["args"]["endpoint"])
        wire["publishedAs"] = "indigo-123456789~2"
        spec = EndpointSpec.from_wire(wire)
        assert spec.published_as == "indigo-123456789~2"
        assert spec.to_wire() == wire

    def test_a_golden_frame_carries_a_non_default_published_as(self):
        """Issues #219/#240 — the hand-built round trip above proves the
        plugin agrees with ITSELF. Only a GOLDEN frame makes both languages
        agree, and every frame in this file was default-identity until
        `upsert_endpoint_published_as` was added: the one field a re-adopt or
        a role change puts on the wire had no cross-language fixture at all.
        The node asserts the same exchange in
        `bridge-node/test/protocol.test.ts`.
        """
        wire = BY_NAME["upsert_endpoint_published_as"]["request"]["args"]["endpoint"]
        assert wire["publishedAs"] == "indigo-123456789~2"
        assert parse_published_id(wire["publishedAs"]) == PublishedId(
            device_id=wire["indigoDeviceId"], generation=2)
        assert EndpointSpec.from_wire(wire).to_wire() == wire


def test_ct_bounds_wire_options_matches_the_golden_colorTemperatureLight_spec():
    """Issue #294 review — cross-checks the Python transform
    (`ct_bounds.wire_options`) directly against the SHARED golden fixture,
    not just against itself: a divergence between what the plugin actually
    computes for a seeded CT range and what `attach_all_roles`'s
    `colorTemperatureLight` spec (indigoDeviceId 900004) carries on the wire
    must fail the PYTHON suite too, not only `bridge-node`'s.

    The seed below (``ctMinMireds: 200, ctMaxMireds: 450``) is the exact
    stored-options shape the golden spec's own ``options`` already shows —
    both a seed pair, no learned keys — so this pins that ``wire_options``
    reproduces it byte-for-byte, not merely that the fixture round-trips
    through ``EndpointSpec`` (``test_every_role_spec_round_trips`` already
    covers that separate claim).
    """
    seed = {ct_bounds.OPTION_CT_MIN_MIREDS: 200, ct_bounds.OPTION_CT_MAX_MIREDS: 450}
    specs = BY_NAME["attach_all_roles"]["request"]["args"]["endpoints"]
    golden = next(s for s in specs if s["indigoDeviceId"] == 900004)
    assert golden["role"] == "colorTemperatureLight"
    assert ct_bounds.wire_options(seed) == golden["options"]


class TestPublishedIdentity:
    """Issues #219/#240 — the Python twin of `bridge-node/src/protocol.ts`'s
    `publishedIdFor`/`parsePublishedId`. There is no existing cross-language
    fixture mechanism for a pure function (§7's golden frames only cover wire
    shapes), so this is a hand-built parallel table: every case here was
    checked by hand against the TypeScript regex/derivation
    (`^indigo-(-?\\d+)(?:~(\\d+))?$`, safe-integer device id, generation >= 2,
    total length <= 32) at the time both were written, and the two must be
    kept in sync by inspection if either changes.

    Two of the divergences below are not about the pattern's SHAPE but about
    what the two languages mean by the same metacharacter — `$` (Python also
    matches before a trailing newline) and `\\d` (Python matches every
    Unicode decimal digit, JavaScript matches `[0-9]`). Both are fixed on the
    Python side (`\\Z` and `re.ASCII`) rather than by widening the TypeScript
    twin, because the node's refusal is the backstop that protects the attach
    and the plugin must never hand it something it will reject.

    The TypeScript half of the pair is
    ``bridge-node/test/published-identity.test.ts``, whose tables are the same
    cases in the same order. Change one, change the other.
    """

    #: (value, expected (device_id, generation)) — every one of these must
    #: parse the same way `protocol.ts`'s `parsePublishedId` parses it.
    VALID = [
        ("indigo-1", (1, 1)),
        ("indigo-0", (0, 1)),
        ("indigo--5", (-5, 1)),
        ("indigo-123~2", (123, 2)),
        ("indigo-123~99", (123, 99)),
        # JavaScript's `Number.MAX_SAFE_INTEGER` — a language constant, not
        # one of §0's measured matter.js facts. `parsePublishedId` guards it
        # with `Number.isSafeInteger`; `bridge_protocol` mirrors the same bound
        # by hand, Python having no equivalent notion.
        ("indigo-9007199254740991", (9007199254740991, 1)),
        # Exactly PUBLISHED_ID_MAX (32) characters: "indigo-" (7) +
        # 16-digit MAX_SAFE_INTEGER + "~" (1) + 8-digit generation.
        ("indigo-9007199254740991~99999999", (9007199254740991, 99999999)),
    ]

    #: Every one of these must be refused by `parsePublishedId` too.
    INVALID = [
        "indigo-123~1",       # generation must be >= 2
        "indigo-123~0",
        "indigo-123.2",       # "." is not the generation separator
        "indigo-abc",         # non-numeric device id
        "matter-1",           # wrong prefix
        "indigo-1~",          # no digits after ~
        "indigo-",            # no device id at all
        "",
        "indigo-1 ",          # trailing whitespace
        # Python's `$` also matches before a trailing newline; JS's does not.
        # The two derivations must refuse this identically.
        "indigo-1\n",
        # One past `Number.MAX_SAFE_INTEGER` (see VALID above — a JS language
        # constant, not a measured fact).
        "indigo-9007199254740992",
        # One character over PUBLISHED_ID_MAX (33), otherwise lawful.
        "indigo-9007199254740991~999999999",
        # Python's `\d` matches every Unicode decimal digit; JavaScript's
        # matches [0-9] only. Same class of divergence as the `$`/`\Z` one
        # above, and worse in effect: `int()` is Unicode-aware too, so this
        # parsed to device id 123 here and was refused outright by
        # `parsePublishedId`. A hand-edited `.indiPref` carrying one would
        # pass plugin-side validation and then take the whole attach down
        # with `malformed_args`.
        "indigo-\u0661\u0662\u0663",              # Arabic-Indic digits
        "indigo-1\u0662",                          # ASCII and Arabic-Indic mixed
        "indigo-1~\u0662",                         # a Unicode-digit GENERATION
        "indigo-\uff11\uff12\uff13",             # fullwidth digits
    ]

    @pytest.mark.parametrize("value,expected", VALID)
    def test_parses_a_lawful_published_id(self, value, expected):
        parsed = parse_published_id(value)
        assert parsed is not None
        assert (parsed.device_id, parsed.generation) == expected

    @pytest.mark.parametrize("value", INVALID)
    def test_refuses_an_unlawful_published_id(self, value):
        assert parse_published_id(value) is None

    @pytest.mark.parametrize("value,expected", VALID)
    def test_published_id_for_is_the_inverse(self, value, expected):
        device_id, generation = expected
        assert published_id_for(device_id, generation) == value

    def test_published_id_for_defaults_to_generation_1(self):
        assert published_id_for(42) == "indigo-42"

    @pytest.mark.parametrize("device_id,generation", [
        (1, 1), (0, 1), (-5, 1), (123, 2), (123, 5), (9007199254740991, 1),
    ])
    def test_published_id_for_and_parse_round_trip(self, device_id, generation):
        parsed = parse_published_id(published_id_for(device_id, generation))
        assert parsed is not None
        assert (parsed.device_id, parsed.generation) == (device_id, generation)

    def test_next_generation_from_the_default(self):
        assert next_generation("indigo-1") == "indigo-1~2"

    def test_next_generation_from_a_generation(self):
        assert next_generation("indigo-1~2") == "indigo-1~3"

    def test_next_generation_keeps_incrementing(self):
        assert next_generation("indigo-1~9") == "indigo-1~10"

    def test_next_generation_preserves_the_device_id(self):
        assert next_generation("indigo-123~7").startswith("indigo-123~")

    def test_next_generation_rejects_an_unlawful_published_as(self):
        with pytest.raises(ValueError):
            next_generation("not-lawful")
