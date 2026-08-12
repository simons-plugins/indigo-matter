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
    ATTR_OFF_WAIT_TIME,
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
ATTR_ON_LEVEL = 0x0011           # a plain writable gap — offered by nothing
ATTR_LEVEL_TRANSITION = 0x0010   # OnOffTransitionTime — a second one, lower id
CLUSTER_DOOR_LOCK = 0x0101
ATTR_OPERATING_MODE = 0x0025     # a real setting the derived rule falsely flagged
CLUSTER_LOCALIZATION = 0x002B    # LocalizationConfiguration — lives on endpoint 0
ATTR_ACTIVE_LOCALE = 0x0000


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


def level_node(listed, **kwargs) -> NodeInfo:
    """A one-endpoint LevelControl device whose AttributeList is *listed*.

    LevelControl, not OnOff, wherever a test needs a plain "writable attribute
    the plugin does not offer". OnOff can no longer supply one: of its three
    writable attributes StartUpOnOff is offered as a setting and the other two
    are parameters of OnWithTimedOff, which the report now filters (#197). Tests
    that used OnTime as a generic example were testing the report's machinery,
    not OnOff, so they moved rather than changed meaning.
    """
    return node({
        (1, CLUSTER_LEVEL, 0x0000): 0,
        (1, CLUSTER_LEVEL, ATTR_ATTRIBUTE_LIST): list(listed),
    }, **kwargs)


def reported(survey) -> set:
    return {(item.cluster, item.attribute)
            for ep in survey.endpoints for item in ep.settable}


# ---------------------------------------------------------------------------
# The AttributeList is the evidence — nothing else is
# ---------------------------------------------------------------------------

def test_a_listed_writable_attribute_is_reported():
    survey = survey_node(level_node([0x0000, ATTR_ON_LEVEL, ATTR_LEVEL_TRANSITION,
                                     ATTR_ATTRIBUTE_LIST]))
    assert reported(survey) == {(CLUSTER_LEVEL, ATTR_ON_LEVEL),
                                (CLUSTER_LEVEL, ATTR_LEVEL_TRANSITION)}


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
    survey = survey_node(level_node(["not-a-number", ATTR_ON_LEVEL]))
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
    listed = [0x0000, ATTR_ON_LEVEL, ATTR_LEVEL_TRANSITION, ATTR_ATTRIBUTE_LIST]
    survey = survey_node(level_node(listed),
                         offered=lambda ep: {(CLUSTER_LEVEL, ATTR_LEVEL_TRANSITION)})
    assert reported(survey) == {(CLUSTER_LEVEL, ATTR_ON_LEVEL)}


def test_offered_is_asked_per_endpoint():
    """A setting is declared against Indigo device TYPES, and what type an
    endpoint became is the only thing that decides whether its field exists — so
    two endpoints of one node can legitimately give different answers."""
    listed = [0x0000, ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST]
    two = node({
        (1, CLUSTER_LEVEL, ATTR_ATTRIBUTE_LIST): listed,
        (2, CLUSTER_LEVEL, ATTR_ATTRIBUTE_LIST): listed,
    })
    survey = survey_node(
        two, offered=lambda ep: {(CLUSTER_LEVEL, ATTR_ON_LEVEL)} if ep == 1 else set())
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


@pytest.mark.parametrize("cluster,attribute,name", [
    (0x0031, 0x0004, "NetworkCommissioning.InterfaceEnabled"),
    (0x0453, 0x0000, "ThreadNetworkDirectory.PreferredExtendedPanId"),
])
def test_specific_infrastructure_clusters_stay_suppressed(cluster, attribute, name):
    """Hard-coded, NOT derived from ``INFRASTRUCTURE_CLUSTERS`` the way
    ``test_matter_plumbing_is_never_reported`` above is. That parametrize is
    self-referential — ``sorted(INFRASTRUCTURE_CLUSTERS)`` — so deleting an
    entry from the constant deletes its own test case along with it, and
    these two currently have ZERO other coverage: both are writable, absent
    from ``NOT_SETTINGS``, and would start recommending fabric plumbing (a
    network interface toggle, the Thread network directory's preferred PAN
    id) on every Thread node the moment their ``INFRASTRUCTURE_CLUSTERS``
    entry went missing.
    """
    survey = survey_node(node({
        (0, cluster, ATTR_ATTRIBUTE_LIST): [attribute, ATTR_ATTRIBUTE_LIST],
    }))
    assert survey.endpoints == (), f"{name} is plumbing, not a setting"


# ---------------------------------------------------------------------------
# Command parameters are not settings — issue #197
# ---------------------------------------------------------------------------

def test_every_generated_command_field_collision_has_been_judged_by_a_human():
    """The whole point of splitting the generator's DETECTOR from a human
    DECIDER (#197 follow-up, after ADR-0005's mechanical rule proved unsound).

    ``COMMAND_FIELD_ATTRIBUTES`` is machine-derived from the pinned model and
    moves when matter.js moves. Every member it contains must have been looked
    at by a human and filed into exactly one of ``NOT_SETTINGS`` (confirmed:
    not a setting, with a citation) or ``REVIEWED_COMMAND_FIELD_COLLISIONS``
    (confirmed: a real setting that merely collides by name, with a citation).

    THIS IS THE SAFETY PROPERTY: if a matter.js bump introduces a new writable
    attribute that happens to share a name with a command field on its own
    cluster, this test fails — loudly, with instructions — instead of the
    report silently swallowing what might be a real setting the way it
    silently swallowed ``DoorLock.OperatingMode`` until this PR. Fix a failure
    here by reading the spec for the new pair and adding it, with a citation,
    to ``NOT_SETTINGS`` if writing it means nothing on its own, or to
    ``REVIEWED_COMMAND_FIELD_COLLISIONS`` if it is a real setting. Never fix it
    by deleting or weakening this assertion.
    """
    detected = {(cluster, attribute)
                for cluster, ids in settings_report.COMMAND_FIELD_ATTRIBUTES.items()
                for attribute in ids}
    judged = set(settings_report.NOT_SETTINGS) | set(
        settings_report.REVIEWED_COMMAND_FIELD_COLLISIONS)
    unjudged = sorted(detected - judged)
    assert not unjudged, (
        "the generated COMMAND_FIELD_ATTRIBUTES has member(s) that are in neither "
        "NOT_SETTINGS nor REVIEWED_COMMAND_FIELD_COLLISIONS — a matter.js bump "
        "introduced a new writable attribute that collides by name with a command "
        "field, and nobody has judged it yet: "
        f"{[f'{cluster_label(c)} {attribute_label(c, a)}' for c, a in unjudged]}. "
        "Read the spec for each pair, decide whether writing the attribute on its "
        "own means anything, and add it (with a citation) to NOT_SETTINGS if not, "
        "or to REVIEWED_COMMAND_FIELD_COLLISIONS if it is a real setting like "
        "DoorLock.OperatingMode was."
    )


def test_every_reviewed_collision_is_still_detected():
    """The direction the test above does NOT check, and the one a mutation audit
    found missing: ``REVIEWED_COMMAND_FIELD_COLLISIONS`` exists only to excuse a
    pair the generator's detector flags (ADR-0006's whole design — a detector
    plus a human decider). An entry that stopped being detected means the
    detector regressed, not that the collision went away.

    This is what catches emptying ``COMMAND_FIELD_ATTRIBUTES`` in the checked-in
    artefact: with ``detected`` empty, ``detected - judged`` above is vacuously
    empty too, so
    ``test_every_generated_command_field_collision_has_been_judged_by_a_human``
    goes green for the wrong reason at the same moment
    ``test_no_declared_setting_is_a_command_parameter`` does. Neither the
    generator's own floor (``KNOWN_COMMAND_FIELD_ATTRIBUTES``) nor CI catches
    that on its own — the generator's assertion only runs when a human
    regenerates with ``@matter/model`` in hand, and CI has neither Node nor
    that package.
    """
    detected = {(cluster, attribute)
                for cluster, ids in settings_report.COMMAND_FIELD_ATTRIBUTES.items()
                for attribute in ids}
    assert set(settings_report.REVIEWED_COMMAND_FIELD_COLLISIONS) <= detected


def test_the_checked_in_table_still_detects_the_four_documented_command_field_pairs():
    """Mirrors ``tools/enumerate_settings.py``'s ``KNOWN_COMMAND_FIELD_ATTRIBUTES``
    floor, but against the SHIPPED artefact rather than a live regeneration —
    the only place in CI that can check the checked-in table, since CI has no
    ``@matter/model`` to regenerate it from. If this fails, either the table was
    hand-edited (never do this — regenerate instead) or the checked-in file is
    stale against a matter.js version that legitimately removed one of these.
    """
    detected = {(cluster, attribute)
                for cluster, ids in settings_report.COMMAND_FIELD_ATTRIBUTES.items()
                for attribute in ids}
    for cluster, attribute in (
        (0x0003, 0x0000),  # Identify.IdentifyTime <- Identify()
        (0x0006, 0x4001),  # OnOff.OnTime <- OnWithTimedOff()
        (0x0006, 0x4002),  # OnOff.OffWaitTime <- OnWithTimedOff()
        (0x0030, 0x0000),  # GeneralCommissioning.Breadcrumb <- ArmFailSafe()/SetRegulatoryConfig()
    ):
        assert (cluster, attribute) in detected, (
            f"{cluster_label(cluster)} {attribute_label(cluster, attribute)} is "
            "missing from the checked-in COMMAND_FIELD_ATTRIBUTES table")


def test_not_settings_and_reviewed_collisions_are_disjoint():
    """The two lists are alternatives for one detected pair, never both — a pair
    in both would mean the report both excludes it (NOT_SETTINGS) and the
    explorer treats it as a reviewed-real setting at the same time, which is a
    contradiction the two tables must not be able to express."""
    assert not (set(settings_report.NOT_SETTINGS)
                & set(settings_report.REVIEWED_COMMAND_FIELD_COLLISIONS))


@pytest.mark.parametrize("attribute", [ATTR_ON_TIME, ATTR_OFF_WAIT_TIME])
def test_a_command_parameter_is_never_reported_as_a_gap(attribute):
    """OnTime and OffWaitTime are writable, implemented by real plugs, and not
    offered by the plugin — every condition the report otherwise needs. They are
    still not settings: they are the mandatory fields of OnWithTimedOff, set BY
    that command. #191's first live run named OffWaitTime on two plugs as a
    setting that "COULD be added", which is the false positive this removes."""
    survey = survey_node(onoff_node([0x0000, attribute, ATTR_ATTRIBUTE_LIST]))
    assert survey.endpoints == ()


def test_retiring_the_onTime_SETTING_does_not_make_it_a_reported_GAP():
    """The interaction that forced #197's two halves to ship together.

    What suppressed OnTime from the report was ``offered_setting_pairs``, which
    derives from the settings registry — so deleting the DeviceSetting would have
    turned a broken dialog field into a recurring INFO recommending it be added
    back. Stated against the registry itself rather than a hard-coded pair, so it
    keeps meaning if the declaration ever returns.
    """
    assert (CLUSTER_ON_OFF, ATTR_ON_TIME) not in settings_report.declared_pairs(), \
        "the setting is retired — see matter_handlers/settings.py"
    survey = survey_node(onoff_node(
        [0x0000, ATTR_ON_TIME, ATTR_ATTRIBUTE_LIST]), offered=lambda ep: set())
    assert survey.endpoints == (), "nothing offers it, and it must still not be reported"


def test_the_explorer_shows_command_parameters_and_says_what_they_are():
    """ADR-0004: the report is allowed to be quiet, the explorer is not allowed
    to be incomplete. Omitting the attribute would leave a user who read the spec
    wondering whether the dump was broken; labelling it answers the question the
    omission would have raised."""
    lines = "\n".join(explore_lines(node({
        (1, CLUSTER_ON_OFF, ATTR_ON_TIME): 0,
        (1, CLUSTER_ON_OFF, ATTR_START_UP_ON_OFF): 1,
    })))
    assert "OnTime (0x4001) [writable, command parameter] = 0" in lines
    assert "StartUpOnOff (0x4003) [writable] = 1" in lines


def test_door_lock_operating_mode_IS_reported_as_a_gap():
    """The false positive that broke the old command-field rule (#197
    follow-up). ``OperatingMode`` is a real DoorLock setting —
    Normal/Vacation/Privacy/NoRemoteLockUnlock (§5.2.9.24) — that merely shares
    its name with a field of the request command ``SetHolidaySchedule``
    (§5.2.10.12.4). It must be reported as a gap, not filtered.

    DoorLock is Indigo device type ``matterLock`` (see ``test_door_lock.py``),
    but ``survey_node`` decides purely from the AttributeList and does not
    consult Indigo device types at all, so a bare ``node()`` fixture is enough
    — the same pattern the parser-fix tests above already use.
    """
    survey = survey_node(node({
        (1, CLUSTER_DOOR_LOCK, ATTR_ATTRIBUTE_LIST):
            [ATTR_OPERATING_MODE, ATTR_ATTRIBUTE_LIST],
    }))
    assert reported(survey) == {(CLUSTER_DOOR_LOCK, ATTR_OPERATING_MODE)}


def test_the_explorer_does_not_label_door_lock_operating_mode_a_command_parameter():
    """The generated ``COMMAND_FIELD_ATTRIBUTES`` flags this pair, but a human
    has reviewed it (``REVIEWED_COMMAND_FIELD_COLLISIONS``) and found it to be
    a real setting — so, unlike ``OnTime``, the explorer must show it as an
    ordinary writable attribute, not a command parameter."""
    body = "\n".join(explore_lines(node({
        (1, CLUSTER_DOOR_LOCK, ATTR_OPERATING_MODE): 0,
    })))
    assert "OperatingMode (0x0025) [writable] = 0" in body
    assert "command parameter" not in body


def test_the_explorer_still_labels_on_time_a_command_parameter():
    """The control case for the test above: a NOT_SETTINGS member must still
    be labelled, so the split between the two tables actually drives the
    explorer rather than the label having quietly gone unconditional."""
    body = "\n".join(explore_lines(node({(1, CLUSTER_ON_OFF, ATTR_ON_TIME): 0})))
    assert "OnTime (0x4001) [writable, command parameter] = 0" in body


@pytest.mark.parametrize("cluster,attribute,name", [
    (0x001F, 0x0000, "AccessControl.Acl"),
    (0x001E, 0x0000, "Binding.Binding"),
    (0x003F, 0x0000, "GroupKeyManagement.GroupKeyMap"),
    (0x0041, 0x0000, "UserLabel.LabelList"),
    (0x002A, 0x0000, "OtaSoftwareUpdateRequestor.DefaultOtaProviders"),
])
def test_the_list_typed_plumbing_recovered_by_the_parser_fix_stays_quiet(
        cluster, attribute, name):
    """#197's parser fixes took the model's writable count 110 → 124 → 137 →
    141. This test is about the first step: the generator had been skipping
    every multi-line ``Attribute(``, i.e. every list- and struct-typed one.

    Seven of the fourteen it recovered are fabric plumbing: an access-control
    list, a binding table, a group-key map. Every node implements several, so
    without an INFRASTRUCTURE_CLUSTERS entry each one would have started
    recommending them as settings on the next survey of every device on the
    fabric — the wallpaper this report is built to avoid, arriving as a
    side-effect of a bug fix.
    """
    survey = survey_node(node({
        (0, cluster, ATTR_ATTRIBUTE_LIST): [attribute, ATTR_ATTRIBUTE_LIST],
    }))
    assert survey.endpoints == (), f"{name} is plumbing, not a setting"


@pytest.mark.parametrize("cluster,attribute,name", [
    (0x0202, 0x0008, "FanControl.RockSetting"),
    (0x0202, 0x000A, "FanControl.WindSetting"),
    (0x0201, 0x0050, "Thermostat.Presets"),
    (0x0201, 0x0051, "Thermostat.Schedules"),
    (0x0101, 0x002B, "DoorLock.EnablePrivacyModeButton"),
    (0x002D, 0x0000, "UnitLocalization.TemperatureUnit"),
])
def test_the_REAL_settings_recovered_by_the_parser_fix_ARE_reported(
        cluster, attribute, name):
    """The other seven, and the point of fixing the parser at all. These are
    ordinary device settings a user would recognise — a fan's oscillation, a
    lock's privacy button — that the report was structurally unable to mention.
    RockSetting and WindSetting bear directly on the open fan work in #46.
    """
    endpoint = 0 if cluster == 0x002D else 1   # UnitLocalization is a node cluster
    survey = survey_node(node({
        (endpoint, cluster, ATTR_ATTRIBUTE_LIST): [attribute, ATTR_ATTRIBUTE_LIST],
    }))
    assert reported(survey) == {(cluster, attribute)}, f"{name} is a genuine gap"


@pytest.mark.parametrize("cluster,attribute,name", [
    (0x002B, 0x0000, "LocalizationConfiguration.ActiveLocale"),
    (0x002C, 0x0000, "TimeFormatLocalization.HourFormat"),
    (0x002D, 0x0000, "UnitLocalization.TemperatureUnit"),
])
def test_the_localization_trio_is_reported_not_suppressed(cluster, attribute, name):
    """``ActiveLocale`` and ``HourFormat`` sat in INFRASTRUCTURE_CLUSTERS from
    the day the report shipped; ``TemperatureUnit`` did not — it was invisible
    to the pre-#197 parser, so it was added and then removed again within this
    same branch. All three were suppressed on the stated grounds that such
    things "belong to the fabric, not to the device's behaviour". They do not.

    They are node-level DISPLAY preferences: show °F on the thermostat, use a
    24-hour clock. A user would recognise every one of them as a setting, which
    is the test this list is supposed to apply — that argument stands on its
    own; **quality N** is not evidence for it (ADR-0006 rejects that inference).
    Only ``ActiveLocale`` carries a device-reported ``Supported*`` list, the
    ``FromAttribute`` bounds strategy the registry already implements; the
    other two have no constraint at all.

    What actually opts all three in is the Root Node device type's
    ``LanguageLocale``/``TimeLocale``/``UnitLocale`` conditions — only
    ``TemperatureUnit`` is separately FeatureMap-gated — so a node reports them
    only if it opted in, which is why un-suppressing them is not a return to
    wallpaper.
    """
    survey = survey_node(node({
        (0, cluster, ATTR_ATTRIBUTE_LIST): [attribute, ATTR_ATTRIBUTE_LIST],
    }))
    assert reported(survey) == {(cluster, attribute)}, f"{name} is a real setting"


def test_bridged_device_basic_information_node_label_stays_suppressed():
    """Hard-coded, not derived from ``INFRASTRUCTURE_CLUSTERS`` — same
    self-reference reason as ``test_specific_infrastructure_clusters_stay_suppressed``
    above: a test parametrized over ``sorted(INFRASTRUCTURE_CLUSTERS)`` loses its
    own case the moment the entry it is meant to guard is deleted.

    ``NodeLabel`` (0x0039/0x0005) inherits ``RW VM`` from ``BasicInformation`` via
    the inheritance-aware regen (#197) and every endpoint of every Matter bridge
    implements ``BridgedDeviceBasicInformation`` — without suppression the report
    would recommend it on all of them. ``BasicInformation``'s own ``NodeLabel``
    (0x0028) is already excluded for the identical reason; Indigo owns the
    device name in both cases.
    """
    survey = survey_node(node({
        (1, 0x0039, ATTR_ATTRIBUTE_LIST): [0x0005, ATTR_ATTRIBUTE_LIST],
    }))
    assert survey.endpoints == ()


def test_a_resource_monitoring_cluster_last_changed_time_IS_reported():
    """The inheritance-aware regen (#197) resolves an attribute's access through
    the cluster's base chain, and it made ``LastChangedTime`` (0x0004, ``RW VO``)
    writable on ``HepaFilterMonitoring``, ``ActivatedCarbonFilterMonitoring`` and
    ``WaterTankLevelMonitoring`` — none of which declare it on their own literal;
    all three inherit it from ``ResourceMonitoring``. The spec (cluster §2.8.6.5)
    says it is "the time at which the resource has been changed", i.e. when the
    filter was last replaced — a genuine user setting, not fabric plumbing like
    ``BridgedDeviceBasicInformation.NodeLabel`` (the fourth attribute this same
    regen turned up, and the one that IS suppressed — see
    ``INFRASTRUCTURE_CLUSTERS``). Pinned here so a future reader who sees four
    new writable attributes appear from one regen does not lump all four
    together and suppress this one too.
    """
    survey = survey_node(node({
        (1, 0x0071, ATTR_ATTRIBUTE_LIST): [0x0004, ATTR_ATTRIBUTE_LIST],  # HepaFilterMonitoring
    }))
    assert reported(survey) == {(0x0071, 0x0004)}


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

@pytest.mark.parametrize("survey", [
    survey_node(level_node([ATTR_ON_LEVEL, ATTR_LEVEL_TRANSITION, ATTR_ATTRIBUTE_LIST])),
    survey_node(node({
        (0, CLUSTER_LOCALIZATION, ATTR_ACTIVE_LOCALE): "en-US",
        (0, CLUSTER_LOCALIZATION, ATTR_ATTRIBUTE_LIST):
            [ATTR_ACTIVE_LOCALE, ATTR_ATTRIBUTE_LIST],
    })),
], ids=["ordinary-endpoint", "endpoint-0-root-node"])
def test_report_reads_as_information_not_as_a_fault(survey):
    """This fires unprompted at commissioning on hardware that is working
    perfectly. A line that reads like an error generates support traffic for a
    non-problem, which is the failure mode the issue calls out by name.

    Parametrized over an ordinary endpoint AND an endpoint-0-only survey: the
    original single-fixture version only ever built from an endpoint-1 fixture,
    so the endpoint-0 ``#204`` note (a whole separate block of prose, see
    ``report_lines``) could say "error"/"problem"/"warning"/"failed" and this
    loop would never see it.
    """
    lines = report_lines(survey)
    blob = "\n".join(lines).lower()
    assert "nothing is wrong" in blob
    assert "once per device" in blob
    assert "github.com/simons-plugins/indigo-matter/issues" in blob
    for word in ("error", "fail", "warning", "problem"):
        assert word not in blob, f"the report must not read as a fault, but says {word!r}"


def test_report_names_vendor_product_and_firmware():
    """#186 established behaviour here is firmware-specific, so the same product
    on two releases is, for this purpose, two devices."""
    lines = report_lines(survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])))
    assert "Aqara FP300 firmware 1.2.3 (node 0x38)" in lines[0]


def test_report_names_the_attribute_and_its_cluster_by_name():
    lines = report_lines(survey_node(onoff_node([ATTR_START_UP_ON_OFF, ATTR_ATTRIBUTE_LIST])))
    body = "\n".join(lines)
    assert "StartUpOnOff (0x4003)" in body
    assert "OnOff (0x0006)" in body


def test_report_counts_in_the_singular_when_there_is_one():
    lines = report_lines(survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])))
    assert "1 settable Matter attribute that" in lines[0]


def test_an_unknown_device_still_gets_an_identity_line():
    survey = survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST],
                                    vendor="", product="", firmware=""))
    assert report_lines(survey)[0].startswith("unknown device (node 0x38)")


def test_nothing_to_say_produces_no_lines_at_all():
    assert report_lines(survey_node(onoff_node([0x0000, ATTR_ATTRIBUTE_LIST]))) == []


def test_endpoints_are_titled_by_their_indigo_device_when_named():
    survey = settings_report.name_endpoints(
        survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])),
        lambda ep: ["Kitchen Plug"])
    assert "Kitchen Plug (endpoint 1)" in "\n".join(report_lines(survey))


def test_an_endpoint_with_no_indigo_device_falls_back_to_its_number():
    """Scoped to endpoint 1, not 0: an ordinary endpoint with no Indigo
    device YET (this fixture just never named one) falls back to its bare
    number, but endpoint 0's heading is fixed regardless of whether it has a
    device ("Matter root node", see the tests below — since ADR-0008 it
    usually does) — the two must not collapse into the same fallback."""
    survey = settings_report.name_endpoints(
        survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])), lambda ep: [])
    assert "  endpoint 1:" in report_lines(survey)


def test_an_ordinary_endpoints_heading_and_lines_are_unchanged():
    """Guards the new endpoint-0 framing against leaking onto an endpoint
    that DOES have an Indigo device — only endpoint 0 is special, and that
    has to stay derived from the endpoint number, not become a default."""
    survey = settings_report.name_endpoints(
        survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])),
        lambda ep: ["Kitchen Plug"])
    body = "\n".join(report_lines(survey))
    assert "Kitchen Plug (endpoint 1)" in body
    assert "root node" not in body.lower()
    assert "node-level" not in body.lower()
    assert "#212" not in body


def test_an_endpoint_0_only_survey_renders_the_root_node_heading_and_212():
    """Endpoint 0 is the Matter root node. Since ADR-0008 it DOES get an
    Indigo device (the matterNode), but no node-level SETTINGS machinery
    targets it yet (issue #212 — no node on the live fabric implements the
    localization clusters this branch is about), so its heading must say
    what it is rather than rendering a bare "endpoint 0:", and its lines
    must say support is already tracked — otherwise the generic closing
    paragraph below would read as an invitation to file a duplicate of
    #212."""
    survey = survey_node(node({
        (0, CLUSTER_LOCALIZATION, ATTR_ACTIVE_LOCALE): "en-US",
        (0, CLUSTER_LOCALIZATION, ATTR_ATTRIBUTE_LIST):
            [ATTR_ACTIVE_LOCALE, ATTR_ATTRIBUTE_LIST],
    }))
    body = "\n".join(report_lines(survey))
    assert "the Matter root node (endpoint 0)" in body
    assert "  endpoint 0:" not in body
    assert "node-level" in body.lower()
    assert "#212" in body


def test_node_level_settings_line_hedges_the_node_device_claim():
    """issue #204 review, fix D: this report runs for every node, including
    the three _ensure_node_device decline paths that leave no matterNode
    device at all (tombstoned, empty plan #105, failed ADR-0003 gate) — the
    line must not assert a node device exists, only that it is where one
    would be. Hedged the same way EndpointSurvey.title() already hedges."""
    survey = survey_node(node({
        (0, CLUSTER_LOCALIZATION, ATTR_ACTIVE_LOCALE): "en-US",
        (0, CLUSTER_LOCALIZATION, ATTR_ATTRIBUTE_LIST):
            [ATTR_ACTIVE_LOCALE, ATTR_ATTRIBUTE_LIST],
    }))
    body = "\n".join(report_lines(survey))
    assert "where one exists" in body
    assert "the node has a home now" not in body


def test_a_node_with_endpoint_0_and_an_application_endpoint_renders_each_correctly():
    """The realistic case: one node reporting both a root-node display
    preference and an ordinary per-device gap in the same block. Each
    endpoint must get its own framing — the root-node text must not bleed
    into endpoint 1's lines, and endpoint 1 must not gain a #212 reference
    that belongs only to endpoint 0."""
    survey = survey_node(node({
        (0, CLUSTER_LOCALIZATION, ATTR_ACTIVE_LOCALE): "en-US",
        (0, CLUSTER_LOCALIZATION, ATTR_ATTRIBUTE_LIST):
            [ATTR_ACTIVE_LOCALE, ATTR_ATTRIBUTE_LIST],
        (1, CLUSTER_LEVEL, ATTR_ATTRIBUTE_LIST): [ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST],
    }))
    lines = report_lines(survey)
    body = "\n".join(lines)
    assert "the Matter root node (endpoint 0)" in body
    assert "#212" in body
    assert "  endpoint 1:" in body
    assert "OnLevel (0x0011)" in body
    # The #212 reference belongs to endpoint 0's block, not endpoint 1's.
    root_index = next(i for i, ln in enumerate(lines) if "root node" in ln)
    ep1_index = next(i for i, ln in enumerate(lines) if ln == "  endpoint 1:")
    assert not any("#212" in ln for ln in lines[ep1_index:])
    assert root_index < ep1_index


def test_report_lines_endpoint_0_block_has_the_caption_before_the_attribute():
    """A structural pin on one endpoint's block, not just substring checks.

    Two mutations survive without this: moving the ``#212`` note to AFTER the
    attribute line(s) it captions (readable as "here are the attributes, oh
    and by the way #212" instead of the caption explaining what follows), and
    emitting every attribute line twice (a duplicated ``for item in
    endpoint.settable`` loop). The note-before-attribute ordering and the
    exactly-once count are both asserted directly.
    """
    survey = survey_node(node({
        (0, CLUSTER_LOCALIZATION, ATTR_ACTIVE_LOCALE): "en-US",
        (0, CLUSTER_LOCALIZATION, ATTR_ATTRIBUTE_LIST):
            [ATTR_ACTIVE_LOCALE, ATTR_ATTRIBUTE_LIST],
    }))
    lines = report_lines(survey)
    note_index = next(i for i, ln in enumerate(lines) if "#212" in ln)
    attr_index = next(i for i, ln in enumerate(lines) if "ActiveLocale" in ln)
    assert note_index < attr_index, (
        "the #212 note must caption the attribute lines that follow it, not "
        "come after them")
    assert sum(1 for ln in lines if "ActiveLocale" in ln) == 1, (
        "each attribute must be listed exactly once")


# ---------------------------------------------------------------------------
# Fingerprint + the once-per-device log
# ---------------------------------------------------------------------------

def test_fingerprint_changes_with_firmware():
    listed = [ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST]
    before = survey_node(level_node(listed, firmware="1.0.0")).fingerprint()
    after = survey_node(level_node(listed, firmware="1.1.0")).fingerprint()
    assert before != after


def test_fingerprint_changes_with_the_settable_set():
    """A first informative snapshot that arrived mid-interview must not latch a
    short answer forever — this is what makes the log self-correcting."""
    short = survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])).fingerprint()
    full = survey_node(level_node([ATTR_ON_LEVEL, ATTR_LEVEL_TRANSITION,
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
    survey = survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST, ATTR_LEVEL_TRANSITION]))
    ids = [item.attribute for ep in survey.endpoints for item in ep.settable]
    assert ids == sorted(ids) == [ATTR_LEVEL_TRANSITION, ATTR_ON_LEVEL]


def test_fingerprint_is_stable_across_attribute_list_ORDER():
    """The property the test above protects the mechanism of: a device
    re-reporting the same ids in a different order is not a change, and treating
    it as one would re-report on every reconnect."""
    one = survey_node(level_node([ATTR_ON_LEVEL, ATTR_LEVEL_TRANSITION, ATTR_ATTRIBUTE_LIST]))
    two = survey_node(level_node([ATTR_LEVEL_TRANSITION, ATTR_ATTRIBUTE_LIST, ATTR_ON_LEVEL]))
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


# ---------------------------------------------------------------------------
# issue #204 final stage — SurveyLog surfaced on the matterNode device
# ---------------------------------------------------------------------------

def test_fingerprint_for_is_none_when_never_surveyed():
    log = SurveyLog()
    assert log.fingerprint_for(0x38) is None


def test_fingerprint_for_reads_back_what_record_wrote():
    log = SurveyLog()
    fp = survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])).fingerprint()
    log.record(0x38, fp)
    assert log.fingerprint_for(0x38) == fp


def test_describe_fingerprint_of_none_says_not_yet_surveyed():
    assert settings_report.describe_fingerprint(None) == "Not yet surveyed."


def test_describe_fingerprint_of_empty_string_says_not_yet_surveyed():
    assert settings_report.describe_fingerprint("") == "Not yet surveyed."


def test_describe_fingerprint_reports_the_pending_count():
    fp = survey_node(level_node([ATTR_ON_LEVEL, ATTR_LEVEL_TRANSITION,
                                 ATTR_ATTRIBUTE_LIST])).fingerprint()
    assert settings_report.describe_fingerprint(fp) == \
        "Last survey: 2 settable attributes not yet offered."


def test_describe_fingerprint_singular_wording():
    fp = survey_node(level_node([ATTR_ON_LEVEL, ATTR_ATTRIBUTE_LIST])).fingerprint()
    assert settings_report.describe_fingerprint(fp) == \
        "Last survey: 1 settable attribute not yet offered."


def test_describe_fingerprint_of_a_fully_supported_node():
    """A node that implements nothing beyond what the plugin already offers
    still gets recorded (report_settable_attributes' "record anyway" rule) —
    the summary must say so plainly, not read as "not yet surveyed"."""
    fp = survey_node(onoff_node([0x0000, ATTR_ATTRIBUTE_LIST])).fingerprint()
    assert settings_report.describe_fingerprint(fp) == \
        "Last survey: fully supported — no settable attributes pending."


def test_describe_fingerprint_tolerates_a_malformed_fingerprint():
    """Display-only — a corrupt or hand-edited pref value must never raise
    out of the Edit Device dialog."""
    assert settings_report.describe_fingerprint("garbage#not-json") == \
        "Last survey: could not be read."


def test_describe_fingerprint_survives_a_hash_in_the_firmware_string():
    """issue #204 review, fix G.1: firmware is a vendor string and CAN contain
    "#" (e.g. "1.2.3#beta"); the JSON tail never can (NodeSurvey.fingerprint
    builds it from a list of int triples, never a string value). A bare
    ``partition("#")`` would split at the firmware's OWN "#" and hand a
    non-JSON fragment to ``json.loads``, mis-reporting "could not be read" for
    a perfectly good fingerprint — ``rpartition`` splits at the LAST "#",
    which is always the real boundary."""
    pairs = [[1, 6, 0x4003]]
    fp = f"1.2.3#beta#{json.dumps(pairs, separators=(',', ':'))}"
    assert settings_report.describe_fingerprint(fp) == \
        "Last survey: 1 settable attribute not yet offered."


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


def test_no_declared_setting_is_a_command_parameter():
    """#197 in one line, pointed forwards.

    The PR that retired onTime derived a machine-readable answer to "which
    writable attributes does a command own?" and then checked the registry
    against it only at the single point (0x0006, 0x4001). Declaring a setting for
    Identify.IdentifyTime — or for whatever a future matter.js adds — would ship
    the same dead dialog field with CI green, and the explorer would label the
    very same attribute "[writable, command parameter]", so the plugin would
    contradict itself in two surfaces.
    """
    parameters = {(cluster, attribute)
                  for cluster, ids in settings_report.COMMAND_FIELD_ATTRIBUTES.items()
                  for attribute in ids}
    assert not (parameters & settings_report.declared_pairs())


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
