"""Issue #364 / S1 — the pure replacement planner (``node_replace``).

No Indigo in this module, so no fixtures: refs in, plan / refusals / rows out.
Each test's docstring names the spec bullet (docs/plans/issue-364-replace-node.md,
"Tests > S1") it pins.

Refusal TEXT is only pinned where the spec pins it ("Migrate an exported
accessory…", the referrers' names). Everything else is pinned structurally
(count and position), so a rewording cannot break these tests but a dropped,
duplicated or reordered refusal does.
"""
from __future__ import annotations

import pytest

import node_replace
from node_replace import DeviceRef


def _old(*, extra=()):
    return [
        DeviceRef(1001, 0, "matterNode", "Coffee Machine Matter Device"),
        DeviceRef(1002, 1, "matterRelay", "Coffee Machine Matter Switch"),
        *extra,
    ]


def _new(*, extra=(), relay_type="matterRelay"):
    return [
        DeviceRef(2001, 0, "matterNode", "Coffee Machine"),
        DeviceRef(2002, 1, relay_type, "Coffee Machine - Switch"),
        *extra,
    ]


def _plan(old=None, new=None, old_node=52, new_node=67):
    return node_replace.plan_replacement(
        old_node, _old() if old is None else old, new_node, _new() if new is None else new)


def _refusals(plan, *, allow_mismatch=False, exported=(), references=None, errors=None):
    return node_replace.refusal_reasons(
        plan, allow_mismatch=allow_mismatch, exported_ids=set(exported),
        references=references or {}, reference_errors=errors or {})


# ---------------------------------------------------------------------------
# plan_replacement
# ---------------------------------------------------------------------------

def test_same_shape_pairs_every_device_and_matches():
    """S1: same shape -> two pairs, nothing unmatched, shape_matches."""
    plan = _plan()
    assert (plan.old_node, plan.new_node) == (52, 67)
    assert [(o.dev_id, n.dev_id) for o, n in plan.pairs] == [(1001, 2001), (1002, 2002)]
    assert plan.unmatched_old == ()
    assert plan.unmatched_new == ()
    assert plan.shape_matches is True


def test_pairs_are_sorted_by_endpoint_then_type_whatever_the_input_order():
    """S1/spec S1 signature: pairs sorted by (endpoint_id, type_id)."""
    plan = _plan(old=list(reversed(_old())), new=list(reversed(_new())))
    assert [(o.endpoint_id, o.type_id) for o, _n in plan.pairs] == [(0, "matterNode"), (1, "matterRelay")]


def test_extra_endpoint_on_new_node_is_unmatched_new():
    """S1: extra endpoint on the new node -> unmatched_new, shape no longer matches."""
    extra = DeviceRef(2003, 2, "matterRelay", "Coffee Machine - Switch 2")
    plan = _plan(new=_new(extra=[extra]))
    assert len(plan.pairs) == 2
    assert plan.unmatched_new == (extra,)
    assert plan.unmatched_old == ()
    assert plan.shape_matches is False


def test_type_differing_on_ep1_leaves_both_sides_unmatched():
    """S1: a type differing on ep1 -> both sides unmatched (ep0 still pairs)."""
    plan = _plan(new=_new(relay_type="matterDimmer"))
    assert [(o.dev_id, n.dev_id) for o, n in plan.pairs] == [(1001, 2001)]
    assert [ref.dev_id for ref in plan.unmatched_old] == [1002]
    assert [ref.dev_id for ref in plan.unmatched_new] == [2002]
    assert plan.shape_matches is False


# ---------------------------------------------------------------------------
# refusal_reasons — each fires alone
# ---------------------------------------------------------------------------

def test_clean_plan_has_no_refusals():
    """S1: baseline for 'each refusal fires alone' — a clean plan refuses nothing."""
    assert _refusals(_plan()) == []


def test_same_node_alone_refuses_once():
    """S1: refusal 1 (same node) fires alone."""
    assert len(_refusals(_plan(new_node=52))) == 1


def test_no_pairs_alone_refuses_once():
    """S1: refusal 2 (no pairs at all) fires alone (mismatch waived so only it is left)."""
    plan = _plan(new=[DeviceRef(2009, 5, "matterDimmer", "Unrelated")])
    assert plan.pairs == ()
    assert len(_refusals(plan, allow_mismatch=True)) == 1


def test_shape_mismatch_alone_refuses_once():
    """S1: refusal 3 (shape mismatch) fires alone."""
    extra = DeviceRef(2003, 2, "matterRelay", "Coffee Machine - Switch 2")
    assert len(_refusals(_plan(new=_new(extra=[extra])))) == 1


def test_exported_paired_new_device_alone_refuses_with_the_migrate_message():
    """S1: refusal 4 (exported paired new device) fires alone, naming 'Migrate an exported accessory…'."""
    reasons = _refusals(_plan(), exported={2002})
    assert len(reasons) == 1
    assert "Migrate an exported accessory" in reasons[0]


def test_referenced_paired_new_device_alone_refuses_naming_the_referrers():
    """S1: refusal 5 (referenced paired new device) fires alone and names the referrers."""
    reasons = _refusals(_plan(), references={2002: ["Trigger: Coffee on", "Schedule: Morning"]})
    assert len(reasons) == 1
    assert "Trigger: Coffee on" in reasons[0]
    assert "Schedule: Morning" in reasons[0]


def test_reference_lookup_error_alone_refuses_never_silence():
    """S1: refusal 6 — a reference-lookup error refuses (a failed check is not a clean bill of health)."""
    reasons = _refusals(_plan(), errors={2002: "getDependencies blew up"})
    assert len(reasons) == 1


# ---------------------------------------------------------------------------
# refusal_reasons — order when several apply
# ---------------------------------------------------------------------------

def test_export_then_reference_then_lookup_error_order_holds():
    """S1: order 4 (export) < 5 (reference) < 6 (lookup error) on one device."""
    reasons = _refusals(
        _plan(), exported={2002}, references={2002: ["Trigger: Coffee on"]},
        errors={2002: "boom"})
    assert len(reasons) == 3
    assert "Migrate an exported accessory" in reasons[0]
    assert "Trigger: Coffee on" in reasons[1]
    assert "Migrate an exported accessory" not in reasons[2]
    assert "Trigger: Coffee on" not in reasons[2]


def test_mismatch_comes_before_the_export_refusal():
    """S1: order 3 (mismatch) before 4 (export)."""
    extra = DeviceRef(2003, 2, "matterRelay", "Coffee Machine - Switch 2")
    reasons = _refusals(_plan(new=_new(extra=[extra])), exported={2002})
    assert len(reasons) == 2
    assert "Migrate an exported accessory" not in reasons[0]
    assert "Migrate an exported accessory" in reasons[1]


def test_same_node_comes_before_everything_else():
    """S1: order 1 (same node) before the rest."""
    reasons = _refusals(_plan(new_node=52), exported={2002})
    assert len(reasons) == 2
    assert "Migrate an exported accessory" not in reasons[0]
    assert "Migrate an exported accessory" in reasons[1]


def test_no_pairs_also_trips_mismatch_unless_waived():
    """S1: order 2 (no pairs) before 3 (mismatch); allow_mismatch drops only the latter."""
    plan = _plan(new=[DeviceRef(2009, 5, "matterDimmer", "Unrelated")])
    assert len(_refusals(plan)) == 2
    assert len(_refusals(plan, allow_mismatch=True)) == 1


# ---------------------------------------------------------------------------
# refusal_reasons — what does NOT refuse
# ---------------------------------------------------------------------------

def test_allow_mismatch_removes_the_mismatch_refusal():
    """S1: mismatch plus allow_mismatch=True -> no mismatch refusal."""
    extra = DeviceRef(2003, 2, "matterRelay", "Coffee Machine - Switch 2")
    plan = _plan(new=_new(extra=[extra]))
    assert len(_refusals(plan)) == 1
    assert _refusals(plan, allow_mismatch=True) == []


def test_exported_unmatched_new_device_does_not_refuse():
    """S1: an exported UNMATCHED new device -> no refusal (unmatched new devices are kept)."""
    extra = DeviceRef(2003, 2, "matterRelay", "Coffee Machine - Switch 2")
    plan = _plan(new=_new(extra=[extra]))
    assert _refusals(plan, allow_mismatch=True, exported={2003}) == []


def test_referenced_or_errored_unmatched_new_device_does_not_refuse():
    """S1: only PAIRED new devices are checked for references / lookup errors."""
    extra = DeviceRef(2003, 2, "matterRelay", "Coffee Machine - Switch 2")
    plan = _plan(new=_new(extra=[extra]))
    assert _refusals(plan, allow_mismatch=True,
                     references={2003: ["Trigger: x"]}, errors={2003: "boom"}) == []


# ---------------------------------------------------------------------------
# rebound_props
# ---------------------------------------------------------------------------

OLD_PROPS = {
    "nodeId": "52", "endpointId": "1", "address": "0x34",
    "createdTypeId": "matterRelay", "startUpOnOff": "on", "onTime": "5",
    "nodeBaseName": "Coffee Machine", "softwareVersion": "1.2.3",
}


def test_rebound_props_rewrites_node_and_address_and_keeps_everything_else():
    """S1: rebound_props keeps startUpOnOff, onTime, nodeBaseName, createdTypeId, endpointId."""
    out = node_replace.rebound_props(OLD_PROPS, 67, software_version=None)
    assert out["nodeId"] == "67"
    assert out["address"] == "0x43"
    for key in ("startUpOnOff", "onTime", "nodeBaseName", "createdTypeId", "endpointId"):
        assert out[key] == OLD_PROPS[key], key
    assert set(out) == set(OLD_PROPS)


def test_rebound_props_does_not_mutate_its_input():
    """S1: rebound_props returns a copy and does not mutate its input."""
    snapshot = dict(OLD_PROPS)
    out = node_replace.rebound_props(OLD_PROPS, 67, software_version="2.0.0")
    assert OLD_PROPS == snapshot
    assert out is not OLD_PROPS


def test_rebound_props_sets_software_version_only_when_given():
    """S1: softwareVersion is set only when given; absent otherwise (old value kept)."""
    given = node_replace.rebound_props(OLD_PROPS, 67, software_version="2.0.0")
    assert given["softwareVersion"] == "2.0.0"
    omitted = node_replace.rebound_props(OLD_PROPS, 67, software_version=None)
    assert omitted["softwareVersion"] == "1.2.3"
    bare = node_replace.rebound_props({k: v for k, v in OLD_PROPS.items() if k != "softwareVersion"},
                                      67, software_version=None)
    assert "softwareVersion" not in bare


# ---------------------------------------------------------------------------
# preview_rows
# ---------------------------------------------------------------------------

def _flat(rows):
    return " | ".join(" ".join(str(part) for part in row) for row in rows)


def test_preview_rows_one_per_pair_names_the_role_and_where_it_moves():
    """Spec S1 preview_rows: 'Switch → moves to 0x43'; names live in the pickers, not the rows."""
    rows = node_replace.preview_rows(_plan(), [])
    assert all(isinstance(row, tuple) and len(row) == 2 for row in rows)
    assert [label for _v, label in rows] == ["Node device → moves to 0x43", "Switch → moves to 0x43"]


def test_preview_rows_unmatched_and_extra_rows_are_short():
    """Unmatched old stays, unmatched new is kept; values are unique strings."""
    extra = DeviceRef(2003, 2, "matterTemperatureSensor", "Coffee Machine - Temperature")
    old_only = DeviceRef(1003, 3, "matterMotionSensor", "Coffee Machine Matter Motion")
    plan = _plan(old=_old(extra=[old_only]), new=_new(extra=[extra]))
    rows = node_replace.preview_rows(plan, [])
    labels = [label for _v, label in rows]
    assert "Motion: no match, stays" in labels
    assert "Temperature (new): kept" in labels
    values = [row[0] for row in rows]
    assert len(values) == len(set(values)), "a list needs unique values"


def test_preview_rows_refusals_collapse_to_one_short_row():
    """Several refusals still give one row; the full reasons come from the Replace click."""
    rows = node_replace.preview_rows(_plan(), ["Refusal reason one", "Refusal reason two"])
    assert rows == [("refuse", "✗ Can't replace: click Replace for the reason")]


def test_preview_rows_no_longer_carry_the_blind_spot_note():
    """The getDependencies blind-spot note moved to a static label under the list."""
    assert "Lamplighter" not in _flat(node_replace.preview_rows(_plan(), []))


@pytest.mark.parametrize("refusals", [[], ["one"]])
def test_preview_rows_values_stay_unique_with_and_without_refusals(refusals):
    """Spec S1 preview_rows: unique values regardless of refusal count."""
    values = [row[0] for row in node_replace.preview_rows(_plan(), refusals)]
    assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# D8 carry: drop_new_node_device (review fix 2)
# ---------------------------------------------------------------------------

def _carry_plan():
    """Old node had its node device removed by hand: only the relay remains. New node has both."""
    return node_replace.plan_replacement(
        52, [DeviceRef(1002, 1, "matterRelay", "Coffee Machine Matter Switch")],
        67, _new(), drop_new_node_device=True)


def test_drop_new_node_device_moves_it_to_extra_deletes_and_is_not_a_mismatch():
    """Review fix 2: the new matterNode goes to extra_deletes; the absent old one is not a shape mismatch."""
    plan = _carry_plan()
    assert [r.dev_id for r in plan.extra_deletes] == [2001]
    assert plan.unmatched_new == ()
    assert plan.shape_matches is True
    assert _refusals(plan) == []


def test_without_the_flag_the_new_node_device_stays_unmatched():
    plan = node_replace.plan_replacement(
        52, [DeviceRef(1002, 1, "matterRelay", "x")], 67, _new())
    assert plan.extra_deletes == ()
    assert [r.dev_id for r in plan.unmatched_new] == [2001]


def test_an_exported_carried_node_device_refuses():
    """Review fix 2: the export check covers extra_deletes."""
    reasons = _refusals(_carry_plan(), exported=[2001])
    assert len(reasons) == 1 and "Migrate an exported accessory" in reasons[0]


def test_a_referenced_carried_node_device_refuses_naming_the_referrer():
    """Review fix 2: the reference check covers extra_deletes."""
    reasons = _refusals(_carry_plan(), references={2001: ["Coffee on"]})
    assert len(reasons) == 1 and "Coffee on" in reasons[0]


def test_a_failed_reference_lookup_on_a_carried_node_device_refuses():
    reasons = _refusals(_carry_plan(), errors={2001: "boom"})
    assert len(reasons) == 1 and "boom" in reasons[0]


def test_preview_labels_the_carried_node_device_as_deleted():
    """Review fix 2: the D8 extra delete reads 'Node device (new): deleted'."""
    rows = node_replace.preview_rows(_carry_plan(), [])
    assert "Node device (new): deleted" in [label for _value, label in rows]
    assert len({value for value, _label in rows}) == len(rows)


def test_a_device_on_both_sides_of_the_plan_refuses():
    """Review fix 3: the same dev_id on the old and new side (stale index) refuses, and is not a clean plan."""
    shared = DeviceRef(1002, 1, "matterRelay", "Coffee Machine Matter Switch")
    plan = _plan(old=[DeviceRef(1001, 0, "matterNode", "n"), shared],
                 new=[DeviceRef(2001, 0, "matterNode", "m"), shared])
    reasons = _refusals(plan)
    assert len(reasons) == 1 and "both nodes" in reasons[0]
