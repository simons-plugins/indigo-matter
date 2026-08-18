"""The "Manage Matter Exports…" dialog — pickers, callbacks and XML shape.

The dialog is the UI-D compromise (PRD-indigo-matter-export §5.1) forced by
what Indigo's XML dialogs can actually do: a multi-select `list` has no
CallbackMethod, and neither has a `textfield`, so master-detail is a filter
field + a reload button + a SINGLE-select `menu` whose callback loads the
picked device's saved settings. These tests pin the resulting contract,
including the two acceptance criteria the PRD names explicitly:

* **XAC6** — a device this plugin created is excluded (also unit-tested at the
  catalog level in `test_export_catalog.py`);
* **XAC9** — excluded devices appear in the picker *with reasons*, never
  silently missing.

Two contracts hardened in PR #122 are pinned here too: button `CallbackMethod`s
return **the values dict only** (the SDK documents a dict, not a
`(values, errors)` tuple — that shape belongs to validation methods), so every
refusal has to be visible in `exportStatus`; and one unreadable device costs one
picker row, never the whole list.
"""
from __future__ import annotations

import importlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

import pytest

import bridge_protocol
import export_catalog
from export_store import OPTION_INVERT, PREF_KEY, ExportEntry, ExportStore
from fakes import (
    OTHER_PLUGIN_ID,
    DimmerDevice,
    FakeIndigoDevices,
    HostileDevice,
    HostilePluginIdDevice,
    RelayDevice,
    SensorDevice,
    SprinklerDevice,
)

MENU_ITEMS_XML = (
    Path(__file__).parent.parent
    / "indigo-matter.indigoPlugin" / "Contents" / "Server Plugin" / "MenuItems.xml"
)
OURS = export_catalog.DEFAULT_PLUGIN_ID


@pytest.fixture
def plugin_mod(mock_indigo_base):
    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module


@pytest.fixture
def devices(mock_indigo_base):
    collection = FakeIndigoDevices([
        RelayDevice(101, "Study Plug"),
        DimmerDevice(102, "Hall Dimmer"),
        SensorDevice(103, "Hall Motion", supportsOnState=True),
        SprinklerDevice(104, "Irrigation"),
        RelayDevice(105, "Matter Plug", plugin_id=OURS),
    ])
    mock_indigo_base.devices = collection
    return collection


@pytest.fixture
def plug(plugin_mod, devices):  # noqa: ARG001 - devices installs indigo.devices
    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginId = OURS
    p.pluginPrefs = {}
    p.exports = ExportStore(lambda: p.pluginPrefs, p.logger)
    # Post-startup shape: the dialog callbacks nudge the export bridge and
    # refresh the deviceUpdated fast-path id set (E3b).
    p.export_bridge = None
    p._exported_ids = frozenset()
    p._subscribed_to_devices = False
    return p


def _values(**kwargs):
    base = {"exportFilter": "", "exportDevice": "0", "exportRole": "",
            "exportName": "", "exportInvert": False, "exportStatus": ""}
    base.update(kwargs)
    return base


def _labels(options):
    return {key: label for key, label in options}


# ---------------------------------------------------------------------------
# MenuItems.xml shape — the constraints the SDK actually imposes
# ---------------------------------------------------------------------------
def _menu_item():
    root = ET.parse(MENU_ITEMS_XML).getroot()
    for item in root.findall("MenuItem"):
        if item.get("id") == "manageMatterExports":
            return item
    raise AssertionError("manageMatterExports menu item missing")


def test_menu_item_exists_and_has_no_callback_method():
    """No CallbackMethod → a single Close button, so the buttons do the work."""
    item = _menu_item()
    assert item.findtext("Name").startswith("Manage Matter Exports")
    assert item.find("CallbackMethod") is None
    assert item.find("ConfigUI") is not None


def test_dialog_fields_are_present_with_the_expected_types():
    fields = {f.get("id"): f.get("type") for f in _menu_item().findall("./ConfigUI/Field")}
    assert fields["exportFilter"] == "textfield"
    assert fields["exportReload"] == "button"
    assert fields["exportDevice"] == "menu"       # menus have CallbackMethod; lists do not
    assert fields["exportRole"] == "menu"
    assert fields["exportName"] == "textfield"
    assert fields["exportInvert"] == "checkbox"
    assert fields["exportAdd"] == "button"
    assert fields["exportDelete"] == "button"
    assert fields["exportStatus"] == "textfield"  # readonly: labels cannot change at runtime
    assert fields["exportList"] == "list"


def test_dynamic_lists_point_at_real_plugin_methods(plug):
    for field in _menu_item().findall("./ConfigUI/Field"):
        list_el = field.find("List")
        if list_el is None or list_el.get("method") is None:
            continue
        assert list_el.get("class") == "self"
        assert list_el.get("dynamicReload") == "true"
        assert callable(getattr(plug, list_el.get("method")))


def test_button_and_menu_callbacks_exist_on_the_plugin(plug):
    for field in _menu_item().findall("./ConfigUI/Field"):
        callback = field.findtext("CallbackMethod")
        if callback:
            assert callable(getattr(plug, callback))


def test_conditional_field_is_counted_in_the_height_calculation():
    invert = [f for f in _menu_item().findall("./ConfigUI/Field")
              if f.get("id") == "exportInvert"][0]
    assert invert.get("visibleBindingId") == "exportRole"
    assert invert.get("visibleBindingValue") == "windowCovering"
    assert invert.get("alwaysUseInDialogHeightCalc") == "true"


def test_status_field_is_readonly():
    status = [f for f in _menu_item().findall("./ConfigUI/Field")
              if f.get("id") == "exportStatus"][0]
    assert status.get("readonly") == "true"


# ---------------------------------------------------------------------------
# Seeding (menu dialogs never remember their values)
# ---------------------------------------------------------------------------
def test_seed_values_only_for_the_export_menu(plug):
    assert plug.get_menu_action_config_ui_values("manageMatterExports")["exportDevice"] == "0"
    assert plug.get_menu_action_config_ui_values("decommissionDevice") == {}


def test_seed_reports_the_current_export_count(plug):
    assert "Nothing is exported" in plug.get_menu_action_config_ui_values(
        "manageMatterExports")["exportStatus"]
    plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
    assert "1 device(s) exported" in plug.get_menu_action_config_ui_values(
        "manageMatterExports")["exportStatus"]


# ---------------------------------------------------------------------------
# Device picker — XAC9 and the id contract
# ---------------------------------------------------------------------------
def test_picker_lists_every_device(plug):
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert labels["101"] == "Study Plug"
    assert labels["102"] == "Hall Dimmer"


def test_xac9_excluded_devices_are_listed_with_their_reason(plug):
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert "x-104" in labels
    assert labels["x-104"].startswith("Irrigation — not exportable: ")
    assert export_catalog.REASON_SPRINKLER in labels["x-104"]


def test_xac6_our_own_device_is_absent_from_the_picker(plug):
    """XAC6: loop-guarded devices are ABSENT, not listed-as-excluded.

    Every device this plugin created shadows a real device the user already
    sees in the picker, so a visible "not exportable" row would be pure noise.
    All OTHER exclusions stay visible with reasons (XAC9) — absence is reserved
    for the loop guard. Defense in depth is unchanged: classify() still returns
    Excluded for these ids, so the store validator and the exportDeviceChanged /
    exportAddOrUpdate callbacks reject a crafted pick anyway.
    """
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert "105" not in labels                       # not selectable
    assert "x-105" not in labels                     # and not even shown
    assert not any("105" in option_id for option_id in labels)


def test_picker_ids_are_never_empty_strings(plug):
    # Regression class: Indigo drops an option with an empty id
    # ("UI dynamic list function returned illegal ID string").
    assert all(key != "" for key, _label in plug.getExportCandidates(valuesDict=_values()))


def test_picker_ids_carry_no_commas_or_semicolons(plug):
    # Dynamic-list option values may not contain either (SDK dynamic-lists).
    for key, _label in plug.getExportCandidates(valuesDict=_values()):
        assert "," not in key and ";" not in key


def test_picker_marks_already_exported_devices(plug):
    plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert labels["101"] == "● Study Plug"
    assert labels["102"] == "Hall Dimmer"


def test_picker_filter_is_case_insensitive_substring(plug, plugin_mod):
    labels = _labels(plug.getExportCandidates(valuesDict=_values(exportFilter="hAlL")))
    assert set(labels) == {plugin_mod.NO_SELECTION_ID, "102", "103"}


def test_picker_always_leads_with_a_real_row_for_the_seeded_value(plug, plugin_mod):
    """The dialog is SEEDED with exportDevice="0", so "0" must be a real row.

    Without it the menu opens on a value that matches no option, which Indigo
    renders as a blank first item the user cannot get back to.
    """
    options = plug.getExportCandidates(valuesDict=_values())
    assert options[0] == (plugin_mod.NO_SELECTION_ID, plugin_mod.NO_SELECTION_LABEL)


def test_picker_option_ids_are_unique(plug, devices, plugin_mod):
    """"0" belongs to the seed row alone — the tail may not reuse it."""
    for device_id in range(1000, 1000 + plugin_mod.EXPORT_PICKER_LIMIT + 5):
        devices.add(RelayDevice(device_id, f"Bulk {device_id}"))
    ids = [key for key, _label in plug.getExportCandidates(valuesDict=_values())]
    assert len(ids) == len(set(ids))


def test_picker_filter_with_no_matches_still_returns_a_legal_option(plug, plugin_mod):
    options = plug.getExportCandidates(valuesDict=_values(exportFilter="zzz"))
    assert options == [(plugin_mod.NO_SELECTION_ID, plugin_mod.NO_SELECTION_LABEL),
                       plugin_mod.NO_MATCH_OPTION]


def test_picker_caps_the_list_and_offers_a_narrowing_tail(plug, devices, plugin_mod):
    for device_id in range(1000, 1000 + plugin_mod.EXPORT_PICKER_LIMIT + 5):
        devices.add(RelayDevice(device_id, f"Bulk {device_id}"))
    options = plug.getExportCandidates(valuesDict=_values())
    # seed row + the cap + the tail
    assert len(options) == plugin_mod.EXPORT_PICKER_LIMIT + 2
    assert options[-1] == plugin_mod.TRUNCATED_OPTION
    assert options[-1][0] != plugin_mod.NO_SELECTION_ID


def test_the_truncation_tail_is_not_a_selectable_device(plug, plugin_mod):
    assert plug._export_selection(
        {"exportDevice": plugin_mod.TRUNCATED_OPTION[0]}) == ("none", 0)
    assert plug._export_selection(
        {"exportDevice": plugin_mod.NO_MATCH_OPTION[0]}) == ("none", 0)


def test_picker_says_so_when_it_fails_outright(plug, mock_indigo_base, plugin_mod):
    boom = Mock()
    boom.__iter__ = Mock(side_effect=RuntimeError("boom"))
    mock_indigo_base.devices = boom
    assert plug.getExportCandidates(valuesDict=_values()) == [plugin_mod.LIST_ERROR_OPTION]
    plug.logger.exception.assert_called()


# ---------------------------------------------------------------------------
# D3 — one bad device costs one row, not the whole dialog
# ---------------------------------------------------------------------------
def test_one_unreadable_device_costs_one_row(plug, devices, plugin_mod):
    devices.add(HostileDevice())
    options = plug.getExportCandidates(valuesDict=_values())
    labels = _labels(options)
    assert labels["101"] == "Study Plug"          # the rest of the list survives
    broken = [label for label in labels.values() if plugin_mod.ROW_ERROR_LABEL in label]
    assert len(broken) == 1
    plug.logger.exception.assert_called()


def test_many_unreadable_devices_log_one_stack_trace(plug, devices):
    for _ in range(5):
        devices.add(HostileDevice())
    plug.getExportCandidates(valuesDict=_values())
    assert plug.logger.exception.call_count == 1
    assert plug.logger.error.call_count == 4


def test_an_unclassifiable_device_is_an_excluded_row_not_a_crash(plug, devices):
    """C1 end-to-end: classify() absorbs it, so the picker shows a reason."""
    devices.add(HostilePluginIdDevice(555, "Flaky Plug"))
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert "555" not in labels                     # not selectable
    assert "error reading device" in labels["x-555"]


def test_current_exports_contains_a_bad_row(plug, plugin_mod, monkeypatch):
    plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
    plug.exports.upsert(ExportEntry(102, "dimmableLight"))
    real = export_catalog.role_label

    def flaky(role):
        if role == "onOffPlugInUnit":
            raise RuntimeError("boom")
        return real(role)

    monkeypatch.setattr(export_catalog, "role_label", flaky)
    labels = _labels(plug.getCurrentExports())
    assert "102" in labels
    assert any(plugin_mod.ROW_ERROR_LABEL in label for label in labels.values())


def test_current_exports_says_so_when_it_fails_outright(plug, plugin_mod, monkeypatch):
    monkeypatch.setattr(plug.exports, "all", Mock(side_effect=RuntimeError("boom")))
    assert plug.getCurrentExports() == [plugin_mod.LIST_ERROR_OPTION]


# ---------------------------------------------------------------------------
# Issue #131 — exported devices sort to the top of the picker
# ---------------------------------------------------------------------------
def test_exported_devices_sort_above_non_exported_regardless_of_database_order(
        plug, devices):
    """Exported devices hoist to the top even when added to the DB last."""
    late = devices.add(RelayDevice(201, "Late Arrival"))
    plug.exports.upsert(ExportEntry(late.id, "onOffPlugInUnit"))
    ids = [key for key, _label in plug.getExportCandidates(valuesDict=_values())]
    assert ids.index("201") < ids.index("101")


def test_exported_and_other_groups_each_preserve_database_order(plug, devices):
    """Names are reverse-alphabetical so an alphabetise-both-groups mutant fails
    the intra-group assertions too, not just the (missing, until now) inter-group one."""
    devices.add(RelayDevice(301, "Zed Export"))
    devices.add(RelayDevice(302, "Zulu Plain"))
    devices.add(RelayDevice(303, "Alpha Export"))
    devices.add(RelayDevice(304, "Alpha Plain"))
    plug.exports.upsert(ExportEntry(301, "onOffPlugInUnit"))
    plug.exports.upsert(ExportEntry(303, "onOffPlugInUnit"))
    ids = [key for key, _label in plug.getExportCandidates(valuesDict=_values())]
    assert ids.index("301") < ids.index("303")   # exported group: DB order
    assert ids.index("302") < ids.index("304")   # other group: DB order
    for exported_id in ("301", "303"):
        for other_id in ("302", "304"):
            assert ids.index(exported_id) < ids.index(other_id)


def test_exported_device_past_the_cap_survives(plug, devices, plugin_mod):
    """An export is the one thing the picker lets you undo — it must never fall off."""
    for device_id in range(1000, 1000 + plugin_mod.EXPORT_PICKER_LIMIT + 5):
        devices.add(RelayDevice(device_id, f"Bulk {device_id}"))
    late = devices.add(RelayDevice(9999, "Late Export"))
    plug.exports.upsert(ExportEntry(late.id, "onOffPlugInUnit"))
    options = plug.getExportCandidates(valuesDict=_values())
    ids = [key for key, _label in options]
    assert ids[1] == "9999"                       # right after the seed row
    assert len(ids) == len(set(ids))               # id-uniqueness survives the split
    # seed + (the exported row + LIMIT-1 others) + the truncated tail
    assert len(options) == plugin_mod.EXPORT_PICKER_LIMIT + 2
    assert options[-1] == plugin_mod.TRUNCATED_OPTION


def test_excluded_and_exported_device_hoists_too(plug, devices):
    """D4: an excluded-but-exported device keeps its marker AND hoists (#131)."""
    plug.exports.upsert(ExportEntry(104, "onOffPlugInUnit"))
    options = plug.getExportCandidates(valuesDict=_values())
    ids = [key for key, _label in options]
    labels = _labels(options)
    assert ids[1] == "x-104"
    assert labels["x-104"].startswith("● ")
    assert ids.index("x-104") < ids.index("101")


def test_the_tail_is_honest_when_only_the_allowance_slice_truncates(plug, devices, plugin_mod):
    """The in-loop counter never fires here, so this pins the post-loop accounting alone."""
    non_exported_in_fixture = 4   # 101, 102, 103, x-104 — see test_picker_lists_every_device
    bulk = plugin_mod.EXPORT_PICKER_LIMIT - non_exported_in_fixture
    for device_id in range(2000, 2000 + bulk):
        devices.add(RelayDevice(device_id, f"Bulk {device_id}"))
    # other_rows now sits at exactly EXPORT_PICKER_LIMIT with zero in-loop truncation.
    late = devices.add(RelayDevice(9999, "Late Export"))
    plug.exports.upsert(ExportEntry(late.id, "onOffPlugInUnit"))
    options = plug.getExportCandidates(valuesDict=_values())
    assert options[-1] == plugin_mod.TRUNCATED_OPTION
    assert len(options) == plugin_mod.EXPORT_PICKER_LIMIT + 2


def test_exported_devices_are_still_filtered_before_they_can_hoist(plug, devices):
    devices.add(RelayDevice(401, "Kitchen Export"))    # exported, matches filter
    devices.add(RelayDevice(402, "Garage Export"))      # exported, filtered out
    devices.add(RelayDevice(403, "Kitchen Plain"))      # not exported, matches filter
    plug.exports.upsert(ExportEntry(401, "onOffPlugInUnit"))
    plug.exports.upsert(ExportEntry(402, "onOffPlugInUnit"))
    ids = [key for key, _label in
           plug.getExportCandidates(valuesDict=_values(exportFilter="kitchen"))]
    assert "401" in ids
    assert "403" in ids
    assert "402" not in ids                             # filtered out despite being exported
    assert ids.index("401") < ids.index("403")           # still hoisted above the plain match


def test_allowance_never_goes_negative_when_exports_exceed_the_cap(
        plug, devices, plugin_mod, monkeypatch):
    """Exported rows are uncapped by design; without the floor a shrunk limit
    would slice other_rows with a negative index and keep the wrong rows
    (a plain device that should have been dropped along with everything else)."""
    monkeypatch.setattr(plugin_mod.export_dialog_mixin, "EXPORT_PICKER_LIMIT", 3)
    for device_id in range(500, 504):
        devices.add(RelayDevice(device_id, f"Export {device_id}"))
        plug.exports.upsert(ExportEntry(device_id, "onOffPlugInUnit"))
    devices.add(RelayDevice(600, "Plain One"))
    devices.add(RelayDevice(601, "Plain Two"))
    options = plug.getExportCandidates(valuesDict=_values())
    ids = [key for key, _label in options]
    for device_id in range(500, 504):
        assert str(device_id) in ids
    # allowance is 0 (4 exports already exceed the limit of 3), so every plain
    # device — the two added here and the fixture's own 101/102/103 — is gone.
    for plain_id in ("101", "102", "103", "600", "601"):
        assert plain_id not in ids
    assert options[-1] == plugin_mod.TRUNCATED_OPTION
    assert len(options) == 1 + 4 + 1               # seed + exported rows + tail


def test_unreadable_device_error_rows_carry_distinct_ids(plug, devices, plugin_mod):
    for _ in range(3):
        devices.add(HostileDevice())
    options = plug.getExportCandidates(valuesDict=_values())
    # Not `_labels()`: it dict-collapses duplicate ids, which is the thing under test.
    error_rows = [(key, label) for key, label in options if plugin_mod.ROW_ERROR_LABEL in label]
    assert len(error_rows) == 3
    assert len({key for key, _label in error_rows}) == 3


def test_picker_works_before_the_store_exists(plug):
    plug.exports = None
    assert _labels(plug.getExportCandidates(valuesDict=_values()))["101"] == "Study Plug"


def test_picker_tolerates_a_missing_values_dict(plug):
    assert plug.getExportCandidates()


# ---------------------------------------------------------------------------
# Role picker
# ---------------------------------------------------------------------------
def test_role_picker_offers_the_catalog_roles_with_the_default_first(plug):
    options = plug.getExportRoles(valuesDict=_values(exportDevice="101"))
    assert [key for key, _ in options] == ["onOffPlugInUnit", "onOffLight", "doorLock"]
    assert options[0][1] == export_catalog.role_label("onOffPlugInUnit")


def test_role_picker_is_empty_without_a_selection(plug):
    assert plug.getExportRoles(valuesDict=_values()) == []


def test_role_picker_is_empty_for_an_excluded_pick(plug):
    assert plug.getExportRoles(valuesDict=_values(exportDevice="x-104")) == []


def test_role_picker_is_empty_for_a_stale_device_id(plug):
    assert plug.getExportRoles(valuesDict=_values(exportDevice="999999")) == []


def test_role_picker_says_so_when_it_fails_outright(plug, plugin_mod, monkeypatch):
    """An empty role menu means "nothing you may choose"; a failure must not
    borrow that meaning."""
    monkeypatch.setattr(export_catalog, "classify",
                        Mock(side_effect=RuntimeError("boom")))
    assert plug.getExportRoles(
        valuesDict=_values(exportDevice="101")) == [plugin_mod.LIST_ERROR_OPTION]
    plug.logger.exception.assert_called()


# ---------------------------------------------------------------------------
# Current-exports summary list
# ---------------------------------------------------------------------------
def test_current_exports_summarises_role_name_and_polarity(plug):
    plug.exports.upsert(ExportEntry(102, "windowCovering", name_override="Blind",
                                    options={OPTION_INVERT: True}))
    label = _labels(plug.getCurrentExports())["102"]
    assert "Hall Dimmer" in label
    assert export_catalog.role_label("windowCovering") in label
    assert 'shown as "Blind"' in label
    assert "inverted" in label


def test_current_exports_names_a_device_that_has_been_deleted(plug):
    plug.exports.upsert(ExportEntry(999999, "onOffLight"))
    assert "deleted device 999999" in _labels(plug.getCurrentExports())["999999"]


def test_current_exports_is_never_an_illegal_empty_option(plug):
    assert plug.getCurrentExports() == [("0", "(nothing exported yet)")]


def test_current_exports_before_startup(plug):
    plug.exports = None
    assert plug.getCurrentExports() == [("0", "(plugin still starting)")]


# ---------------------------------------------------------------------------
# Picker-changed callback
# ---------------------------------------------------------------------------
def test_device_changed_loads_the_saved_entry(plug):
    plug.exports.upsert(ExportEntry(102, "windowCovering", name_override="Blind",
                                    options={OPTION_INVERT: True}))
    values = plug.exportDeviceChanged(_values(exportDevice="102"), "manageMatterExports")
    assert values["exportRole"] == "windowCovering"
    assert values["exportName"] == "Blind"
    assert values["exportInvert"] is True
    assert "is exported as" in values["exportStatus"]


def test_device_changed_seeds_the_safe_default_for_a_new_export(plug):
    values = plug.exportDeviceChanged(_values(exportDevice="101"), "manageMatterExports")
    assert values["exportRole"] == "onOffPlugInUnit"
    assert values["exportName"] == ""
    assert values["exportInvert"] is False
    assert "not exported yet" in values["exportStatus"]


def test_device_changed_reports_an_excluded_pick_in_the_status_field(plug):
    values = plug.exportDeviceChanged(_values(exportDevice="x-104"), "manageMatterExports")
    assert values["exportRole"] == ""
    assert values["exportStatus"] == f"Not exportable: {export_catalog.REASON_SPRINKLER}"


def test_device_changed_reports_our_own_device_as_loop_guarded(plug):
    values = plug.exportDeviceChanged(_values(exportDevice="x-105"), "manageMatterExports")
    assert export_catalog.REASON_LOOP_GUARD in values["exportStatus"]


# ---------------------------------------------------------------------------
# D4 — excluded AND exported is a real state, and it has to be legible
# ---------------------------------------------------------------------------
def test_picker_keeps_the_exported_marker_on_an_excluded_device(plug):
    """A device that became unexportable AFTER it was exported keeps its dot.

    Dropping the marker makes the picker disagree with the "Currently exported"
    list, which reads as a picker bug rather than the stale export it is.
    """
    plug.exports.upsert(ExportEntry(104, "onOffPlugInUnit"))
    label = _labels(plug.getExportCandidates(valuesDict=_values()))["x-104"]
    assert label.startswith("● ")
    assert export_catalog.REASON_SPRINKLER in label


def test_device_changed_warns_when_an_excluded_device_is_still_exported(plug):
    plug.exports.upsert(ExportEntry(104, "onOffPlugInUnit"))
    values = plug.exportDeviceChanged(_values(exportDevice="x-104"), "manageMatterExports")
    assert "IS currently exported" in values["exportStatus"]
    assert "fail to bridge" in values["exportStatus"]


def test_device_changed_does_not_cry_wolf_for_an_unexported_exclusion(plug):
    values = plug.exportDeviceChanged(_values(exportDevice="x-104"), "manageMatterExports")
    assert "IS currently exported" not in values["exportStatus"]


def test_device_changed_clears_the_whole_pane_for_an_excluded_pick(plug, devices):
    """The second excluded branch used to leave a stale name/polarity behind."""
    devices.add(HostilePluginIdDevice(555, "Flaky Plug"))
    values = plug.exportDeviceChanged(
        _values(exportDevice="555", exportRole="onOffLight", exportName="Stale",
                exportInvert=True),
        "manageMatterExports")
    assert values["exportRole"] == ""
    assert values["exportName"] == ""
    assert values["exportInvert"] is False


def test_device_changed_clears_the_detail_pane_on_no_selection(plug):
    values = plug.exportDeviceChanged(
        _values(exportDevice="0", exportRole="doorLock", exportName="X"), "manageMatterExports")
    assert values["exportRole"] == ""
    assert values["exportName"] == ""


def test_device_changed_handles_a_deleted_device(plug):
    values = plug.exportDeviceChanged(_values(exportDevice="999999"), "manageMatterExports")
    assert "no longer exists" in values["exportStatus"]


# ---------------------------------------------------------------------------
# Add / update
# ---------------------------------------------------------------------------
def test_add_persists_the_entry_and_reports_it(plug):
    values = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
    assert plug.exports.get(101) == ExportEntry(101, "onOffLight", None, {})
    assert "Added Study Plug" in values["exportStatus"]
    plug.logger.info.assert_called()


def test_add_writes_through_to_prefs(plug):
    plug.exportAddOrUpdate(_values(exportDevice="101", exportRole="onOffLight"),
                           "manageMatterExports")
    assert "onOffLight" in plug.pluginPrefs[PREF_KEY]


def test_update_replaces_the_existing_entry(plug):
    plug.exports.upsert(ExportEntry(101, "onOffLight"))
    values = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="doorLock"), "manageMatterExports")
    assert plug.exports.get(101).role == "doorLock"
    assert len(plug.exports) == 1
    assert "Updated" in values["exportStatus"]


def test_add_stores_the_name_override_trimmed(plug):
    plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="onOffLight", exportName="  Desk Lamp  "),
        "manageMatterExports")
    assert plug.exports.get(101).name_override == "Desk Lamp"


def test_add_treats_a_blank_name_override_as_none(plug):
    plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="onOffLight", exportName="   "),
        "manageMatterExports")
    assert plug.exports.get(101).name_override is None


def test_add_records_covering_polarity_only_for_coverings(plug):
    plug.exportAddOrUpdate(
        _values(exportDevice="102", exportRole="windowCovering", exportInvert="true"),
        "manageMatterExports")
    assert plug.exports.get(102).options == {OPTION_INVERT: True}
    plug.exportAddOrUpdate(
        _values(exportDevice="102", exportRole="dimmableLight", exportInvert=True),
        "manageMatterExports")
    assert plug.exports.get(102).options == {}


def test_add_requires_a_selection(plug):
    values = plug.exportAddOrUpdate(_values(), "manageMatterExports")
    assert "Select a device to export" in values["exportStatus"]
    assert len(plug.exports) == 0


def test_add_rejects_an_excluded_pick_in_the_status_field(plug):
    values = plug.exportAddOrUpdate(
        _values(exportDevice="x-104", exportRole="onOffLight"), "manageMatterExports")
    assert export_catalog.REASON_SPRINKLER in values["exportStatus"]
    assert len(plug.exports) == 0


def test_xac6_add_refuses_our_own_device_even_if_the_id_is_unprefixed(plug):
    """Server-side rejection, not just a picker label — the guard is structural."""
    values = plug.exportAddOrUpdate(
        _values(exportDevice="105", exportRole="onOffLight"), "manageMatterExports")
    assert export_catalog.REASON_LOOP_GUARD in values["exportStatus"]
    assert len(plug.exports) == 0


def test_add_rejects_a_role_the_device_is_not_eligible_for(plug):
    values = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="thermostat"), "manageMatterExports")
    assert "Choose how this device should appear" in values["exportStatus"]
    assert len(plug.exports) == 0


def test_add_rejects_an_empty_role(plug):
    values = plug.exportAddOrUpdate(_values(exportDevice="101"), "manageMatterExports")
    assert "Choose how this device should appear" in values["exportStatus"]


def test_add_rejects_a_stale_device_id(plug):
    values = plug.exportAddOrUpdate(
        _values(exportDevice="999999", exportRole="onOffLight"), "manageMatterExports")
    assert "no longer exists" in values["exportStatus"]


def test_add_before_startup_says_so(plug):
    plug.exports = None
    values = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
    assert "still starting" in values["exportStatus"]


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------
def test_remove_drops_the_entry_and_clears_the_pane(plug):
    plug.exports.upsert(ExportEntry(101, "onOffLight"))
    values = plug.exportRemove(
        _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
    assert plug.exports.get(101) is None
    assert values["exportRole"] == ""
    assert "Removed Study Plug" in values["exportStatus"]


def test_remove_requires_a_selection(plug):
    values = plug.exportRemove(_values(), "manageMatterExports")
    assert "Select a device to remove" in values["exportStatus"]


def test_remove_of_something_not_exported_is_reported(plug):
    values = plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
    assert values["exportStatus"] == "That device is not exported."


def test_remove_works_for_a_deleted_indigo_device(plug):
    plug.exports.upsert(ExportEntry(999999, "onOffLight"))
    values = plug.exportRemove(_values(exportDevice="999999"), "manageMatterExports")
    assert plug.exports.get(999999) is None
    assert "device 999999" in values["exportStatus"]


def test_remove_before_startup_says_so(plug):
    plug.exports = None
    values = plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
    assert "still starting" in values["exportStatus"]


# ---------------------------------------------------------------------------
# D1 — button callbacks return the values dict ONLY
# ---------------------------------------------------------------------------
# The SDK's button reference documents a button CallbackMethod as returning "a
# dictionary back to the dialog containing any field changes"; the
# (valuesDict, errorsDict) tuple belongs to the VALIDATION methods. It also says
# a button's own field is read-only and so cannot carry an error message. Every
# refusal therefore has to reach the user through exportStatus.
BUTTON_CALLS = [
    ("exportReloadPicker", _values()),
    ("exportAddOrUpdate", _values()),                                   # no selection
    ("exportAddOrUpdate", _values(exportDevice="x-104")),               # excluded
    ("exportAddOrUpdate", _values(exportDevice="999999")),              # stale id
    ("exportAddOrUpdate", _values(exportDevice="101")),                 # no role
    ("exportAddOrUpdate", _values(exportDevice="101", exportRole="thermostat")),
    ("exportAddOrUpdate", _values(exportDevice="101", exportRole="onOffLight")),
    ("exportRemove", _values()),
    ("exportRemove", _values(exportDevice="101")),
]


@pytest.mark.parametrize("callback,values", BUTTON_CALLS,
                         ids=[f"{name}-{i}" for i, (name, _v) in enumerate(BUTTON_CALLS)])
def test_button_callbacks_return_a_bare_values_dict(plug, callback, values):
    result = getattr(plug, callback)(dict(values), "manageMatterExports")
    assert not isinstance(result, tuple), f"{callback} returned the undocumented tuple"
    assert "exportStatus" in result


@pytest.mark.parametrize("callback,values", BUTTON_CALLS,
                         ids=[f"{name}-{i}" for i, (name, _v) in enumerate(BUTTON_CALLS)])
def test_every_button_path_leaves_a_non_empty_status(plug, callback, values):
    """Including the early returns: a silent dialog is indistinguishable from
    a dead button."""
    result = getattr(plug, callback)(dict(values), "manageMatterExports")
    assert str(result["exportStatus"]).strip()


@pytest.mark.parametrize("callback", ["exportAddOrUpdate", "exportRemove"])
def test_button_callbacks_before_startup_still_return_a_dict(plug, callback):
    plug.exports = None
    result = getattr(plug, callback)(
        _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
    assert not isinstance(result, tuple)


# ---------------------------------------------------------------------------
# S6 — a failed save is never reported as success
# ---------------------------------------------------------------------------
def test_add_reports_a_failed_save_instead_of_claiming_success(plug, monkeypatch):
    monkeypatch.setattr(plug.exports, "upsert", Mock(side_effect=RuntimeError("prefs died")))
    values = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
    assert values["exportStatus"] == "FAILED to save the export list — see Event Log"
    plug.logger.exception.assert_called()


def test_remove_reports_a_failed_save_instead_of_claiming_success(plug, monkeypatch):
    plug.exports.upsert(ExportEntry(101, "onOffLight"))
    monkeypatch.setattr(plug.exports, "remove", Mock(side_effect=RuntimeError("prefs died")))
    values = plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
    assert values["exportStatus"] == "FAILED to save the export list — see Event Log"
    plug.logger.exception.assert_called()


# ---------------------------------------------------------------------------
# S3 — a load failure must never render as "Nothing is exported yet."
# ---------------------------------------------------------------------------
def test_the_dialog_reports_an_unreadable_export_list(plug):
    plug.pluginPrefs = {PREF_KEY: "{broken"}
    plug.exports = ExportStore(lambda: plug.pluginPrefs, plug.logger)
    status = plug.get_menu_action_config_ui_values("manageMatterExports")["exportStatus"]
    assert "could not be read" in status
    assert "preserved" in status
    assert "Nothing is exported yet." not in status


def test_the_dialog_reports_dropped_rows_alongside_the_count(plug):
    blob = json.dumps({"v": 1, "exports": [
        {"indigoDeviceId": 101, "role": "onOffLight"},
        {"indigoDeviceId": 102, "role": "teleporter"},
    ]})
    plug.pluginPrefs = {PREF_KEY: blob}
    plug.exports = ExportStore(lambda: plug.pluginPrefs, plug.logger)
    status = plug._export_summary()
    assert "could not be read" in status
    assert "1 device(s) exported" in status


# ---------------------------------------------------------------------------
# Filter button
# ---------------------------------------------------------------------------
def test_reload_button_echoes_the_filter_in_the_status(plug):
    values = plug.exportReloadPicker(_values(exportFilter="hall"), "manageMatterExports")
    assert 'Filtered on "hall"' in values["exportStatus"]


def test_reload_button_with_no_filter_shows_the_summary(plug):
    plug.exports.upsert(ExportEntry(101, "onOffLight"))
    values = plug.exportReloadPicker(_values(), "manageMatterExports")
    assert values["exportStatus"] == "1 device(s) exported."


# ---------------------------------------------------------------------------
# Selection decoding + checkbox coercion
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("", ("none", 0)),
    ("0", ("none", 0)),
    ("junk", ("none", 0)),
    ("101", ("device", 101)),
    ("x-104", ("excluded", 104)),
])
def test_selection_decoding(plug, raw, expected):
    assert plug._export_selection({"exportDevice": raw}) == expected


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False), ("true", True), ("YES", True),
    ("1", True), ("false", False), ("", False), (None, False),
])
def test_checkbox_coercion(plug, raw, expected):
    assert plug._truthy(raw) is expected


def test_plugin_id_falls_back_to_the_bundle_constant(plug):
    del plug.pluginId
    assert plug._export_plugin_id() == export_catalog.DEFAULT_PLUGIN_ID


# ---------------------------------------------------------------------------
# startup wiring — the allow-list must exist before anything consults it
# ---------------------------------------------------------------------------
@pytest.fixture
def started(plugin_mod, devices, monkeypatch):  # noqa: ARG001 - devices installs indigo.devices
    """A Plugin whose startup() can be driven without touching the real host."""
    class FakeRuntimeObj:
        is_running = True

        def start(self):
            pass

        def submit(self, coro):
            if hasattr(coro, "close"):
                coro.close()
            return Mock()

    monkeypatch.setattr(plugin_mod, "MatterClient",
                        lambda *a, **k: Mock(run=lambda: None))
    monkeypatch.setattr(plugin_mod, "AsyncRuntime", lambda logger: FakeRuntimeObj())
    monkeypatch.setattr(plugin_mod, "CommissionJobs", lambda *a, **k: Mock())
    monkeypatch.setattr(plugin_mod, "HttpApi", lambda *a, **k: Mock())
    # MUST be patched — an unpatched ServerProcess touches the real $HOME (#104).
    monkeypatch.setattr(plugin_mod, "ServerProcess", lambda *a, **k: Mock())

    def build(prefs=None):
        p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
        p.logger = Mock()
        p.pluginId = OURS
        p._version = "2026.0.1"
        p._subscribed_to_devices = False
        p._exported_ids = frozenset()
        p.export_bridge = None
        p.pluginPrefs = {} if prefs is None else prefs
        p.proto = object()
        p.registry = object()
        p.device_sync = Mock()
        p.runtime = None
        p.server_process = None
        p.exports = None
        return p

    return build


def _seed(prefs, *entries):
    store = ExportStore(lambda: prefs, Mock())
    for entry in entries:
        store.upsert(entry)
    return prefs


def test_startup_loads_the_allow_list_from_prefs(started):
    p = started()
    _seed(p.pluginPrefs, ExportEntry(101, "onOffLight"))
    p.startup()
    assert p.exports is not None
    assert p.exports.ids() == frozenset({101})


def test_startup_wires_the_store_to_indigos_prefs_flush(started, mock_indigo_base):
    """S1 — the store's commit step must be a REAL flush, not the default no-op."""
    p = started()
    p.startup()
    mock_indigo_base.server.savePluginPrefs.reset_mock()
    p.exports.upsert(ExportEntry(101, "onOffLight"))
    mock_indigo_base.server.savePluginPrefs.assert_called_once()


def test_startup_wires_the_store_to_a_live_prefs_binding(started):
    """S2 — a PluginConfig save can hand the plugin a NEW pluginPrefs object."""
    p = started()
    p.startup()
    p.pluginPrefs = {}                      # Indigo rebinds it
    p.exports.upsert(ExportEntry(101, "onOffLight"))
    assert PREF_KEY in p.pluginPrefs        # the write followed the rebinding


def test_startup_drops_a_restored_entry_for_our_own_device(started):
    """S5 — load is an unguarded write path; the loop guard is re-run there.

    Device 105 carries OUR pluginId. A blob restored from a backup (or edited
    by hand) that exports it must not survive into the store, or E3 would build
    an endpoint for a device this plugin created.
    """
    prefs = _seed({}, ExportEntry(101, "onOffLight"), ExportEntry(105, "onOffLight"))
    p = started(prefs)
    p.startup()
    assert p.exports.ids() == frozenset({101})
    assert p.exports.load_error is not None


def test_startup_keeps_a_merely_unexportable_entry(started):
    """Only the loop guard deletes. Ordinary ineligibility is reported, not acted on."""
    prefs = _seed({}, ExportEntry(104, "onOffPlugInUnit"))   # sprinkler
    p = started(prefs)
    p.startup()
    assert p.exports.ids() == frozenset({104})


# ---------------------------------------------------------------------------
# R1 — startup reconcile is REPORT-ONLY
# ---------------------------------------------------------------------------
def test_reconcile_warns_about_a_deleted_device_but_keeps_it(started):
    prefs = _seed({}, ExportEntry(999999, "onOffLight"))
    p = started(prefs)
    p.startup()
    assert p.exports.ids() == frozenset({999999})
    assert any("no longer exists" in str(call)
               for call in p.logger.warning.call_args_list)


def test_reconcile_warns_about_a_now_unexportable_device(started):
    prefs = _seed({}, ExportEntry(104, "onOffPlugInUnit"))   # sprinkler
    p = started(prefs)
    p.startup()
    assert any("no longer exportable" in str(call)
               for call in p.logger.warning.call_args_list)


def test_reconcile_warns_about_a_role_the_device_no_longer_offers(started):
    prefs = _seed({}, ExportEntry(101, "windowCovering"))    # relay: no covering role
    p = started(prefs)
    p.startup()
    assert any("no longer offers" in str(call)
               for call in p.logger.warning.call_args_list)


def test_reconcile_is_silent_when_everything_is_fine(started):
    prefs = _seed({}, ExportEntry(101, "onOffPlugInUnit"))
    p = started(prefs)
    p.startup()
    assert p.logger.warning.call_args_list == []


def test_reconcile_never_fails_startup(started, monkeypatch):
    prefs = _seed({}, ExportEntry(101, "onOffPlugInUnit"))
    p = started(prefs)
    monkeypatch.setattr(export_catalog, "classify", Mock(side_effect=RuntimeError("boom")))
    p.startup()                              # must not raise
    assert p.exports.ids() == frozenset({101})


# ---------------------------------------------------------------------------
# The two recovery exits (BRIDGE_PROTOCOL §3.10/§3.11) — A2
# ---------------------------------------------------------------------------
def _menu_item_by_id(item_id: str):
    root = ET.parse(MENU_ITEMS_XML).getroot()
    for item in root.findall("MenuItem"):
        if item.get("id") == item_id:
            return item
    raise AssertionError(f"{item_id} menu item missing")


class TestRecoveryMenusExist:
    """⊗ `rebuild_endpoint_map` had NO caller anywhere in the plugin.

    Three user-facing strings told the user to confirm a rebuild "in the
    plugin" — the attach-refusal error, `TERMINAL_ATTACH_ERRORS`' remedy text
    and the export dialog's status line — and there was nothing to confirm. The
    only real way out of the refuse-to-start state was to hand-edit JSON in the
    storage directory.
    """

    def test_the_rebuild_menu_exists_and_is_wired_to_a_callback(self, plugin_mod):
        item = _menu_item_by_id("rebuildEndpointMap")
        assert item.findtext("CallbackMethod") == "menuRebuildEndpointMap"
        assert hasattr(plugin_mod.Plugin, "menuRebuildEndpointMap")

    def test_the_rebuild_menu_states_what_a_rebuild_does_and_demands_a_tick(self):
        """#132: the dialog used to warn the rebuild WILL duplicate accessories.

        It cannot — it renumbers nothing. The duplication belongs to the storage
        loss that already happened, and the dialog now distinguishes the two
        faults (damaged map file vs lost Matter storage) instead of promising
        the worst case for both.
        """
        item = _menu_item_by_id("rebuildEndpointMap")
        fields = {f.get("id"): f for f in item.find("ConfigUI").findall("Field")}
        assert fields["confirm"].get("type") == "checkbox"
        assert fields["confirm"].get("defaultValue") == "false"
        assert "cannot be undone" in fields["confirm"].findtext("Label")
        warning = fields["warning"].findtext("Label")
        assert "cannot itself duplicate accessories" in warning
        assert "renumbers NOTHING" in warning
        assert "Matter storage was lost" in warning
        assert "No pairing is touched" in warning

    def test_the_reset_menu_exists_and_is_wired_to_a_callback(self, plugin_mod):
        item = _menu_item_by_id("resetBridgePairings")
        assert item.findtext("CallbackMethod") == "menuResetBridgePairings"
        assert hasattr(plugin_mod.Plugin, "menuResetBridgePairings")

    def test_the_reset_menu_demands_TWO_ticks(self):
        """The only plugin action that destroys every pairing at once."""
        item = _menu_item_by_id("resetBridgePairings")
        fields = {f.get("id"): f for f in item.find("ConfigUI").findall("Field")}
        for name in ("confirm", "confirmAgain"):
            assert fields[name].get("type") == "checkbox"
            assert fields[name].get("defaultValue") == "false"


class _FakeRuntime:
    def __init__(self, result=None, error=None):
        self.result_value = result
        self.error = error
        self.submitted: list = []

    def submit(self, coro):
        self.submitted.append(coro)
        coro.close()
        return self

    def result(self, timeout=None):  # noqa: ARG002
        if self.error is not None:
            raise self.error
        return self.result_value


def _bridge_with(plug, **client_state):
    client = Mock(connected=True, **client_state)
    plug.export_bridge = Mock(client=client)
    return client


def _status(endpoint_count=2, warnings=()):
    import bridge_protocol
    return bridge_protocol.StatusReport(
        commissioned=True, fabrics=[], endpoint_count=endpoint_count,
        endpoints=[], drift=[], drift_checked=True, warnings=list(warnings))


class TestRebuildMenuCallback:
    def test_it_refuses_without_the_tick(self, plug):
        """⊗ The gate is asserted against THE CLIENT, with a client present.

        This test used to leave ``plug.export_bridge`` at ``None``, so deleting
        the confirm check entirely left it green: the connection gate one line
        further down filled the same ``errors["confirm"]`` key, and the
        assertion could not tell the two apart. With a live client the mutation
        is what it is in the field — the irreversible rebuild REALLY RUNS on an
        unticked box.
        """
        client = _bridge_with(plug, recovery=True)
        plug.runtime = _FakeRuntime(result=_status())
        ok, _values_out, errors = plug.menuRebuildEndpointMap({"confirm": False})
        assert ok is False and "confirm" in errors
        assert "cannot be undone" in errors["confirm"]
        client.rebuild_endpoint_map.assert_not_called()

    def test_it_refuses_when_there_is_no_bridge_connection(self, plug):
        plug.runtime = _FakeRuntime()
        plug.export_bridge = None
        ok, _values_out, errors = plug.menuRebuildEndpointMap({"confirm": True})
        assert ok is False and "confirm" in errors

    def test_it_calls_the_client_and_names_the_outcome(self, plug):
        client = _bridge_with(plug, recovery=True, attached=True)
        plug.runtime = _FakeRuntime(result=_status(endpoint_count=3))

        ok, _values_out = plug.menuRebuildEndpointMap({"confirm": True})

        assert ok is True
        client.rebuild_endpoint_map.assert_called_once()
        said = " ".join(str(c.args[0]) for c in plug.logger.warning.call_args_list)
        assert "REBUILT" in said and "Nothing was renumbered" in said

    def test_it_works_from_the_RECOVERY_state_where_no_attach_ever_happened(self, plug):
        """`connected`, not `attached`: §1.1 holds the socket open un-attached,
        and that is the only state in which a rebuild is ever needed."""
        client = _bridge_with(plug, attached=False, recovery=True)
        plug.runtime = _FakeRuntime(result=_status())
        ok, _values_out = plug.menuRebuildEndpointMap({"confirm": True})
        assert ok is True
        client.rebuild_endpoint_map.assert_called_once()

    def test_it_refuses_to_rebuild_a_node_that_is_NOT_refusing(self, plug):
        """⊗ M11: `connected` gates reaching the node, not rebuilding it.

        Run against a healthy bridge, §3.11 silently discards the retained
        endpoint-number allocation of every export that is not currently live —
        and §3.3 keeps exactly those so that re-adding a device restores its
        accessory rather than creating a second one.
        """
        client = _bridge_with(plug, recovery=False)
        plug.runtime = _FakeRuntime(result=_status())
        ok, _values_out, errors = plug.menuRebuildEndpointMap({"confirm": True})
        assert ok is False and "confirm" in errors
        client.rebuild_endpoint_map.assert_not_called()

    def test_a_rebuild_whose_re_attach_failed_is_still_a_rebuild(self, plug):
        """⊗ The two outcomes are reported separately.

        By the time the node answers, the map has been rewritten and the
        refusal is gone — the irreversible half is done. Reporting the pair as
        one told the user their node was "unchanged and still refusing" over a
        map that no longer existed, and invited them to repeat an operation
        that had already discarded the persisted baseline.
        """
        client = _bridge_with(plug, recovery=True, attached=False)
        plug.runtime = _FakeRuntime(result=_status(endpoint_count=0))

        ok, _values_out = plug.menuRebuildEndpointMap({"confirm": True})

        assert ok is True, "the rebuild happened; it is not a failure"
        said = " ".join(str(c.args[0]) for c in plug.logger.warning.call_args_list)
        assert "REBUILT" in said
        assert "does NOT need repeating" in said
        errors = " ".join(str(c.args[0]) for c in plug.logger.error.call_args_list)
        assert "still refusing" not in errors

    def test_the_menu_deadline_covers_the_rebuild_AND_its_re_attach(self, plug):
        """⊗ A flat 45s expired mid-re-attach on a large export list.

        The call waits on two deadlines in series — LONG_TIMEOUT for the
        rebuild, then a full `attach_timeout_for` for the re-attach — so a
        number chosen without reference to either reports a failure over a
        rebuild that already succeeded and cannot be undone.
        """
        import bridge_client
        plug.pluginPrefs = {}
        plug.exports = ExportStore(lambda: plug.pluginPrefs, plug.logger)
        for device_id in range(1, 91):
            plug.exports.upsert(ExportEntry(device_id, "onOffLight"))
        _bridge_with(plug, recovery=True, attached=True)
        runtime = _FakeRuntime(result=_status())
        plug.runtime = runtime
        seen: dict = {}

        def _result(timeout=None):
            seen["timeout"] = timeout
            return _status()

        runtime.result = _result

        plug.menuRebuildEndpointMap({"confirm": True})

        expected = bridge_client.rebuild_timeout_for(90)
        assert seen["timeout"] == expected
        assert seen["timeout"] > bridge_client.LONG_TIMEOUT + bridge_client.attach_timeout_for(90)

    def test_a_failed_rebuild_is_reported_as_a_failure_not_a_success(self, plug):
        """The node answers with an error when the new map could not be
        written, and the refusal is still in force. Reporting success there
        tells the user the rebuild they confirmed worked, and the next start
        refuses again for the same reason."""
        _bridge_with(plug)
        plug.runtime = _FakeRuntime(error=RuntimeError("map could not be written"))

        ok, _values_out, errors = plug.menuRebuildEndpointMap({"confirm": True})

        assert ok is False and "confirm" in errors
        said = " ".join(str(c.args[0]) for c in plug.logger.error.call_args_list)
        assert "FAILED" in said and "still refusing" in said

    def test_node_warnings_are_surfaced(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime(result=_status(warnings=["disk is full"]))
        plug.menuRebuildEndpointMap({"confirm": True})
        said = " ".join(str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0])
                        for c in plug.logger.warning.call_args_list)
        assert "disk is full" in said


class TestResetPairingsMenuCallback:
    """⊗ Every gate here is asserted against the CLIENT, with a client present.

    These tests used to run with ``plug.export_bridge`` at ``None``, which made
    all three confirm mutations survive: the connection check immediately after
    the gate fills the same ``errors`` key, so "an error was reported" was true
    either way. What it could not see is the only thing that matters — whether
    the destructive command reached the node — and with a live client it does:
    unticked, ``factory_reset`` must never be called.
    """

    def test_one_tick_is_not_enough(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime()
        ok, _values_out, errors = plug.menuResetBridgePairings(
            {"confirm": True, "confirmAgain": False})
        assert ok is False and "confirmAgain" in errors
        client.factory_reset.assert_not_called()

    def test_the_second_tick_alone_is_not_enough_either(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime()
        ok, _values_out, errors = plug.menuResetBridgePairings(
            {"confirm": False, "confirmAgain": True})
        assert ok is False and "confirm" in errors
        client.factory_reset.assert_not_called()

    def test_neither_tick_wipes_nothing(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime()
        ok, _values_out, _errors = plug.menuResetBridgePairings(
            {"confirm": False, "confirmAgain": False})
        assert ok is False
        client.factory_reset.assert_not_called()

    def test_both_ticks_reset_and_preserve_endpoint_numbers(self, plug):
        client = _bridge_with(plug)
        plug.runtime = _FakeRuntime()

        ok, _values_out = plug.menuResetBridgePairings(
            {"confirm": True, "confirmAgain": True})

        assert ok is True
        # preserve=True: a user re-pairing the same ecosystems must not also
        # lose accessory identity. The "the map is corrupt" path is the rebuild.
        client.factory_reset.assert_called_once_with(True)

    def test_a_failed_reset_says_pairings_are_unchanged(self, plug):
        _bridge_with(plug)
        plug.runtime = _FakeRuntime(error=RuntimeError("node is gone"))
        ok, _values_out, errors = plug.menuResetBridgePairings(
            {"confirm": True, "confirmAgain": True})
        assert ok is False and "confirmAgain" in errors
        said = " ".join(str(c.args[0]) for c in plug.logger.error.call_args_list)
        assert "Pairings are unchanged" in said


# ---------------------------------------------------------------------------
# Re-adopt a Matter accessory… (§4, issue #219)
# ---------------------------------------------------------------------------
def _orphan(unique_id="indigo-901", number=7, role="onOffPlugInUnit", label="Old Plug",
           orphaned_at=None, device_id=None):
    return bridge_protocol.OrphanRecord(
        unique_id=unique_id, number=number, role=role, label=label,
        orphaned_at=orphaned_at, device_id=device_id)


def _said(mock_calls) -> str:
    """Every %-formatted call.args[0] from a Mock's call_args_list, joined."""
    return " ".join(
        str(c.args[0]) % c.args[1:] if len(c.args) > 1 else str(c.args[0]) for c in mock_calls)


class TestReadoptMenuExists:
    def test_the_readopt_menu_exists_and_is_wired_to_a_callback(self, plugin_mod):
        item = _menu_item_by_id("readoptExport")
        assert item.findtext("Name") == "Re-adopt a Matter accessory…"
        assert item.findtext("CallbackMethod") == "menuReadoptExport"
        assert hasattr(plugin_mod.Plugin, "menuReadoptExport")

    def test_the_fields_have_the_expected_types(self, plug):
        item = _menu_item_by_id("readoptExport")
        fields = {f.get("id"): f for f in item.find("ConfigUI").findall("Field")}
        assert fields["readoptOrphan"].get("type") == "menu"       # needs a CallbackMethod
        assert fields["readoptDevice"].get("type") == "menu"
        assert fields["readoptConfirm"].get("type") == "checkbox"
        assert fields["readoptConfirm"].get("defaultValue") == "false"
        assert fields["readoptOrphan"].findtext("CallbackMethod") == "readoptOrphanChanged"
        list_el = fields["readoptOrphan"].find("List")
        assert list_el.get("class") == "self" and list_el.get("dynamicReload") == "true"
        assert callable(getattr(plug, list_el.get("method")))
        list_el = fields["readoptDevice"].find("List")
        assert list_el.get("class") == "self" and list_el.get("dynamicReload") == "true"
        assert callable(getattr(plug, list_el.get("method")))

    def test_it_is_between_the_rebuild_and_recreate_menu_items(self):
        """Placement is the point (#134's own convention): identity surgery
        belongs beside the other identity-surgery recovery exits."""
        ids = [item.get("id") for item in ET.parse(MENU_ITEMS_XML).getroot().findall("MenuItem")]
        assert ids.index("rebuildEndpointMap") < ids.index("readoptExport") \
            < ids.index("recreateNodeDevices")

    def test_seeding_matches_the_pickers_no_selection_row(self, plug, plugin_mod):
        values = plug.get_menu_action_config_ui_values("readoptExport")
        assert values["readoptOrphan"] == plugin_mod.NO_SELECTION_ID
        assert values["readoptDevice"] == plugin_mod.NO_SELECTION_ID
        assert values["readoptConfirm"] is False


class TestGetReadoptOrphans:
    """§4.2 — the orphan picker's contents."""

    def test_not_attached_degrades_to_an_explanatory_row(self, plug, plugin_mod):
        _bridge_with(plug, attached=False)
        plug.runtime = _FakeRuntime()
        options = plug.getReadoptOrphans(valuesDict={})
        assert options[0][0] == plugin_mod.NO_SELECTION_ID
        assert "not connected" in options[0][1]

    def test_no_bridge_at_all_degrades_the_same_way(self, plug, plugin_mod):
        plug.export_bridge = None
        options = plug.getReadoptOrphans(valuesDict={})
        assert options[0][0] == plugin_mod.NO_SELECTION_ID

    def test_empty_orphan_list_is_a_single_row(self, plug, plugin_mod):
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(result=[])
        options = plug.getReadoptOrphans(valuesDict={})
        assert options == [(plugin_mod.NO_SELECTION_ID,
                            "(no left-behind accessories — nothing to re-adopt)")]

    def test_a_failed_read_degrades_to_the_list_error_row(self, plug, plugin_mod):
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(error=RuntimeError("boom"))
        assert plug.getReadoptOrphans(valuesDict={}) == [plugin_mod.LIST_ERROR_OPTION]

    def test_a_full_entry_is_formatted_with_role_date_and_number(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", number=7, role="onOffPlugInUnit",
                         label="Kitchen Lamp", orphaned_at="2026-08-12T09:15:00Z")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptOrphans(valuesDict={})
        assert options == [("indigo-901",
                            "Kitchen Lamp — " + export_catalog.role_label("onOffPlugInUnit") +
                            " — un-exported 12 Aug 2026 (accessory #7)")]

    def test_a_date_unknown_entry_says_so(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-902", number=4, role="onOffLight",
                         label="Porch Light", orphaned_at=None)
        plug.runtime = _FakeRuntime(result=[orphan])
        label = plug.getReadoptOrphans(valuesDict={})[0][1]
        assert "un-exported (date unknown) (accessory #4)" in label

    def test_a_bare_orphan_is_listed_but_unmatchable(self, plug):
        """PR5 design E4 — a pre-2026.16.2 orphan with no role/label: shown so the user
        can see the number is spoken for, refused only at Execute time."""
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-903", number=9, role=None, label=None)
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptOrphans(valuesDict={})
        assert options == [("indigo-903",
                            "(accessory #9 — no role recorded, cannot be re-adopted)")]

    def test_newest_orphan_sorts_first_and_date_unknown_sorts_last(self, plug):
        older = _orphan(unique_id="indigo-1", number=1, orphaned_at="2026-01-01T00:00:00Z")
        newer = _orphan(unique_id="indigo-2", number=2, orphaned_at="2026-08-01T00:00:00Z")
        unknown = _orphan(unique_id="indigo-3", number=3, orphaned_at=None)
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(result=[older, unknown, newer])
        options = plug.getReadoptOrphans(valuesDict={})
        assert [key for key, _label in options] == ["indigo-2", "indigo-1", "indigo-3"]


class TestGetReadoptDevices:
    """§4.3 — the device picker: selectable rows for devices that can take the
    orphan's role, and an EXPLAINED row for every device that cannot (XAC9).
    """

    def test_empty_without_a_picked_orphan(self, plug):
        assert plug.getReadoptDevices(valuesDict={}) == []
        assert plug.getReadoptDevices(valuesDict={"readoptOrphan": "0"}) == []

    def test_before_startup_says_so_rather_than_showing_no_devices(self, plug, plugin_mod):
        """XAC9: an empty list reads as "no device can take this role", which
        is a different fact from "the export list has not loaded yet"."""
        plug.exports = None
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(result=[_orphan()])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": "indigo-901"})
        assert options == [(plugin_mod.NO_SELECTION_ID,
                            "(plugin still starting — re-open this dialog in a moment)")]

    def test_only_devices_offering_the_orphans_role_are_selectable(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert "101" in labels                          # RelayDevice offers onOffPlugInUnit
        assert "102" not in labels                       # DimmerDevice does not

    def test_a_role_ineligible_device_is_shown_with_its_reason_not_hidden(self, plug):
        """XAC9. The picker-level half of PR5 design E3 — refused, but VISIBLY: "I
        recreated the device, why is it not in the list?" is exactly the
        question an absence answers with nothing."""
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="dimmableLight")            # RelayDevice cannot take this role
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert "101" not in labels, "still unselectable"
        assert labels["x-101"] == ("Study Plug — cannot appear as a "
                                   + export_catalog.role_label("dimmableLight"))

    def test_an_unexportable_device_is_shown_with_the_classifiers_reason(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert labels["x-104"].startswith("Irrigation — not exportable: ")
        assert export_catalog.REASON_SPRINKLER in labels["x-104"]

    def test_xac6_our_own_device_stays_absent_here_too(self, plug):
        """The one exclusion that is ABSENT rather than explained: every
        loop-guarded device shadows one the user already sees."""
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert not any("105" in option_id for option_id in labels)

    def test_selectable_rows_come_first_and_are_never_truncated(self, plug, plugin_mod, devices):
        """Same rule as `getExportCandidates`, same reason: this dialog is the
        only place a re-adopt starts, so a house full of explained rows must
        not push the pickable one out of reach. There is no filter field to
        narrow with."""
        for extra in range(plugin_mod.EXPORT_PICKER_LIMIT + 5):
            devices.add(SprinklerDevice(900_000 + extra, f"Zone {extra}"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])

        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})

        assert options[0] == ("101", "Study Plug")
        assert options[-1] == plugin_mod.TRUNCATED_OPTION
        explained = [row for row in options[1:] if row != plugin_mod.TRUNCATED_OPTION]
        assert len(explained) == plugin_mod.EXPORT_PICKER_LIMIT
        assert all(key.startswith("x-") for key, _label in explained)

    def test_already_exported_device_is_marked(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert labels["101"] == "● Study Plug"

    def test_a_device_already_re_adopted_onto_a_different_orphan_names_which(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit", published_as="indigo-999"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert "101" not in labels, "still unselectable"
        assert labels["x-101"] == "● Study Plug — already re-adopted onto accessory indigo-999"

    def test_a_device_already_re_adopted_onto_THIS_orphan_is_kept(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit", published_as="indigo-901"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert "101" in labels

    def test_a_role_changed_device_stays_in_the_picker(self, plug):
        """Its own `indigo-<ownId>~2` (issue #240) claims nobody else's
        accessory, so PR5 design §4.3 rule 3 must not drop it — its E2 names
        "`indigo-<newId>`, or a generation of it" as the allowed
        already-exported case."""
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit", published_as="indigo-101~2"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert labels["101"] == "● Study Plug"

    def test_a_bare_role_orphan_yields_no_devices(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role=None, label=None)
        plug.runtime = _FakeRuntime(result=[orphan])
        assert plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}) == []


def test_readopt_orphan_changed_is_a_pure_passthrough(plug):
    values = {"readoptOrphan": "indigo-901"}
    assert plug.readoptOrphanChanged(values) is values


class TestMenuReadoptExportValidation:
    """PR5 design §4.4's seven-step order, one refusal at a time."""

    def test_step1_refuses_without_the_tick(self, plug):
        client = _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(result=[])
        ok, _values_out, errors = plug.menuReadoptExport({"readoptConfirm": False})
        assert ok is False and "readoptConfirm" in errors
        assert not client.list_orphans.called

    def test_step2_refuses_when_not_attached(self, plug):
        _bridge_with(plug, attached=False)
        plug.runtime = _FakeRuntime()
        ok, _values_out, errors = plug.menuReadoptExport({"readoptConfirm": True})
        assert ok is False and "readoptConfirm" in errors
        assert "Start it, let the plugin attach, then re-open this dialog" \
            in _said(plug.logger.warning.call_args_list)

    def test_step2_refuses_with_no_bridge_at_all(self, plug):
        plug.export_bridge = None
        ok, _values_out, errors = plug.menuReadoptExport({"readoptConfirm": True})
        assert ok is False and "readoptConfirm" in errors

    def test_refuses_without_an_orphan_selected(self, plug):
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(result=[])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": "0"})
        assert ok is False and "readoptOrphan" in errors

    def test_step3_e6_refuses_when_the_orphan_went_live_again(self, plug):
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(result=[])       # freshly-fetched: gone
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": "indigo-901", "readoptDevice": "101"})
        assert ok is False and "readoptOrphan" in errors
        assert "in use again" in errors["readoptOrphan"]
        assert "in use again" in _said(plug.logger.warning.call_args_list)

    def test_step4_e4_refuses_a_bare_orphan(self, plug):
        _bridge_with(plug, attached=True)
        bare = _orphan(unique_id="indigo-903", number=9, role=None, label=None)
        plug.runtime = _FakeRuntime(result=[bare])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": "indigo-903", "readoptDevice": "101"})
        assert ok is False and "readoptOrphan" in errors
        assert "older than 2026.16.2" in errors["readoptOrphan"]

    def test_refuses_an_unselectable_row_by_pointing_at_its_reason(self, plug):
        """The XAC9 rows are shown so the user can see WHY, and the label
        already says it — but the pick still has to be refused, and the
        "already re-adopted onto another orphan" one is not caught by any
        later step."""
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit", published_as="indigo-999"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "x-101"})
        assert ok is False and "readoptDevice" in errors
        assert "listed with the reason" in errors["readoptDevice"]
        assert plug.exports.get(101).published_as == "indigo-999", "nothing was changed"

    def test_refuses_without_a_device_selected(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "0"})
        assert ok is False and "readoptDevice" in errors

    def test_step5_refuses_a_deleted_device(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id,
             "readoptDevice": "999999"})
        assert ok is False and "readoptDevice" in errors
        assert "no longer exists" in errors["readoptDevice"]

    def test_step5_refuses_an_excluded_device(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id,
             "readoptDevice": "104"})  # SprinklerDevice — always excluded
        assert ok is False and "readoptDevice" in errors
        assert "cannot be exported" in errors["readoptDevice"]

    def test_step6_e3_refuses_cross_role(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="dimmableLight")            # RelayDevice cannot take this role
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert ok is False and "readoptDevice" in errors
        assert '"Study Plug" cannot appear as a' in errors["readoptDevice"]
        assert "becomes a new accessory" in errors["readoptDevice"]

    def test_step7_e12_refuses_an_identity_already_claimed(self, plug):
        plug.exports.upsert(ExportEntry(102, "dimmableLight", published_as="indigo-901"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert ok is False and "readoptOrphan" in errors
        assert "already claimed by Indigo device 102" in errors["readoptOrphan"]

    def test_a_failed_store_write_is_reported_not_claimed_as_success(self, plug, monkeypatch):
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        monkeypatch.setattr(plug.exports, "upsert",
                            Mock(side_effect=RuntimeError("disk full")))
        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert ok is False and "readoptDevice" in errors
        assert "FAILED" in errors["readoptDevice"]


class TestMenuReadoptExportSuccess:
    """PR5 design §4.4's closing paragraph and §4.5's confirmation log."""

    def test_writes_the_store_with_the_orphans_role_and_identity(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert ok is True
        entry = plug.exports.get(101)
        assert entry.role == "onOffPlugInUnit"
        assert entry.published_as == "indigo-901"

    def test_preserves_an_existing_name_override_and_options(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffLight", name_override="Front Door"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])
        plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert plug.exports.get(101).name_override == "Front Door"

    def test_nudges_the_bridge_with_role_changed_true(self, plug):
        """PR5 design §4.4's closing paragraph: the remove-then-add path, the same shape
        as a role change, even though the role itself did not move here."""
        client = _bridge_with(plug, attached=True)
        _fake_bridge = plug.export_bridge
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])
        plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        _fake_bridge.replace.assert_called_once_with(101)
        _fake_bridge.upsert.assert_not_called()
        assert client is _fake_bridge.client

    def test_confirmation_log_names_device_role_and_accessory_number(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", number=7, role="onOffPlugInUnit",
                         label="Kitchen Lamp", device_id=123456789)
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert ok is True
        said = _said(plug.logger.warning.call_args_list)
        assert 'RE-ADOPTED accessory "Kitchen Lamp"' in said
        assert export_catalog.role_label("onOffPlugInUnit") in said
        assert "accessory number 7" in said
        assert 'onto Indigo device "Study Plug" (id 101)' in said
        assert "left behind when device 123456789 stopped exporting it" in said
        assert "nothing is re-paired and nothing is renumbered" in said
        assert "previous accessory" not in said     # device 101 was never exported before

    def test_confirmation_log_omits_the_driving_device_when_unrecorded(self, plug):
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp",
                         device_id=None)
        plug.runtime = _FakeRuntime(result=[orphan])
        plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        said = _said(plug.logger.warning.call_args_list)
        assert "That accessory was left behind." in said
        assert "stopped exporting it" not in said

    def test_confirmation_log_names_the_devices_own_previous_accessory(self, plug):
        """The closing sentence: emitted only when the target device was
        already exported under a DIFFERENT identity than the one it is now
        inheriting — its own accessory has just been removed too."""
        client = _bridge_with(plug, attached=True)
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))   # default identity
        client.status = bridge_protocol.StatusReport(
            commissioned=True, fabrics=[], endpoint_count=1,
            endpoints=[bridge_protocol.EndpointSummary(
                indigo_device_id=101, endpoint_number=11, role="onOffPlugInUnit")],
            drift=[], drift_checked=True)
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        assert ok is True
        said = _said(plug.logger.warning.call_args_list)
        assert "Device 101's own previous accessory (number 11) has been removed " \
            "from your ecosystems." in said

    def test_confirmation_log_omits_the_number_when_status_has_none(self, plug):
        client = _bridge_with(plug, attached=True)
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        client.status = None
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])
        plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})
        said = _said(plug.logger.warning.call_args_list)
        assert "Device 101's own previous accessory has been removed from your ecosystems." \
            in said


class TestReadoptOutcomeTruthfulness:
    """The success WARNING is emitted BEFORE the fire-and-forget bridge work
    lands, so every way that work can fail has to reach the user."""

    def test_a_failed_nudge_is_not_reported_as_a_successful_re_adopt(self, plug):
        _bridge_with(plug, attached=True)
        plug.export_bridge.replace = Mock(side_effect=RuntimeError("socket died"))
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])

        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})

        assert ok is False and "readoptDevice" in errors
        assert "the bridge node was not told" in errors["readoptDevice"]
        # The store write DID land — the message says "Saved", and this is what
        # makes that true.
        assert plug.exports.get(101).published_as == "indigo-901"
        assert "RE-ADOPTED" not in _said(plug.logger.warning.call_args_list)
        said = _said(plug.logger.error.call_args_list)
        assert "export list WAS saved" in said and "catches up at the next" in said

    def test_a_reporting_failure_never_unwinds_a_committed_re_adopt(self, plug):
        """`_previous_accessory_number` reads a cached StatusReport purely to
        decorate the log. It runs AFTER the store write and the bridge nudge,
        so anything it raises would fail a re-adopt that already happened."""
        client = _bridge_with(plug, attached=True)
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))

        class _Exploding:
            @property
            def endpoints(self):
                raise ValueError("not a TypeError")

        client.status = _Exploding()
        orphan = _orphan(unique_id="indigo-901", role="onOffPlugInUnit", label="Kitchen Lamp")
        plug.runtime = _FakeRuntime(result=[orphan])

        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})

        assert ok is True
        assert "RE-ADOPTED" in _said(plug.logger.warning.call_args_list)

    def test_step7_refuses_an_identity_an_ORDINARY_export_already_publishes(self, plug):
        """`published_as is None` does not mean "claims nothing" — it means
        "publishes as indigo-<own id>". Missing that let a re-adopt write a
        SECOND entry publishing one identity, which the node refuses for the
        whole attach (`malformed_args`), taking every export offline."""
        plug.exports.upsert(ExportEntry(102, "dimmableLight"))   # publishes as indigo-102
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-102", role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])

        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "101"})

        assert ok is False and "readoptOrphan" in errors
        assert "already claimed by Indigo device 102" in errors["readoptOrphan"]
        assert plug.exports.get(101) is None

    def test_a_failed_orphan_re_read_does_not_claim_the_accessory_went_live(self, plug):
        """Step 3's refusal used to be "something re-exported it while this
        dialog was open" for every reason the list came back empty — including
        the node simply not answering, which is a story about the user's house
        that never happened."""
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(error=TimeoutError("node did not answer"))

        ok, _values_out, errors = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": "indigo-901", "readoptDevice": "101"})

        assert ok is False and "readoptOrphan" in errors
        assert "in use again" not in errors["readoptOrphan"]
        assert "Could not re-check" in errors["readoptOrphan"]
        assert "Nothing was changed" in errors["readoptOrphan"]
        assert "list_orphans" in _said(plug.logger.error.call_args_list)

    def test_a_failed_read_gives_the_device_picker_the_list_error_row(self, plug, plugin_mod):
        _bridge_with(plug, attached=True)
        plug.runtime = _FakeRuntime(error=TimeoutError("node did not answer"))
        assert plug.getReadoptDevices(valuesDict={"readoptOrphan": "indigo-901"}) \
            == [plugin_mod.LIST_ERROR_OPTION]
