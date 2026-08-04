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
"""
from __future__ import annotations

import importlib
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
    p.exports = ExportStore(p.pluginPrefs, p.logger)
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


def test_xac6_our_own_device_is_shown_as_excluded_not_hidden(plug):
    """The loop-guarded device stays visible so the user can see WHY."""
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert "105" not in labels                       # not selectable
    assert export_catalog.REASON_LOOP_GUARD in labels["x-105"]


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


def test_picker_filter_is_case_insensitive_substring(plug):
    labels = _labels(plug.getExportCandidates(valuesDict=_values(exportFilter="hAlL")))
    assert set(labels) == {"102", "103"}


def test_picker_filter_with_no_matches_still_returns_a_legal_option(plug):
    options = plug.getExportCandidates(valuesDict=_values(exportFilter="zzz"))
    assert options == [("0", "(no devices match the filter)")]


def test_picker_caps_the_list_and_offers_a_narrowing_tail(plug, devices, plugin_mod):
    for device_id in range(1000, 1000 + plugin_mod.EXPORT_PICKER_LIMIT + 5):
        devices.add(RelayDevice(device_id, f"Bulk {device_id}"))
    options = plug.getExportCandidates(valuesDict=_values())
    assert len(options) == plugin_mod.EXPORT_PICKER_LIMIT + 1
    tail_id, tail_label = options[-1]
    assert tail_id == "0"
    assert "narrow the filter" in tail_label


def test_picker_degrades_to_empty_on_error(plug, mock_indigo_base):
    boom = Mock()
    boom.__iter__ = Mock(side_effect=RuntimeError("boom"))
    mock_indigo_base.devices = boom
    assert plug.getExportCandidates(valuesDict=_values()) == []
    plug.logger.exception.assert_called()


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


def test_role_picker_degrades_to_empty_on_error(plug, monkeypatch):
    monkeypatch.setattr(export_catalog, "classify",
                        Mock(side_effect=RuntimeError("boom")))
    assert plug.getExportRoles(valuesDict=_values(exportDevice="101")) == []
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
    _values_out, errors = plug.exportAddOrUpdate(_values(), "manageMatterExports")
    assert "exportDevice" in errors
    assert len(plug.exports) == 0


def test_add_rejects_an_excluded_pick_with_a_field_error(plug):
    values, errors = plug.exportAddOrUpdate(
        _values(exportDevice="x-104", exportRole="onOffLight"), "manageMatterExports")
    assert export_catalog.REASON_SPRINKLER in errors["exportDevice"]
    assert export_catalog.REASON_SPRINKLER in values["exportStatus"]
    assert len(plug.exports) == 0


def test_xac6_add_refuses_our_own_device_even_if_the_id_is_unprefixed(plug):
    """Server-side rejection, not just a picker label — the guard is structural."""
    _out, errors = plug.exportAddOrUpdate(
        _values(exportDevice="105", exportRole="onOffLight"), "manageMatterExports")
    assert export_catalog.REASON_LOOP_GUARD in errors["exportDevice"]
    assert len(plug.exports) == 0


def test_add_rejects_a_role_the_device_is_not_eligible_for(plug):
    _out, errors = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="thermostat"), "manageMatterExports")
    assert "exportRole" in errors
    assert len(plug.exports) == 0


def test_add_rejects_an_empty_role(plug):
    _out, errors = plug.exportAddOrUpdate(_values(exportDevice="101"), "manageMatterExports")
    assert "exportRole" in errors


def test_add_rejects_a_stale_device_id(plug):
    _out, errors = plug.exportAddOrUpdate(
        _values(exportDevice="999999", exportRole="onOffLight"), "manageMatterExports")
    assert "no longer exists" in errors["exportDevice"]


def test_add_before_startup_is_a_dialog_error(plug):
    plug.exports = None
    _out, errors = plug.exportAddOrUpdate(
        _values(exportDevice="101", exportRole="onOffLight"), "manageMatterExports")
    assert "still starting" in errors["exportDevice"]


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
    _out, errors = plug.exportRemove(_values(), "manageMatterExports")
    assert "exportDevice" in errors


def test_remove_of_something_not_exported_is_a_field_error(plug):
    _out, errors = plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
    assert errors["exportDevice"] == "That device is not exported."


def test_remove_works_for_a_deleted_indigo_device(plug):
    plug.exports.upsert(ExportEntry(999999, "onOffLight"))
    values = plug.exportRemove(_values(exportDevice="999999"), "manageMatterExports")
    assert plug.exports.get(999999) is None
    assert "device 999999" in values["exportStatus"]


def test_remove_before_startup_is_a_dialog_error(plug):
    plug.exports = None
    _out, errors = plug.exportRemove(_values(exportDevice="101"), "manageMatterExports")
    assert "still starting" in errors["exportDevice"]


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
def test_startup_loads_the_allow_list_from_prefs(plugin_mod, monkeypatch):
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

    p = plugin_mod.Plugin.__new__(plugin_mod.Plugin)
    p.logger = Mock()
    p.pluginPrefs = {}
    p.proto = object()
    p.registry = object()
    p.device_sync = Mock()
    p.runtime = None
    p.server_process = None
    p.exports = None
    ExportStore(p.pluginPrefs, p.logger).upsert(ExportEntry(101, "onOffLight"))

    p.startup()
    assert p.exports is not None
    assert p.exports.ids() == frozenset({101})
