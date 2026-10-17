"""Issue #364 / S2 + S3 — DeviceSync primitives and ``replace_node``.

Spec: docs/plans/issue-364-replace-node.md ("Tests > S2", "Tests > S3"). Each
test's docstring names the bullet it pins.

The "coffee" pair: a fielded-looking OLD node 52 (matterNode at ep0 + matterRelay
at ep1, user-renamed, with user props) and a freshly re-paired NEW node 67 (same
shape, address 0x43) — both built through ``create_from_raw`` so groups and the
index are real.

Two rules from the workspace degradation-path convention shape every test here:

* **Hazard simulation.** Real Indigo restarts a device when its props are
  rewritten, and the restart calls ``deviceStartComm`` -> ``note_device``, which
  overwrites ``_index[(node, ep)][type]``. Every old device here has its
  ``replacePluginPropsOnServer`` wrapped to do the same. Without it trap 1 (D1)
  cannot fail.
* **Fatal "did not happen".** A call that must not occur is a fake that BOTH
  records itself and raises, and the test asserts the record. Recording matters
  because ``replace_node`` guards each pair with a broad ``except`` — a bare
  raise could be swallowed into ``skipped`` and the test would still pass.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import protocol
from protocol import MatterEvent

from test_device_sync import _with_node_evidence, ds, indigo_env  # noqa: F401  (fixtures)
from test_handlers import RELAY_NODE

OLD_NODE, NEW_NODE = 52, 67
OLD_NODE_NAME = "Coffee Machine Matter Device"
OLD_RELAY_NAME = "Coffee Machine Matter Switch"


def _coffee_raw(node_id: int, sw_version: str) -> dict:
    raw = _with_node_evidence(RELAY_NODE)
    raw["node_id"] = node_id
    raw["attributes"]["0/40/10"] = sw_version
    return raw


def _ref(dev):
    # Imported lazily so the tests that exercise only EXISTING code (the delete_node
    # regression, the fixture guard) still collect and run before node_replace exists.
    from node_replace import DeviceRef
    return DeviceRef(dev.id, int(dev.pluginProps["endpointId"]), dev.deviceTypeId, dev.name)


def _wire_comm_restart(ds, dev, writes: list) -> None:
    """Make a props write behave like Indigo's: the device restarts and
    ``deviceStartComm`` re-registers it in the index (the D1 hazard)."""
    original = dev.replacePluginPropsOnServer

    def restarted(new_props):
        writes.append(dev.id)
        original(new_props)
        ds.note_device(dev)

    dev.replacePluginPropsOnServer = restarted


def _make_fatal_props_write(dev, calls: list) -> None:
    def forbidden(_new_props):
        calls.append(dev.id)
        raise AssertionError("replacePluginPropsOnServer must not be called on device %s" % dev.id)

    dev.replacePluginPropsOnServer = forbidden


def _wrap_delete(indigo, *, before=None, after=None, refuse=()):
    """Wrap ``indigo.device.delete``; returns the list of ids it was ASKED to delete."""
    real = indigo.device.delete
    asked: list[int] = []

    def wrapped(dev_or_id):
        dev_id = getattr(dev_or_id, "id", dev_or_id)
        asked.append(dev_id)
        if before is not None:
            before(dev_id)
        if dev_id in refuse:
            raise ValueError("Indigo refused to delete device %s" % dev_id)
        real(dev_or_id)
        if after is not None:
            after(dev_id)

    indigo.device.delete = wrapped
    return asked


class _Coffee(SimpleNamespace):
    @property
    def plan(self):
        from node_replace import plan_replacement
        return plan_replacement(OLD_NODE, [_ref(d) for d in self.old_devs],
                                NEW_NODE, [_ref(d) for d in self.new_devs])


def _build_coffee(ds, indigo_env, *, with_new_node_evidence=True):
    """Old node 52 + new node 67, both grouped by the real create path."""
    import device_sync as device_sync_mod
    indigo, devices = indigo_env
    ds.node_tombstones = device_sync_mod.NodeDeviceTombstones()
    ds.retired_nodes = device_sync_mod.NodeDeviceTombstones()

    raw_old = _coffee_raw(OLD_NODE, "1.2.3")
    ds.create_from_raw(raw_old, "Coffee Machine")
    old_relay = devices[ds.lookup(OLD_NODE, 1, "matterRelay")]
    old_node_id = ds.lookup(OLD_NODE, 0, "matterNode")
    old_node = devices[old_node_id] if old_node_id is not None else None

    # Fielded-install look: user-renamed, user-edited props.
    old_relay.name = OLD_RELAY_NAME
    old_relay.pluginProps = {**old_relay.pluginProps, "startUpOnOff": "on", "onTime": "5",
                             "nodeBaseName": "Coffee Machine"}
    if old_node is not None:
        old_node.name = OLD_NODE_NAME

    raw_new = _coffee_raw(NEW_NODE, "2.0.0")
    if not with_new_node_evidence:
        raw_new["attributes"].pop("0/40/65531")
    ds.create_from_raw(raw_new, "Coffee Machine")
    new_relay = devices[ds.lookup(NEW_NODE, 1, "matterRelay")]
    new_node_id = ds.lookup(NEW_NODE, 0, "matterNode")
    new_node = devices[new_node_id] if new_node_id is not None else None

    writes: list[int] = []
    for dev in (old_node, old_relay):
        if dev is not None:
            _wire_comm_restart(ds, dev, writes)

    old_devs = [d for d in (old_node, old_relay) if d is not None]
    new_devs = [d for d in (new_node, new_relay) if d is not None]
    return _Coffee(
        indigo=indigo, devices=devices, raw_old=raw_old, raw_new=raw_new,
        old_node=old_node, old_relay=old_relay, new_node=new_node, new_relay=new_relay,
        old_devs=old_devs, new_devs=new_devs, writes=writes,
        old_props_before={d.id: dict(d.pluginProps) for d in old_devs},
        old_names_before={d.id: d.name for d in old_devs},
    )


@pytest.fixture
def coffee(ds, indigo_env):  # noqa: F811
    return _build_coffee(ds, indigo_env)


# ===========================================================================
# S2 — DeviceSync primitives
# ===========================================================================

def test_coffee_fixture_is_really_grouped_and_has_both_nodes(ds, coffee):
    """Fixture guard: both nodes carry node devices and are real device groups (not a pinned bullet)."""
    assert coffee.old_node is not None and coffee.new_node is not None
    assert coffee.indigo.device.groups.get(coffee.old_relay.id), "old relay must be grouped"
    assert coffee.indigo.device.groups.get(coffee.new_relay.id), "new relay must be grouped"


def test_node_device_refs_snapshots_index_with_names(ds, coffee):
    """S2: node_device_refs(node) -> DeviceRef snapshot from _index, names resolved."""
    refs = ds.node_device_refs(OLD_NODE)
    assert {(r.dev_id, r.endpoint_id, r.type_id, r.name) for r in refs} == {
        (coffee.old_node.id, 0, "matterNode", OLD_NODE_NAME),
        (coffee.old_relay.id, 1, "matterRelay", OLD_RELAY_NAME),
    }
    assert ds.node_device_refs(999) == []


def test_node_device_refs_skips_a_device_deleted_out_of_band(ds, coffee):
    """S2: node_device_refs skips a device deleted out of band (the index still holds it)."""
    del coffee.devices._by_id[coffee.old_relay.id]
    assert ds.lookup(OLD_NODE, 1, "matterRelay") == coffee.old_relay.id  # index is stale on purpose
    refs = ds.node_device_refs(OLD_NODE)
    assert [r.dev_id for r in refs] == [coffee.old_node.id]


def test_delete_devices_dissolves_the_group_before_any_delete(ds, coffee):
    """S2: _delete_devices ungroups ALL before the first delete (fatal if a grouped root goes first)."""
    ids = [coffee.new_relay.id, coffee.new_node.id]
    groups = coffee.indigo.device.groups

    def nothing_may_still_be_grouped(dev_id):
        assert not groups.get(dev_id), "device %s deleted while still grouped" % dev_id
        assert not groups.get(coffee.new_node.id) and not groups.get(coffee.new_relay.id), \
            "a sibling was still grouped when the first delete ran"

    asked = _wrap_delete(coffee.indigo, before=nothing_may_still_be_grouped)
    deleted = ds._delete_devices(ids, coffee.new_node.id)
    assert set(deleted) == set(ids)
    assert set(asked) == set(ids)
    assert coffee.new_relay.id not in coffee.devices._by_id
    assert coffee.new_node.id not in coffee.devices._by_id


def test_delete_devices_marks_the_node_device_self_deleted_before_deleting_it(ds, coffee):
    """S2: _delete_devices keeps delete_node's _self_deleted_ids marking (set before the delete runs)."""
    seen = {}

    def record(dev_id):
        seen[dev_id] = coffee.new_node.id in ds._self_deleted_ids

    _wrap_delete(coffee.indigo, before=record)
    ds._delete_devices([coffee.new_relay.id, coffee.new_node.id], coffee.new_node.id)
    assert seen[coffee.new_node.id] is True


def test_delete_devices_prunes_the_index_as_each_id_is_deleted(ds, coffee):
    """S2 (D12): once the first id is deleted, lookup() no longer returns it while later deletes run."""
    first_id, second_id = coffee.new_relay.id, coffee.new_node.id
    seen = {}

    def before(dev_id):
        if dev_id == second_id:
            seen["first_lookup"] = ds.lookup(NEW_NODE, 1, "matterRelay")

    _wrap_delete(coffee.indigo, before=before)
    deleted = ds._delete_devices([first_id, second_id], second_id)
    assert deleted[0] == first_id, "fixture assumption: the relay is deleted before the node device"
    assert "first_lookup" in seen, "the second delete never ran"
    assert seen["first_lookup"] is None, "the first-deleted id was still indexed while the second delete ran"


def test_delete_devices_returns_only_what_was_really_deleted(ds, coffee):
    """S2: a refused delete is excluded from the returned ids (delete_node's contract, extracted)."""
    _wrap_delete(coffee.indigo, refuse={coffee.new_relay.id})
    deleted = ds._delete_devices([coffee.new_relay.id, coffee.new_node.id], coffee.new_node.id)
    assert deleted == [coffee.new_node.id]
    assert coffee.new_relay.id in coffee.devices._by_id


def test_delete_node_still_deletes_a_grouped_node_through_the_extracted_body(ds, coffee):
    """S2: delete_node regression — still removes every device of a grouped node and returns the ids."""
    expected = {coffee.old_node.id, coffee.old_relay.id}
    deleted = ds.delete_node(OLD_NODE)
    assert set(deleted) == expected
    assert not (expected & set(coffee.devices._by_id))
    assert ds.lookup(OLD_NODE, 1, "matterRelay") is None


def test_creation_suspended_stops_create_for_that_node_only(ds, indigo_env):  # noqa: F811
    """S2: create_devices under creation_suspended(67) creates nothing for 67, still creates for 42."""
    _indigo, devices = indigo_env
    with ds.creation_suspended(NEW_NODE):
        suspended = ds.create_from_raw(_coffee_raw(NEW_NODE, "2.0.0"), "Coffee Machine")
        assert suspended["indigoDeviceIds"] == []
        assert not list(devices), "nothing may be created for a suspended node"
        other = ds.create_from_raw(RELAY_NODE, "Office Plug")
        assert len(other["indigoDeviceIds"]) == 1
    assert ds.lookup(NEW_NODE, 1) is None


def test_creation_suspension_lifts_after_the_block(ds, indigo_env):  # noqa: F811
    """S2: once the context exits, create_devices for the node works again."""
    with ds.creation_suspended(NEW_NODE):
        pass
    result = ds.create_from_raw(_coffee_raw(NEW_NODE, "2.0.0"), "Coffee Machine")
    assert result["indigoDeviceIds"]


def test_creation_suspension_lifts_after_an_exception_in_the_body(ds, indigo_env):  # noqa: F811
    """S2: the suspension lifts after an exception in the body (try/finally)."""
    with pytest.raises(RuntimeError):
        with ds.creation_suspended(NEW_NODE):
            raise RuntimeError("boom")
    result = ds.create_from_raw(_coffee_raw(NEW_NODE, "2.0.0"), "Coffee Machine")
    assert result["indigoDeviceIds"]


def test_creation_suspension_is_reentrant(ds, indigo_env):  # noqa: F811
    """S2: re-entrant-safe — an inner exit must not release the outer suspension."""
    _indigo, devices = indigo_env
    with ds.creation_suspended(NEW_NODE):
        with ds.creation_suspended(NEW_NODE):
            pass
        assert ds.create_from_raw(_coffee_raw(NEW_NODE, "2.0.0"), "Coffee Machine")["indigoDeviceIds"] == []
        assert not list(devices)
    assert ds.create_from_raw(_coffee_raw(NEW_NODE, "2.0.0"), "Coffee Machine")["indigoDeviceIds"]


def test_node_unreachable_inverts_the_node_devices_reachable_state(ds, coffee):
    """Spec S2 node_unreachable (not in the Tests list): inverted `reachable`, None without a node device."""
    coffee.old_node.states["reachable"] = False
    assert ds.node_unreachable(OLD_NODE) is True
    coffee.old_node.states["reachable"] = True
    assert ds.node_unreachable(OLD_NODE) is False
    assert ds.node_unreachable(999) is None


# ===========================================================================
# S3 — DeviceSync.replace_node (the issue's acceptance at the DeviceSync level)
# ===========================================================================

def test_replace_rebinds_old_devices_onto_the_new_node(ds, coffee):
    """S3: nodeId '67' / address '0x43', other props AND names unchanged, duplicates gone, lookup + tombstone + knows_node."""
    result = ds.replace_node(coffee.plan)

    for dev in (coffee.old_node, coffee.old_relay):
        assert dev.pluginProps["nodeId"] == "67"
        assert dev.pluginProps["address"] == "0x43"
        before = coffee.old_props_before[dev.id]
        for key, value in before.items():
            if key in ("nodeId", "address", "softwareVersion"):
                continue
            assert dev.pluginProps[key] == value, key
        assert dev.name == coffee.old_names_before[dev.id]
    # user props survived
    assert coffee.old_relay.pluginProps["startUpOnOff"] == "on"
    assert coffee.old_relay.pluginProps["onTime"] == "5"
    # matterNode takes the new node's softwareVersion (D7); the relay gets none
    assert coffee.old_node.pluginProps["softwareVersion"] == "2.0.0"

    assert coffee.new_node.id not in coffee.devices._by_id
    assert coffee.new_relay.id not in coffee.devices._by_id
    assert ds.lookup(NEW_NODE, 1, "matterRelay") == coffee.old_relay.id
    assert ds.lookup(NEW_NODE, 0, "matterNode") == coffee.old_node.id
    assert ds.lookup(OLD_NODE, 1, "matterRelay") is None
    assert not ds.node_tombstones.is_tombstoned(NEW_NODE)
    assert ds.knows_node(NEW_NODE)

    assert set(result.rebound) == {coffee.old_node.id, coffee.old_relay.id}
    assert set(result.deleted) == {coffee.new_node.id, coffee.new_relay.id}
    assert result.skipped == []


def test_trap1_rebinding_last_means_only_the_duplicates_are_deleted(ds, coffee):
    """S3 trap 1 (D1): with note_device wired into the props write, the old devices survive; only duplicates are deleted."""
    asked = _wrap_delete(coffee.indigo)
    ds.replace_node(coffee.plan)

    assert set(asked) == {coffee.new_node.id, coffee.new_relay.id}, \
        "only ids captured up front may be deleted — never whatever the index says about node 67 later"
    assert coffee.old_node.id in coffee.devices._by_id
    assert coffee.old_relay.id in coffee.devices._by_id
    assert coffee.writes, "the comm-restart hazard was never exercised"
    assert ds.lookup(NEW_NODE, 1, "matterRelay") == coffee.old_relay.id


def test_trap2_a_node_updated_during_the_delete_recreates_nothing(ds, coffee):
    """S3 trap 2 (D2/D12): EVT_NODE_UPDATED for 67 (full node data) delivered from inside the delete creates nothing.

    No index refresh in the hook: the event must find a pruned index (D12), so a suspended
    creation and an unsuspended one are distinguishable."""
    created_before = len(coffee.indigo.device.created)
    delivered = []
    event_errors = []

    def node_updated_arrives(_dev_id):
        delivered.append(_dev_id)
        try:
            ds.handle_event(MatterEvent(
                kind=protocol.EVT_NODE_UPDATED, node_id=NEW_NODE,
                raw={"event": "node_updated", "data": coffee.raw_new}))
        except Exception as exc:  # pylint: disable=broad-except
            # What plugin._on_matter_event does: an event handler failure is
            # logged on the event thread and never reaches Indigo's delete.
            event_errors.append(exc)

    _wrap_delete(coffee.indigo, after=node_updated_arrives)
    ds.replace_node(coffee.plan)

    assert delivered, "the node_updated was never delivered"
    assert len(coffee.indigo.device.created) == created_before, "a duplicate was recreated mid-replacement"
    node67 = [d for d in coffee.devices._by_id.values() if d.pluginProps.get("nodeId") == "67"]
    assert {d.id for d in node67} == {coffee.old_node.id, coffee.old_relay.id}


def test_build_command_for_the_old_relay_targets_the_new_node(ds, coffee):
    """S3: build_command for the old relay targets node 67."""
    import indigo
    ds.replace_node(coffee.plan)

    class Action:
        deviceAction = indigo.kDeviceAction.TurnOn

    cmd = ds.build_command(coffee.old_relay, Action())
    assert cmd is not None
    assert cmd.command == "On" and cmd.node_id == NEW_NODE


def test_attribute_event_from_the_new_node_updates_the_old_relay(ds, coffee):
    """S3: an attribute event from (67, 1) updates the old relay."""
    ds.replace_node(coffee.plan)
    ds.handle_event(MatterEvent(
        kind=protocol.EVT_ATTRIBUTE_UPDATED, node_id=NEW_NODE, endpoint=1,
        cluster=0x0006, attribute=0x0000, value=True))
    assert coffee.old_relay.states["onOffState"] is True


def test_reconcile_after_replacement_creates_nothing_and_renames_nothing(ds, coffee):
    """S3 + review fix 1: reconcile_all over BOTH nodes (old 52 still commissioned but unavailable, as after a
    reset device that couldn't answer RemoveFabric) creates nothing and doesn't rename the old devices."""
    ds.replace_node(coffee.plan)
    created_before = len(coffee.indigo.device.created)
    ids_before = set(coffee.devices._by_id)

    ds.reconcile_all([{**coffee.raw_old, "available": False}, coffee.raw_new])

    assert len(coffee.indigo.device.created) == created_before
    assert set(coffee.devices._by_id) == ids_before
    assert coffee.old_node.name == OLD_NODE_NAME
    assert coffee.old_relay.name == OLD_RELAY_NAME


def test_retired_node_stays_offered_for_decommission(ds, coffee):
    """Review fix 1: a retired node is still in list_nodes/knows_node after reconcile, so it can be decommissioned."""
    ds.replace_node(coffee.plan)
    ds.reconcile_all([{**coffee.raw_old, "available": False}, coffee.raw_new])
    assert ds.knows_node(OLD_NODE)
    assert OLD_NODE in [nid for nid, _names in ds.list_nodes()]


def test_node_removed_clears_the_retirement(ds, coffee):
    """Review fix 1: a node_removed for 52 (decommission) un-retires it."""
    ds.retired_nodes.add(OLD_NODE)
    ds.handle_event(MatterEvent(kind=protocol.EVT_NODE_REMOVED, node_id=OLD_NODE))
    assert not ds.retired_nodes.is_tombstoned(OLD_NODE)


def test_retirement_survives_a_persist_load_round_trip(ds, coffee):
    """Review fix 1: the retired set is persisted like the tombstones."""
    import device_sync as device_sync_mod
    saved: list[str] = []
    ds.retired_nodes = device_sync_mod.NodeDeviceTombstones(save=saved.append)
    ds.replace_node(coffee.plan)
    assert saved, "retiring must persist"
    reloaded = device_sync_mod.NodeDeviceTombstones()
    reloaded.load(saved[-1])
    assert reloaded.is_tombstoned(OLD_NODE)
    assert not reloaded.is_tombstoned(NEW_NODE)


def test_refused_duplicate_delete_leaves_that_old_device_unbound_and_reports_it(ds, coffee):
    """S3 (D3): ep1 duplicate delete refused -> ep1 old device not rebound, in `skipped`; the node-device pair still rebinds."""
    props_calls: list[int] = []
    _make_fatal_props_write(coffee.old_relay, props_calls)
    _wrap_delete(coffee.indigo, refuse={coffee.new_relay.id})

    result = ds.replace_node(coffee.plan)

    assert props_calls == [], "the relay's props must not be written when its duplicate survived"
    assert coffee.old_relay.pluginProps["nodeId"] == "52"
    assert coffee.new_relay.id in coffee.devices._by_id
    assert coffee.old_node.pluginProps["nodeId"] == "67"
    assert [dev_id for dev_id, _why in result.skipped] == [coffee.old_relay.id]
    assert isinstance(result.skipped[0][1], str) and result.skipped[0][1]
    assert result.rebound == [coffee.old_node.id]
    assert result.deleted == [coffee.new_node.id]
    # never two devices claiming one (node, ep, type)
    assert ds.lookup(NEW_NODE, 1, "matterRelay") == coffee.new_relay.id
    assert ds.lookup(OLD_NODE, 1, "matterRelay") == coffee.old_relay.id


def test_refused_props_write_skips_that_device_and_rebinds_the_others(ds, coffee):
    """S3: a props write refused for one device -> that one is in `skipped`, the others are rebound."""
    attempts: list[int] = []

    def refused(_new_props):
        attempts.append(coffee.old_relay.id)
        raise ValueError("Indigo refused the props write")

    coffee.old_relay.replacePluginPropsOnServer = refused
    result = ds.replace_node(coffee.plan)

    assert attempts == [coffee.old_relay.id]
    assert [dev_id for dev_id, _why in result.skipped] == [coffee.old_relay.id]
    assert result.rebound == [coffee.old_node.id]
    assert coffee.old_node.pluginProps["nodeId"] == "67"
    assert coffee.old_relay.pluginProps["nodeId"] == "52"


def test_old_node_tombstoned_deletes_the_new_node_device_and_tombstones_the_new_node(ds, indigo_env):  # noqa: F811
    """S3 (D8): old node tombstoned -> new matterNode deleted (paired or not), 67 tombstoned, reconcile recreates no node device."""
    coffee = _build_coffee(ds, indigo_env)
    # The user deleted node 52's node device by hand -> tombstone it, as plugin.deviceDeleted does.
    old_node_dev = coffee.old_node
    del coffee.devices._by_id[old_node_dev.id]
    ds.note_node_device_deleted(OLD_NODE, old_node_dev.id)
    assert ds.node_tombstones.is_tombstoned(OLD_NODE)
    assert not ds.node_tombstones.is_tombstoned(NEW_NODE)
    assert coffee.new_node is not None and coffee.new_node.id in coffee.devices._by_id

    from node_replace import plan_replacement
    plan = plan_replacement(OLD_NODE, [_ref(coffee.old_relay)],
                            NEW_NODE, [_ref(coffee.new_node), _ref(coffee.new_relay)])
    assert [ref.dev_id for ref in plan.unmatched_new] == [coffee.new_node.id], "the node device is NOT paired here"
    def indigo_calls_device_deleted(dev_id):
        if dev_id == coffee.new_node.id:      # plugin.deviceDeleted only routes matterNode ids
            ds.note_node_device_deleted(NEW_NODE, dev_id)

    _wrap_delete(coffee.indigo, after=indigo_calls_device_deleted)
    result = ds.replace_node(plan)

    assert result.rebound == [coffee.old_relay.id]
    assert coffee.new_node.id not in coffee.devices._by_id, "the new node device must be deleted too"
    assert ds.node_tombstones.is_tombstoned(NEW_NODE)
    assert not ds._self_deleted_ids
    ds.reconcile_all([_coffee_raw(NEW_NODE, "2.0.0")])  # evidenced -> would create a node device if not tombstoned
    assert ds.lookup(NEW_NODE, 0, "matterNode") is None
    assert ds.lookup(NEW_NODE, 1, "matterRelay") == coffee.old_relay.id


def test_a_kept_unmatched_new_node_device_is_not_left_marked_self_deleted(ds, indigo_env):  # noqa: F811
    """S3 (D3/D8): 'replace anyway' keeps an unmatched new matterNode; it must not linger in _self_deleted_ids."""
    coffee = _build_coffee(ds, indigo_env)
    # Old node has no node device (an install that predates them), and is NOT tombstoned.
    del coffee.devices._by_id[coffee.old_node.id]
    ds.rebuild_index()
    assert not ds.node_tombstones.is_tombstoned(OLD_NODE)

    from node_replace import plan_replacement
    plan = plan_replacement(OLD_NODE, [_ref(coffee.old_relay)],
                            NEW_NODE, [_ref(coffee.new_node), _ref(coffee.new_relay)])
    assert [ref.dev_id for ref in plan.unmatched_new] == [coffee.new_node.id]
    result = ds.replace_node(plan)

    assert result.rebound == [coffee.old_relay.id]
    assert coffee.new_node.id in coffee.devices._by_id, "an unmatched new device is kept"
    assert not ds._self_deleted_ids, "a device that was never deleted must not stay marked"
    # So a LATER genuine user deletion of it still tombstones the node.
    ds.note_node_device_deleted(NEW_NODE, coffee.new_node.id)
    assert ds.node_tombstones.is_tombstoned(NEW_NODE)


def test_device_deleted_for_the_deleted_new_matternode_does_not_tombstone_it(ds, coffee):
    """S3 (D8): deviceDeleted for the deleted duplicate matterNode (note_node_device_deleted) must not tombstone 67."""
    def indigo_calls_device_deleted(dev_id):
        if dev_id == coffee.new_node.id:      # plugin.deviceDeleted only routes matterNode ids
            ds.note_node_device_deleted(NEW_NODE, dev_id)

    _wrap_delete(coffee.indigo, after=indigo_calls_device_deleted)
    ds.replace_node(coffee.plan)

    assert not ds.node_tombstones.is_tombstoned(NEW_NODE)
    assert not ds._self_deleted_ids, "the self-delete mark must be consumed"
    assert ds.lookup(NEW_NODE, 0, "matterNode") == coffee.old_node.id


def test_delete_node_of_the_old_node_deletes_nothing_afterwards(ds, coffee):
    """S3: afterwards, delete_node(52) deletes nothing — the index no longer holds the rebound devices."""
    ds.replace_node(coffee.plan)
    asked = _wrap_delete(coffee.indigo)
    assert ds.delete_node(OLD_NODE) == []
    assert asked == []
    assert coffee.old_node.id in coffee.devices._by_id
    assert coffee.old_relay.id in coffee.devices._by_id
