"""Issue #364 / S4 — ``NodeReplaceMenuMixin`` and its MenuItems.xml wiring.

Spec: docs/plans/issue-364-replace-node.md ("Tests > S4"). Each test's docstring
names the bullet it pins.

The mixin is composed on its own (the test_share_node_menu.py shape), over a REAL
``DeviceSync`` and the fake Indigo devices from the S3 coffee fixture, so what is
asserted is what actually happened to devices — not which mock was called.

Two conventions (workspace degradation-path rule):

* A refusal is only proven by "devices untouched", so every refusal test arms
  ``_arm_untouchable``: every device's ``replacePluginPropsOnServer`` and
  ``indigo.device.delete`` RECORD and RAISE. The test asserts the record is empty
  — recording matters because the mixin (and ``replace_node``) catch broadly, so a
  bare raise could be swallowed and the test would still pass.
* ``indigo.device.getDependencies`` returns the REAL shape (a dict with six list
  keys — see the vendored docs), not the "non-empty list" the spec's D6 describes.
  See the report: taken literally, D6 would mark every device referenced.
"""
from __future__ import annotations

import importlib
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from unittest.mock import Mock

import pytest

from indigo_fakes import FakeDev
from test_device_sync import ds, indigo_env  # noqa: F401  (fixtures)
from test_device_sync_replace import (  # noqa: F401
    NEW_NODE, OLD_NODE, OLD_NODE_NAME, OLD_RELAY_NAME, _build_coffee, _coffee_raw, _wrap_delete,
)

SERVER_PLUGIN = (Path(__file__).parent.parent / "indigo-matter.indigoPlugin"
                 / "Contents" / "Server Plugin")

NO_DEPENDENCIES = {"actionGroups": [], "controlPages": [], "devices": [],
                   "schedules": [], "triggers": [], "variables": []}
COFFEE_REFERENCED = {**NO_DEPENDENCIES, "actionGroups": [{"ID": 1280166770, "Name": "Coffee on"}]}

FABRIC_REMOVED = {"nodeId": "0x34", "removedIndigoDeviceIds": [], "fabricRemoved": True}
FABRIC_NOT_REMOVED = {"nodeId": "0x34", "removedIndigoDeviceIds": [], "fabricRemoved": False}


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def coffee(ds, indigo_env):  # noqa: F811
    indigo, _devices = indigo_env
    indigo.device.getDependencies = lambda _dev_id: NO_DEPENDENCIES
    return _build_coffee(ds, indigo_env)


@pytest.fixture
def menu(coffee, ds, mock_logger):  # noqa: F811
    sys.modules.pop("node_replace_menu_mixin", None)
    module = importlib.import_module("node_replace_menu_mixin")

    class _Composed(module.NodeReplaceMenuMixin):
        pass

    obj = _Composed()
    obj.logger = mock_logger
    obj.pluginPrefs = {}
    obj.device_sync = ds
    obj.matter = Mock()
    obj.runtime = Mock()
    obj.runtime.submit.return_value.result.return_value = None
    obj._resync = Mock(return_value="RESYNC_COROUTINE")
    obj._decommission_sync = Mock(return_value=FABRIC_REMOVED)
    # D11: exported ids come ONLY from Plugin's cached frozenset. No `exports` store is
    # provided, so an implementation that reads the store fails with AttributeError.
    obj._exported_ids = frozenset()
    mock_logger.warning.reset_mock()
    mock_logger.error.reset_mock()
    return obj


def _values(**overrides):
    values = {"replaceOld": str(OLD_NODE), "replaceNew": str(NEW_NODE), "replaceAnyway": False,
              "replaceDecommissionOld": False, "confirm": True}
    values.update(overrides)
    return values


def _snapshot(coffee):
    return {dev.id: (dev.name, dict(dev.pluginProps), dev.deviceTypeId)
            for dev in coffee.devices._by_id.values()}


def _arm_untouchable(coffee):
    """Every write path becomes record-and-raise; returns the record."""
    touched: list = []
    for dev in list(coffee.devices._by_id.values()):
        def forbidden_write(_props, dev=dev):
            touched.append(("replacePluginPropsOnServer", dev.id))
            raise AssertionError("props write on device %s" % dev.id)
        dev.replacePluginPropsOnServer = forbidden_write

    def forbidden_delete(dev_or_id):
        touched.append(("delete", getattr(dev_or_id, "id", dev_or_id)))
        raise AssertionError("delete of %s" % (dev_or_id,))
    coffee.indigo.device.delete = forbidden_delete
    return touched


def _error_text(result):
    assert len(result) == 3, "a refusal must be (False, valuesDict, errors), got %r" % (result,)
    ok, _values_dict, errors = result
    assert ok is False
    assert errors, "a refusal must put a field error on the dialog"
    return " ".join(str(v) for v in errors.values())


def _assert_refused_and_untouched(menu, coffee, result, snapshot, touched):
    _error_text(result)
    assert touched == [], "a refusal touched devices: %r" % (touched,)
    assert _snapshot(coffee) == snapshot
    menu._resync.assert_not_called()
    menu._decommission_sync.assert_not_called()
    menu.runtime.submit.assert_not_called()


def _add_differently_shaped_node(ds, indigo_env, node_id):  # noqa: F811
    """A node with a relay on ep1 and NO node device — differs from the coffee shape."""
    raw = _coffee_raw(node_id, "9.9.9")
    raw["attributes"].pop("0/40/65531")
    ds.create_from_raw(raw, "Other Plug")


# ---------------------------------------------------------------------------
# refusals leave everything untouched
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides", [
    {"replaceOld": ""},
    {"replaceNew": ""},
    {"replaceOld": "not-a-number"},
    {"replaceNew": "0x43"},
], ids=["no-old", "no-new", "old-not-an-integer", "new-not-an-integer"])
def test_missing_or_non_integer_selection_refuses_and_changes_nothing(menu, coffee, overrides):
    """S4: refusals (selections present and integer) leave every device untouched."""
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values(**overrides))
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def test_replacing_a_node_with_itself_refuses_and_changes_nothing(menu, coffee):
    """S4: refusal (same node) leaves every device untouched."""
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values(replaceNew=str(OLD_NODE)))
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def test_shape_mismatch_refuses_unless_replace_anyway_and_changes_nothing(menu, coffee, ds, indigo_env):  # noqa: F811
    """S4/D5: a mismatched node is selectable but refused on confirm without 'replace anyway'."""
    _add_differently_shaped_node(ds, indigo_env, 80)
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values(replaceNew="80"))
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def test_unticked_confirm_changes_nothing(menu, coffee):
    """S4: unticked confirm -> no change."""
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values(confirm=False))
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def test_exported_paired_new_device_refuses_with_the_migrate_message(menu, coffee):
    """S4 (D11): an exported paired new device (via _exported_ids) -> error mentions 'Migrate an exported accessory'; nothing changes."""
    menu._exported_ids = frozenset({coffee.new_relay.id})
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values())
    assert "Migrate an exported accessory" in _error_text(result)
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def test_referenced_paired_new_device_refuses_and_changes_nothing(menu, coffee):
    """S4: a paired new device that an automation references -> refused (D6); nothing changes."""
    coffee.indigo.device.getDependencies = (
        lambda dev_id: COFFEE_REFERENCED if dev_id == coffee.new_relay.id else NO_DEPENDENCIES)
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values())
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def _sibling_deps(coffee, extra_devices=()):
    """Real Indigo shape for a grouped device: its group siblings appear under
    ``devices`` (observed live 2026-10-17 on node 67's relay and node device)."""
    new_ids = {d.id for d in coffee.new_devs}

    def deps(dev_id):
        siblings = [{"ID": d.id, "Name": d.name} for d in coffee.new_devs if d.id != dev_id]
        out = {kind: [] for kind in NO_DEPENDENCIES}
        out["devices"] = siblings + list(extra_devices) if dev_id in new_ids else []
        return out
    return deps


def test_group_siblings_on_the_new_node_are_not_references(menu, coffee):
    """S4 (D6): a new device's own group siblings in getDependencies()['devices'] do not refuse."""
    coffee.indigo.device.getDependencies = _sibling_deps(coffee)
    result = menu.menuReplaceNode(_values())
    assert result[0] is True, _error_text(result)
    assert coffee.old_relay.pluginProps["nodeId"] == "67"


def test_a_device_outside_the_new_node_still_counts_as_a_reference(menu, coffee):
    """S4 (D6): only same-node siblings are ignored; any other device dependent still refuses."""
    coffee.indigo.device.getDependencies = _sibling_deps(
        coffee, extra_devices=[{"ID": 999999, "Name": "Some other device"}])
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values())
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


def test_get_dependencies_raising_refuses_and_changes_nothing(menu, coffee):
    """S4 (D6): getDependencies raises -> refused, never treated as 'no references'."""
    def broken(_dev_id):
        raise RuntimeError("dependency check failed")

    coffee.indigo.device.getDependencies = broken
    snapshot, touched = _snapshot(coffee), _arm_untouchable(coffee)
    result = menu.menuReplaceNode(_values())
    _assert_refused_and_untouched(menu, coffee, result, snapshot, touched)


# ---------------------------------------------------------------------------
# happy path and its degradations
# ---------------------------------------------------------------------------

def test_happy_path_rebinds_resyncs_once_and_returns_true(menu, coffee):
    """S4: happy path -> _resync submitted once and (True, valuesDict) returned."""
    values = _values()
    result = menu.menuReplaceNode(values)

    assert result[0] is True and result[1] is values and len(result) == 2
    assert coffee.old_node.pluginProps["nodeId"] == "67"
    assert coffee.old_relay.pluginProps["nodeId"] == "67"
    assert menu._resync.call_count == 1
    assert menu.runtime.submit.call_count == 1
    menu.runtime.submit.assert_called_once_with("RESYNC_COROUTINE")
    menu._decommission_sync.assert_not_called()


def test_a_failing_resync_still_returns_true_and_logs_the_next_reconnect(menu, coffee):
    """S4 (D10): resync failing -> still True, with an error logged that names the next reconnect."""
    menu.runtime.submit.return_value.result.side_effect = FuturesTimeoutError()
    result = menu.menuReplaceNode(_values())

    assert result[0] is True
    assert coffee.old_relay.pluginProps["nodeId"] == "67"
    logged = " ".join(str(call) for call in menu.logger.error.call_args_list)
    assert "reconnect" in logged


def test_a_partial_replacement_is_a_dialog_error_listing_what_was_skipped(menu, coffee):
    """Spec S4 (return rule, beyond the Tests list): partial result -> (False, ..., errors) naming the skipped device."""
    _wrap_delete(coffee.indigo, refuse={coffee.new_relay.id})
    result = menu.menuReplaceNode(_values())
    text = _error_text(result)
    assert OLD_RELAY_NAME in text
    assert coffee.old_node.pluginProps["nodeId"] == "67", "the pair that could be rebound still was"
    assert coffee.old_relay.pluginProps["nodeId"] == "52"


# ---------------------------------------------------------------------------
# optional decommission of the old node (D9)
# ---------------------------------------------------------------------------

def test_decommission_ticked_calls_it_once_and_deletes_no_indigo_device(menu, coffee):
    """S4 (D9): decommission ticked -> _decommission_sync(52) once; no Indigo device deleted by it."""
    asked = _wrap_delete(coffee.indigo)
    result = menu.menuReplaceNode(_values(replaceDecommissionOld=True))

    assert result[0] is True
    menu._decommission_sync.assert_called_once_with(OLD_NODE)
    assert set(asked) == {coffee.new_node.id, coffee.new_relay.id}, \
        "only the replaced duplicates may be deleted — never the rebound old devices"
    assert coffee.old_node.id in coffee.devices._by_id
    assert coffee.old_relay.id in coffee.devices._by_id


@pytest.mark.parametrize("outcome", [
    {"raises": RuntimeError("device no longer answers RemoveFabric")},
    {"returns": FABRIC_NOT_REMOVED},
], ids=["raises", "fabricRemoved-false"])
def test_decommission_failure_warns_and_still_returns_true(menu, coffee, outcome):
    """S4 (D9): decommission raising or returning fabricRemoved False -> warning, still True."""
    if "raises" in outcome:
        menu._decommission_sync.side_effect = outcome["raises"]
    else:
        menu._decommission_sync.return_value = outcome["returns"]
    menu.logger.warning.reset_mock()

    result = menu.menuReplaceNode(_values(replaceDecommissionOld=True))

    assert result[0] is True
    menu._decommission_sync.assert_called_once_with(OLD_NODE)
    assert menu.logger.warning.called
    assert coffee.old_relay.pluginProps["nodeId"] == "67"


def test_decommission_is_skipped_with_a_warning_while_an_old_device_remains(menu, coffee, ds):  # noqa: F811
    """S4 (D9): an unmatched old device still on 52 -> _decommission_sync NOT called, with a warning."""
    spare = FakeDev(coffee.devices.next_id(), "Coffee Machine Spare", "matterRelay",
                    {"nodeId": str(OLD_NODE), "endpointId": "2", "address": "0x34"})
    coffee.devices.add(spare)
    ds.rebuild_index()
    menu.logger.warning.reset_mock()

    result = menu.menuReplaceNode(_values(replaceAnyway=True, replaceDecommissionOld=True))

    assert result[0] is True
    menu._decommission_sync.assert_not_called()
    assert menu.logger.warning.called
    assert spare.pluginProps["nodeId"] == str(OLD_NODE)
    assert ds.lookup(OLD_NODE, 2, "matterRelay") == spare.id


# ---------------------------------------------------------------------------
# pickers / preview (spec S4 "Callbacks" + D4/D5; not itemised in the Tests list)
# ---------------------------------------------------------------------------

def _ids(options):
    return [value for value, _label in options]


def test_old_node_picker_lists_nodes_with_devices_unreachable_first(menu, coffee, ds, indigo_env):  # noqa: F811
    """Spec S4 getReplaceOldNodes: nodes that have devices, unreachable first."""
    coffee.old_node.states["reachable"] = False
    coffee.new_node.states["reachable"] = True
    options = menu.getReplaceOldNodes()
    assert _ids(options)[0] == str(OLD_NODE)
    assert set(_ids(options)) == {str(OLD_NODE), str(NEW_NODE)}


def test_new_node_picker_excludes_the_old_node_and_leads_with_matching_shapes(menu, coffee, ds, indigo_env):  # noqa: F811
    """Spec S4 getReplaceNewNodes / D5: excludes the selected old node; matches first, the rest '(shape differs)'."""
    _add_differently_shaped_node(ds, indigo_env, 60)   # lower id than 67, so natural order would lead with it
    options = menu.getReplaceNewNodes(valuesDict=_values())
    assert str(OLD_NODE) not in _ids(options)
    assert _ids(options) == [str(NEW_NODE), "60"]
    labels = dict(options)
    assert "(shape differs)" in labels["60"]
    assert "(shape differs)" not in labels[str(NEW_NODE)]


def test_replace_node_changed_is_a_noop_that_returns_the_values(menu):
    """Spec S4 replaceNodeChanged: a no-op that forces the reload."""
    values = _values()
    assert menu.replaceNodeChanged(values) is values


def test_preview_names_both_devices_and_marks_the_duplicate_deleted(menu, coffee):
    """Spec S4 getReplacePreview (D4): read-only rows built from the plan."""
    rows = menu.getReplacePreview(valuesDict=_values())
    text = " ".join(" ".join(row) for row in rows)
    assert OLD_NODE_NAME in text and OLD_RELAY_NAME in text
    assert "(deleted)" in text


@pytest.mark.parametrize("call", ["getReplaceOldNodes", "getReplaceNewNodes", "getReplacePreview"])
def test_list_callbacks_degrade_to_an_empty_list_and_log(menu, call):
    """Spec S4: list callbacks degrade to an empty list and log the exception (getMatterNodes pattern)."""
    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("device_sync is broken: %s" % name)

    menu.device_sync = Broken()
    menu.logger.exception.reset_mock()
    assert getattr(menu, call)(valuesDict=_values()) == []
    assert menu.logger.exception.called


# ---------------------------------------------------------------------------
# MenuItems.xml wiring (issue #146: callbacks must resolve on Plugin)
# ---------------------------------------------------------------------------

@pytest.fixture
def plugin_cls(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module.Plugin


def _menu_item(item_id):
    root = ET.parse(SERVER_PLUGIN / "MenuItems.xml").getroot()
    items = root.findall("MenuItem")
    index = next((i for i, item in enumerate(items) if item.get("id") == item_id), None)
    assert index is not None, "MenuItems.xml has no MenuItem id=%r" % item_id
    return items, index


def test_replace_menu_item_has_the_specified_fields_and_follows_decommission():
    """S4: MenuItem replaceMatterNode, after decommissionDevice, with exactly the specified fields."""
    items, index = _menu_item("replaceMatterNode")
    assert items[index - 1].get("id") == "decommissionDevice"
    item = items[index]
    assert item.findtext("CallbackMethod") == "menuReplaceNode"
    fields = {f.get("id"): f for f in item.find("ConfigUI").findall("Field")}
    assert set(fields) == {"replaceIntro", "replaceOld", "replaceNew", "replacePreview",
                           "replaceAnyway", "replaceDecommissionOld", "confirm"}
    assert fields["replaceIntro"].get("type") == "label"
    for menu_id in ("replaceOld", "replaceNew"):
        assert fields[menu_id].get("type") == "menu"
        assert fields[menu_id].findtext("CallbackMethod") == "replaceNodeChanged"
        assert fields[menu_id].find("List").get("dynamicReload") == "true"
    assert fields["replacePreview"].get("type") == "list"
    assert fields["replacePreview"].get("readonly") == "true"
    assert fields["replacePreview"].find("List").get("method") == "getReplacePreview"
    assert fields["replacePreview"].find("List").get("dynamicReload") == "true"
    for box in ("replaceAnyway", "replaceDecommissionOld", "confirm"):
        assert fields[box].get("type") == "checkbox"
    assert fields["replaceDecommissionOld"].get("defaultValue") == "false"


def test_every_callback_and_list_method_in_the_replace_menu_resolves_on_plugin(plugin_cls):
    """S4: every CallbackMethod and list `method` of the new MenuItem resolves on Plugin (the #146 trap)."""
    items, index = _menu_item("replaceMatterNode")
    item = items[index]
    names = {el.text.strip() for el in item.iter("CallbackMethod") if el.text}
    names |= {el.get("method") for el in item.iter("List") if el.get("class") == "self" and el.get("method")}
    assert {"menuReplaceNode", "replaceNodeChanged", "getReplaceOldNodes", "getReplaceNewNodes",
            "getReplacePreview"} <= names
    for name in names:
        assert hasattr(plugin_cls, name), "MenuItems.xml references missing method %s()" % name
    assert "NodeReplaceMenuMixin" in [cls.__name__ for cls in plugin_cls.__mro__]
