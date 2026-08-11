"""Tests for the settable-attribute report and the read-only explorer — #191.

``settings_report`` is Indigo-free and matter-server-free, so everything about
what a device exposes is decided here against a ``NodeInfo`` built by hand from
the shape matter-server really sends.

The case worth stating up front, because it is the reason the module exists:
issue #190's device attribution was WRONG — it named the Shelly 1PM Mini Gen3 as
implementing OnOff's Lighting feature and the IKEA GRILLPLATS as lacking it,
when the live devices are the exact opposite. That error survived into the
issue, into the fixtures, and into a release. So the fixtures below are built
from AttributeLists, the same evidence the production code uses, and no test
asserts anything about which real product implements what — that claim is what
went wrong last time.

Covers:
- The AttributeList, not the values, decides what is reported
- Nothing reported for attributes already offered, already driven, or plumbing
- Wording: information, never a fault
- The once-per-device log: fingerprint, firmware change, decommission
- The explorer's dump, its scoping, and its truncation
"""
from __future__ import annotations

import json

import pytest

import settings_report
from matter_handlers.settings import (
    ATTR_ATTRIBUTE_LIST,
    ATTR_ON_TIME,
    ATTR_START_UP_ON_OFF,
    CLUSTER_ON_OFF,
)
from matter_model import NodeInfo
from settings_report import (
    DRIVEN_ATTRIBUTES,
    INFRASTRUCTURE_CLUSTERS,
    SurveyLog,
    attribute_label,
    cluster_label,
    explore_lines,
    report_lines,
    survey_node,
)

CLUSTER_OCCUPANCY = 0x0406
ATTR_HOLD_TIME = 0x0003
ATTR_PIR_DELAY = 0x0010          # PirOccupiedToUnoccupiedDelay — writable, never offered
CLUSTER_THERMOSTAT = 0x0201
ATTR_HEAT_SETPOINT = 0x0012      # driven as an ordinary Indigo control
ATTR_LOCAL_CALIBRATION = 0x0010  # LocalTemperatureCalibration — a real gap
CLUSTER_BASIC_INFO = 0x0028
ATTR_NODE_LABEL = 0x0005
CLUSTER_LEVEL = 0x0008
ATTR_ON_LEVEL = 0x0011


def node(attributes: dict, *, node_id: int = 0x38, vendor: str = "Aqara",
         product: str = "FP300", firmware: str = "1.2.3") -> NodeInfo:
    """A NodeInfo carrying exactly the attributes given, and nothing else."""
    return NodeInfo(node_id=node_id, vendor_name=vendor, product_name=product,
                    sw_version=firmware, attributes=dict(attributes))


def onoff_node(listed, **kwargs) -> NodeInfo:
    """A one-endpoint OnOff device whose AttributeList is *listed*."""
    return node({
        (1, CLUSTER_ON_OFF, 0x0000): False,
        (1, CLUSTER_ON_OFF, ATTR_ATTRIBUTE_LIST): list(listed),
    }, **kwargs)


def reported(survey) -> set:
    return {(item.cluster, item.attribute)
            for ep in survey.endpoints for item in ep.settable}


# ---------------------------------------------------------------------------
# The AttributeList is the evidence — nothing else is
# ---------------------------------------------------------------------------

def test_a_listed_writable_attribute_is_reported():
    survey = survey_node(onoff_node([0x0000, ATTR_ON_TIME, ATTR_START_UP_ON_OFF,
                                     ATTR_ATTRIBUTE_LIST]))
    assert reported(survey) == {(CLUSTER_ON_OFF, ATTR_ON_TIME),
                                (CLUSTER_ON_OFF, ATTR_START_UP_ON_OFF)}


def test_an_unlisted_attribute_is_not_reported_however_capable_the_cluster_is():
    """The OnOff cluster HAS three writable attributes in the spec. This device
    says it implements none of them, and the device is the authority — the
    inverse of the mistake #190 made by reasoning from the product name."""
    survey = survey_node(onoff_node([0x0000, ATTR_ATTRIBUTE_LIST]))
    assert survey.endpoints == ()


def test_a_missing_attribute_list_reports_nothing_rather_than_guessing():
    """Unknown is not "implements everything". A partial interview must
    under-report; inventing capabilities is what the whole module exists to
    stop."""
    survey = survey_node(node({(1, CLUSTER_ON_OFF, 0x0000): False}))
    assert survey.endpoints == ()


def test_an_unparseable_attribute_list_reports_nothing():
    """``implements`` refuses a confident answer from a list it could only
    partly read, and that refusal has to survive up to here."""
    survey = survey_node(onoff_node(["not-a-number", ATTR_ON_TIME]))
    assert survey.endpoints == ()


def test_a_value_in_the_snapshot_is_not_evidence_of_implementation():
    """Indigo supplies a default for every declared state and matter-server
    caches whatever it once saw, so a VALUE proves nothing. Only the list does.
    """
    survey = survey_node(node({
        (1, CLUSTER_ON_OFF, ATTR_START_UP_ON_OFF): 1,     # a real cached value
        (1, CLUSTER_ON_OFF, ATTR_ATTRIBUTE_LIST): [0x0000],  # …but not implemented
    }))
    assert survey.endpoints == ()


# ---------------------------------------------------------------------------
# What is deliberately NOT reported
# ---------------------------------------------------------------------------

def test_a_setting_the_plugin_already_offers_here_is_not_reported():
    listed = [0x0000, ATTR_ON_TIME, ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST]
    survey = survey_node(onoff_node(listed),
                         offered=lambda ep: {(CLUSTER_ON_OFF, ATTR_START_UP_ON_OFF)})
    assert reported(survey) == {(CLUSTER_ON_OFF, ATTR_ON_TIME)}


def test_offered_is_asked_per_endpoint():
    """A setting is declared against Indigo device TYPES, and what type an
    endpoint became is the only thing that decides whether its field exists — so
    two endpoints of one node can legitimately give different answers."""
    listed = [0x0000, ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST]
    two = node({
        (1, CLUSTER_ON_OFF, ATTR_ATTRIBUTE_LIST): listed,
        (2, CLUSTER_ON_OFF, ATTR_ATTRIBUTE_LIST): listed,
    })
    survey = survey_node(
        two, offered=lambda ep: {(CLUSTER_ON_OFF, ATTR_ON_TIME)} if ep == 1 else set())
    assert [ep.endpoint for ep in survey.endpoints] == [2]


def test_an_attribute_the_plugin_drives_as_a_control_is_not_reported():
    """OccupiedHeatingSetpoint is writable and the plugin writes it — from the
    ordinary Indigo thermostat action. Reporting it would send a user to file an
    issue for a feature that shipped."""
    survey = survey_node(node({
        (1, CLUSTER_THERMOSTAT, ATTR_ATTRIBUTE_LIST):
            [ATTR_HEAT_SETPOINT, ATTR_LOCAL_CALIBRATION, ATTR_ATTRIBUTE_LIST],
    }))
    assert reported(survey) == {(CLUSTER_THERMOSTAT, ATTR_LOCAL_CALIBRATION)}


@pytest.mark.parametrize("cluster", sorted(INFRASTRUCTURE_CLUSTERS))
def test_matter_plumbing_is_never_reported(cluster):
    """A commissioning breadcrumb or a device's NodeLabel is not a setting
    anyone would ask for, and burying the two useful lines under them is how an
    automatic INFO becomes wallpaper."""
    writable = settings_report.WRITABLE_ATTRIBUTES.get(cluster, frozenset())
    survey = survey_node(node({
        (0, cluster, ATTR_ATTRIBUTE_LIST): sorted(writable) + [ATTR_ATTRIBUTE_LIST],
    }))
    assert survey.endpoints == ()


def test_a_cluster_with_no_writable_attributes_at_all_is_skipped():
    survey = survey_node(node({
        (1, 0x0402, ATTR_ATTRIBUTE_LIST): [0x0000, ATTR_ATTRIBUTE_LIST],  # TemperatureMeasurement
    }))
    assert survey.endpoints == ()


def test_a_cluster_the_pinned_model_has_never_heard_of_is_skipped():
    """A manufacturer-specific cluster has no spec writability to consult, and
    guessing would be the one thing a generated table exists to avoid."""
    survey = survey_node(node({
        (1, 0xFC42, ATTR_ATTRIBUTE_LIST): [0x0000, 0x0001, ATTR_ATTRIBUTE_LIST],
    }))
    assert survey.endpoints == ()


def test_the_deprecated_pir_delays_ARE_reported():
    """Deprecated in the spec, still settable, and still not offered — the plugin
    declares HoldTime instead. A user with pre-1.4 firmware genuinely has these
    and nothing else, which is exactly the gap worth hearing about."""
    survey = survey_node(node({
        (1, CLUSTER_OCCUPANCY, ATTR_ATTRIBUTE_LIST):
            [ATTR_PIR_DELAY, ATTR_ATTRIBUTE_LIST],
    }))
    assert reported(survey) == {(CLUSTER_OCCUPANCY, ATTR_PIR_DELAY)}


# ---------------------------------------------------------------------------
# The log block
# ---------------------------------------------------------------------------

def test_report_reads_as_information_not_as_a_fault():
    """This fires unprompted at commissioning on hardware that is working
    perfectly. A line that reads like an error generates support traffic for a
    non-problem, which is the failure mode the issue calls out by name."""
    lines = report_lines(survey_node(onoff_node(
        [ATTR_ON_TIME, ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST])))
    blob = "\n".join(lines).lower()
    assert "nothing is wrong" in blob
    assert "once per device" in blob
    assert "github.com/simons-plugins/indigo-matter/issues" in blob
    for word in ("error", "fail", "warning", "problem"):
        assert word not in blob, f"the report must not read as a fault, but says {word!r}"


def test_report_names_vendor_product_and_firmware():
    """#186 established behaviour here is firmware-specific, so the same product
    on two releases is, for this purpose, two devices."""
    lines = report_lines(survey_node(onoff_node([ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST])))
    assert "Aqara FP300 firmware 1.2.3 (node 0x38)" in lines[0]


def test_report_names_the_attribute_and_its_cluster_by_name():
    lines = report_lines(survey_node(onoff_node([ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST])))
    body = "\n".join(lines)
    assert "StartUpOnOff (0x4003)" in body
    assert "OnOff (0x0006)" in body


def test_report_counts_in_the_singular_when_there_is_one():
    lines = report_lines(survey_node(onoff_node([ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST])))
    assert "1 settable Matter attribute that" in lines[0]


def test_an_unknown_device_still_gets_an_identity_line():
    survey = survey_node(onoff_node([ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST],
                                    vendor="", product="", firmware=""))
    assert report_lines(survey)[0].startswith("unknown device (node 0x38)")


def test_nothing_to_say_produces_no_lines_at_all():
    assert report_lines(survey_node(onoff_node([0x0000, ATTR_ATTRIBUTE_LIST]))) == []


def test_endpoints_are_titled_by_their_indigo_device_when_named():
    survey = settings_report.name_endpoints(
        survey_node(onoff_node([ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST])),
        lambda ep: ["Kitchen Plug"])
    assert "Kitchen Plug (endpoint 1)" in "\n".join(report_lines(survey))


def test_an_endpoint_with_no_indigo_device_falls_back_to_its_number():
    survey = settings_report.name_endpoints(
        survey_node(onoff_node([ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST])), lambda ep: [])
    assert "  endpoint 1:" in report_lines(survey)


# ---------------------------------------------------------------------------
# Fingerprint + the once-per-device log
# ---------------------------------------------------------------------------

def test_fingerprint_changes_with_firmware():
    listed = [ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST]
    before = survey_node(onoff_node(listed, firmware="1.0.0")).fingerprint()
    after = survey_node(onoff_node(listed, firmware="1.1.0")).fingerprint()
    assert before != after


def test_fingerprint_changes_with_the_settable_set():
    """A first informative snapshot that arrived mid-interview must not latch a
    short answer forever — this is what makes the log self-correcting."""
    short = survey_node(onoff_node([ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST])).fingerprint()
    full = survey_node(onoff_node([ATTR_ON_TIME, ATTR_START_UP_ON_OFF,
                                   ATTR_ATTRIBUTE_LIST])).fingerprint()
    assert short != full


def test_the_reported_order_does_not_follow_the_wire():
    """matter-server gives no ordering guarantee for an AttributeList, so the
    survey walks the MODEL's writable set and filters, rather than walking the
    device's list. Asserting ascending ids is what tells the two apart: iterating
    the wire list would echo the order it arrived in.

    (Written this way after a mutation check: the obvious version — two surveys
    from differently-ordered lists have equal fingerprints — passes even with
    every sort removed, because a frozenset of small ints iterates stably within
    a process. It proved the property and pinned none of the code.)
    """
    survey = survey_node(onoff_node([ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST, ATTR_ON_TIME]))
    ids = [item.attribute for ep in survey.endpoints for item in ep.settable]
    assert ids == sorted(ids) == [ATTR_ON_TIME, ATTR_START_UP_ON_OFF]


def test_fingerprint_is_stable_across_attribute_list_ORDER():
    """The property the test above protects the mechanism of: a device
    re-reporting the same ids in a different order is not a change, and treating
    it as one would re-report on every reconnect."""
    one = survey_node(onoff_node([ATTR_ON_TIME, ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST]))
    two = survey_node(onoff_node([ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST, ATTR_ON_TIME]))
    assert one.fingerprint() == two.fingerprint()


def test_survey_log_reports_once_then_stays_quiet():
    log = SurveyLog()
    assert log.should_report(0x38, "fp-1")
    log.record(0x38, "fp-1")
    assert not log.should_report(0x38, "fp-1")


def test_survey_log_reports_again_when_the_answer_changes():
    log = SurveyLog()
    log.record(0x38, "fp-1")
    assert log.should_report(0x38, "fp-2")


def test_survey_log_forgets_a_decommissioned_node():
    """Node ids are re-used across a decommission/recommission cycle, so keeping
    the mark would let a genuinely different device inherit it."""
    log = SurveyLog()
    log.record(0x38, "fp-1")
    log.forget(0x38)
    assert log.should_report(0x38, "fp-1")


def test_survey_log_round_trips_through_json():
    saved = []
    log = SurveyLog(save=saved.append)
    log.record(0x38, "fp-1")
    assert json.loads(saved[-1]) == {"56": "fp-1"}

    restored = SurveyLog()
    restored.load(saved[-1])
    assert not restored.should_report(0x38, "fp-1")


@pytest.mark.parametrize("blob", ["", "   ", "not json", "[1,2,3]", None, 42, {"a": 1}])
def test_survey_log_starts_empty_on_an_unusable_blob(blob):
    """One duplicate INFO is a far better outcome than a reconcile pass that
    dies on its own bookkeeping."""
    log = SurveyLog()
    log.load(blob)
    assert log.should_report(1, "anything")


def test_a_failing_save_never_escapes():
    def boom(_blob):
        raise OSError("prefs are gone")

    log = SurveyLog(save=boom)
    log.record(0x38, "fp-1")          # must not raise
    assert not log.should_report(0x38, "fp-1")


def test_forget_of_an_unknown_node_does_not_write():
    saved = []
    log = SurveyLog(save=saved.append)
    log.forget(0x99)
    assert saved == []


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def test_cluster_label_always_carries_the_id():
    """The id is what a maintainer looks the cluster up by, and an unrecognised
    cluster has nothing else."""
    assert cluster_label(CLUSTER_ON_OFF) == "OnOff (0x0006)"
    assert cluster_label(0xFC42) == "cluster 0xFC42"


def test_attribute_label_names_global_attributes():
    """The model defines the globals once on a base element rather than per
    cluster, so they are named in settings_report — without that, every dump
    would print the most common attribute on the wire as 'attribute 0xFFFB'."""
    assert attribute_label(CLUSTER_ON_OFF, ATTR_ATTRIBUTE_LIST) == "AttributeList (0xFFFB)"
    assert attribute_label(0xFC42, 0x0001) == "attribute 0x0001"


# ---------------------------------------------------------------------------
# The explorer (deliverable A)
# ---------------------------------------------------------------------------

EXPLORE_NODE = node({
    (0, CLUSTER_BASIC_INFO, ATTR_NODE_LABEL): "Kitchen",
    (1, CLUSTER_ON_OFF, 0x0000): True,
    (1, CLUSTER_ON_OFF, ATTR_START_UP_ON_OFF): 1,
    (1, CLUSTER_LEVEL, 0x0000): 254,
})


def test_explore_dumps_everything_by_default():
    body = "\n".join(explore_lines(EXPLORE_NODE))
    assert "endpoint 0:" in body and "endpoint 1:" in body
    assert "NodeLabel (0x0005)" in body
    assert "StartUpOnOff (0x4003)" in body
    assert "CurrentLevel (0x0000)" in body


def test_explore_scopes_to_one_endpoint():
    body = "\n".join(explore_lines(EXPLORE_NODE, endpoint=1))
    assert "NodeLabel" not in body
    assert "StartUpOnOff (0x4003)" in body


def test_explore_scopes_to_one_cluster():
    body = "\n".join(explore_lines(EXPLORE_NODE, endpoint=1, cluster=CLUSTER_ON_OFF))
    assert "CurrentLevel" not in body
    assert "StartUpOnOff (0x4003)" in body


def test_explore_can_be_pointed_at_endpoint_zero():
    """Endpoint 0 is the Matter root — BasicInformation and every node-wide
    cluster live there, so it is one of the more interesting things to explore.
    A wildcard sentinel that swallowed 0 would make it the one unreachable
    address."""
    body = "\n".join(explore_lines(EXPLORE_NODE, endpoint=0))
    assert "NodeLabel (0x0005)" in body
    assert "StartUpOnOff" not in body


def test_explore_marks_writable_attributes():
    body = "\n".join(explore_lines(EXPLORE_NODE, endpoint=1, cluster=CLUSTER_ON_OFF))
    assert "StartUpOnOff (0x4003) [writable]" in body
    assert "OnOff (0x0000) [writable]" not in body


def test_explore_shows_infrastructure_the_report_filters_out():
    """Hiding things from a diagnostic is how a diagnostic stops being trusted.
    BasicInformation is plumbing for the REPORT and perfectly legitimate here."""
    assert CLUSTER_BASIC_INFO in INFRASTRUCTURE_CLUSTERS
    assert "NodeLabel" in "\n".join(explore_lines(EXPLORE_NODE, endpoint=0))


def test_explore_says_its_values_are_cached():
    """The dump and the live read are not interchangeable, and a user who acts
    on a stale reading without being told it might be stale is the failure this
    header exists to prevent."""
    header = "\n".join(explore_lines(EXPLORE_NODE)[:2]).lower()
    assert "cached" in header and "stale" in header


def test_explore_of_an_empty_scope_says_so_rather_than_printing_a_bare_header():
    body = "\n".join(explore_lines(EXPLORE_NODE, endpoint=9))
    assert "nothing reported here" in body


def test_explore_truncates_a_huge_value_and_admits_it():
    """Silently dropping the tail would make the whole dump untrustworthy."""
    big = node({(1, CLUSTER_ON_OFF, ATTR_ATTRIBUTE_LIST): list(range(500))})
    line = [ln for ln in explore_lines(big) if "AttributeList" in ln][0]
    assert "truncated" in line
    assert len(line) < settings_report.MAX_VALUE_CHARS + 200


def test_explore_renders_a_struct_in_its_WIRE_shape():
    """matter-server serialises structs with field-INDEX keys. Prettifying that
    into {"min": …} is precisely the mistake that shipped a settings field which
    never appeared (#186) — the dump must show what actually arrives."""
    struct = node({(1, CLUSTER_OCCUPANCY, 0x0004): {"0": 1, "1": 300, "2": 10}})
    body = "\n".join(explore_lines(struct))
    assert '"0": 1' in body and '"1": 300' in body


def test_explore_survives_a_value_json_cannot_encode():
    body = "\n".join(explore_lines(node({(1, CLUSTER_ON_OFF, 0x0000): {1, 2, 3}})))
    assert "endpoint 1:" in body


# ---------------------------------------------------------------------------
# The hand-maintained set, kept honest
# ---------------------------------------------------------------------------

def test_every_driven_attribute_is_really_writable_in_the_model():
    """A pair listed here suppresses a report line. One that is not writable at
    all suppresses nothing and is just a stale entry pretending to do work."""
    for cluster, attribute in DRIVEN_ATTRIBUTES:
        assert attribute in settings_report.WRITABLE_ATTRIBUTES.get(cluster, frozenset()), (
            f"{attribute_label(cluster, attribute)} on {cluster_label(cluster)} is in "
            f"DRIVEN_ATTRIBUTES but the model does not call it writable"
        )


def test_no_attribute_is_both_driven_and_declared_as_a_setting():
    """The two are alternatives: a setting goes through the verified write path,
    a control goes through the action bridge. Both would mean two UI routes to
    one attribute with different validation."""
    assert not (DRIVEN_ATTRIBUTES & settings_report.declared_pairs())


def _handler_write_targets() -> set:
    """Every ``(cluster, attribute)`` a cluster handler builds a MatterWrite for.

    A text parse, resolving constant names against the same module's own
    ``CLUSTER_*``/``ATTR_*`` definitions — the approach ``tools/enumerate_settings``
    already uses, and for the same reason: importing the handlers would drag in
    the registry and an ``indigo`` module, and this only needs to read code.
    """
    import re
    from pathlib import Path

    handler_dir = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
                   / "Contents" / "Server Plugin" / "matter_handlers")
    call = re.compile(r"MatterWrite\(\s*[^,]+,\s*[^,]+,\s*([\w.]+)\s*,\s*([\w.]+)\s*,", re.S)
    targets = set()
    for path in sorted(handler_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        consts = {name: int(value, 0) for name, value in re.findall(
            r"^([A-Z][A-Z0-9_]*)\s*=\s*(0x[0-9a-fA-F]+|\d+)\s*(?:#.*)?$", source, re.M)}

        def resolve(token, _consts=consts):
            token = token.strip()
            if token in _consts:
                return _consts[token]
            try:
                return int(token, 0)
            except ValueError:
                return None

        for raw_cluster, raw_attribute in call.findall(source):
            cluster, attribute = resolve(raw_cluster), resolve(raw_attribute)
            if cluster is not None and attribute is not None:
                targets.add((cluster, attribute))
    return targets


def test_the_parse_that_backs_the_parity_check_actually_finds_something():
    """Guards the guard. A regex that silently matched nothing would make the
    check below pass forever while enforcing nothing at all — the exact shape of
    the tautology #186 shipped."""
    assert len(_handler_write_targets()) >= 5


def test_every_attribute_a_handler_writes_is_accounted_for():
    """A handler that writes an attribute is either driving a CONTROL (declare it
    in DRIVEN_ATTRIBUTES) or applying a SETTING (declare a DeviceSetting). An
    attribute in neither would be reported to the user as a gap the plugin does
    not fill, while the plugin fills it — sending them to file an issue for a
    feature that shipped.

    This is what stops DRIVEN_ATTRIBUTES rotting: it is hand-maintained because
    nothing can derive it (a handler builds its write at call time from an
    Indigo action), so the parity has to be checked rather than assumed.
    """
    known = DRIVEN_ATTRIBUTES | settings_report.declared_pairs()
    unaccounted = sorted(_handler_write_targets() - known)
    assert not unaccounted, (
        "handler(s) write these attributes, but they are neither in "
        "DRIVEN_ATTRIBUTES nor declared as a DeviceSetting, so the settable-attribute "
        f"report will name them as gaps the plugin does not fill: "
        f"{[f'{cluster_label(c)} {attribute_label(c, a)}' for c, a in unaccounted]}"
    )


# ---------------------------------------------------------------------------
# The generated table
# ---------------------------------------------------------------------------

def test_the_generated_table_is_present_and_populated():
    """It is a checked-in build artefact — the plugin must never import
    ``@matter/model`` (ADR-0006) — so nothing at runtime would notice it having
    been emitted from an empty directory.

    Deliberately NOT asserting it is CURRENT: "current" is only knowable with the
    model in hand, which CI does not have. The header records the version it came
    from so a human can compare it against bridge-node/package.json.
    """
    from matter_handlers import writable_attributes as table

    assert table.MODEL_VERSION and table.MODEL_VERSION != "unknown"
    assert len(table.CLUSTER_NAMES) > 100
    assert sum(len(v) for v in table.WRITABLE_ATTRIBUTES.values()) > 50
    # Every writable id must be nameable, or the report prints bare hex.
    for cluster, ids in table.WRITABLE_ATTRIBUTES.items():
        for attribute in ids:
            assert table.ATTRIBUTE_NAMES.get(cluster, {}).get(attribute), (
                f"0x{cluster:04X}/0x{attribute:04X} is writable but has no name")


def test_the_settings_registry_agrees_with_the_generated_table():
    """Every declared DeviceSetting targets an attribute the model calls
    writable. One that does not is a declaration against an attribute no
    conformant device will accept a write for."""
    from matter_handlers import writable_attributes as table

    for cluster, attribute in settings_report.declared_pairs():
        assert attribute in table.WRITABLE_ATTRIBUTES.get(cluster, frozenset()), (
            f"{attribute_label(cluster, attribute)} on {cluster_label(cluster)} is declared "
            f"as a DeviceSetting but the Matter data model does not call it writable")
