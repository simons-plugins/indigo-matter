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
        plug, devices, plugin_mod):
    """Exported devices hoist to the top even when added to the DB last."""
    late = devices.add(RelayDevice(201, "Late Arrival"))
    plug.exports.upsert(ExportEntry(late.id, "onOffPlugInUnit"))
    ids = [key for key, _label in plug.getExportCandidates(valuesDict=_values())]
    assert ids.index("201") < ids.index("101")


def test_exported_and_other_groups_each_preserve_database_order(plug, devices):
    devices.add(RelayDevice(301, "Exported A"))
    devices.add(RelayDevice(302, "Plain A"))
    devices.add(RelayDevice(303, "Exported B"))
    devices.add(RelayDevice(304, "Plain B"))
    plug.exports.upsert(ExportEntry(301, "onOffPlugInUnit"))
    plug.exports.upsert(ExportEntry(303, "onOffPlugInUnit"))
    ids = [key for key, _label in plug.getExportCandidates(valuesDict=_values())]
    assert ids.index("301") < ids.index("303")   # exported group: DB order
    assert ids.index("302") < ids.index("304")   # other group: DB order


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
