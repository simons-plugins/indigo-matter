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
import fakes
from export_store import (OPTION_CT_LEARNED_MAX_MIREDS, OPTION_CT_LEARNED_MIN_MIREDS,
                          OPTION_CT_MAX_MIREDS, OPTION_CT_MIN_MIREDS, OPTION_INVERT, PREF_KEY,
                          ExportEntry, ExportStore)
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
            "exportName": "", "exportInvert": False, "exportStatus": "",
            "exportNeedsMapping": "false", "exportStateKey": "", "exportStateInvert": False,
            "exportCtWarmestK": "", "exportCtCoolestK": ""}
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
# Colour-temperature bounds fields (issue #293)
# ---------------------------------------------------------------------------
def _ct_field(field_id):
    fields = [f for f in _menu_item().findall("./ConfigUI/Field") if f.get("id") == field_id]
    assert fields, f"{field_id} field missing from manageMatterExports"
    return fields[0]


@pytest.mark.parametrize("field_id", ["exportCtWarmestK", "exportCtCoolestK"])
def test_ct_bounds_fields_are_textfields_visible_only_for_ct_roles(field_id):
    field = _ct_field(field_id)
    assert field.get("type") == "textfield"
    assert field.get("visibleBindingId") == "exportRole"
    xml_roles = set(field.get("visibleBindingValue").split(","))
    assert xml_roles == {"colorTemperatureLight", "extendedColorLight"}
    assert field.get("alwaysUseInDialogHeightCalc") == "true"


@pytest.mark.parametrize("field_id", ["exportCtWarmestK", "exportCtCoolestK"])
def test_ct_bounds_fields_have_no_callback(field_id):
    """Plain textfields, matching `exportFilter`/`exportName` — nothing needs
    to react while the user is typing a Kelvin value."""
    assert _ct_field(field_id).findtext("CallbackMethod") is None


def test_ct_bounds_fields_carry_the_reciprocal_kelvin_direction_in_their_labels():
    """Not just present — labelled the RIGHT way round: get warmest/coolest
    backwards in the XML and a user reading the dialog would seed an
    inverted range with no code able to catch the mistake."""
    assert "Warmest" in _ct_field("exportCtWarmestK").findtext("Label")
    assert "Coolest" in _ct_field("exportCtCoolestK").findtext("Label")


# ---------------------------------------------------------------------------
# Alexa leak/freeze/rain export warning (issue #278) — static XML binding,
# not a callback. Indigo evaluates visibleBindingId/visibleBindingValue
# itself, so there is no Python decision logic to unit-test; these tests pin
# the XML shape and keep it in lockstep with export_catalog's role constant,
# which is the thing that could silently drift and re-hide the warning.
# ---------------------------------------------------------------------------
def _alexa_warning_field():
    fields = [f for f in _menu_item().findall("./ConfigUI/Field")
              if f.get("id") == "exportAlexaLeakWarning"]
    assert fields, "exportAlexaLeakWarning field missing from manageMatterExports"
    return fields[0]


def test_alexa_leak_warning_is_a_conditional_label():
    field = _alexa_warning_field()
    assert field.get("type") == "label"
    assert field.get("visibleBindingId") == "exportRole"
    assert field.get("alwaysUseInDialogHeightCalc") == "true"
    assert field.findtext("Label")  # non-empty


def test_alexa_unsupported_warning_matches_roles():
    """The XML's role list must stay exactly the roles export_catalog names.

    A typo or a drift here is a SILENT failure: the dialog would render fine,
    the picker would still work, and the warning would simply never show for
    a role it was supposed to cover. Exact-set comparison (not substring)
    catches both a missing role and an extra one.
    """
    field = _alexa_warning_field()
    xml_roles = set(field.get("visibleBindingValue").split(","))
    assert xml_roles == set(export_catalog.ALEXA_UNSUPPORTED_ROLES)


def test_alexa_unsupported_roles_are_real_roles():
    """Adversarial: every role named must exist in the protocol's role enum.

    Guards against a hand-typed constant naming a role that was renamed or
    never existed — which would make the XML binding permanently inert
    (Indigo just never matches it) without raising anything, anywhere.
    """
    for role in export_catalog.ALEXA_UNSUPPORTED_ROLES:
        assert role in bridge_protocol.ROLES


@pytest.mark.parametrize("safe_role", [
    "contactSensor", "occupancySensor", "smokeAlarm", "coAlarm",
    "windowCovering", "onOffPlugInUnit", "not-a-real-role",
])
def test_alexa_warning_does_not_cover_unrelated_or_bogus_roles(safe_role):
    """Negative + adversarial: a role that should NOT warn, and one that does
    not exist at all, must both be absent from the XML's own token list.

    Token-exact against the raw comma-split list, not a substring check —
    this pins what we wrote into ``visibleBindingValue``, not what Indigo does
    with it at runtime. Indigo's own matching there IS a substring contains
    (SDK label reference: "the string comparison is a contains — so if any
    option contains the specified string it will match"), so a future role
    whose name embeds one of these three — e.g. a hypothetical
    ``rainSensorV2`` — WOULD inherit this warning. That is fail-safe here
    (over-warning), the opposite failure mode from the one this test suite
    guards against elsewhere (a role silently never warning), so it is not
    asserted against.
    """
    field = _alexa_warning_field()
    xml_roles = field.get("visibleBindingValue").split(",")
    assert safe_role not in xml_roles


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


def test_current_exports_shows_a_measured_ct_range_without_opening_the_device(plug, devices):
    """The whole point of the row note: "did the calibration take?" answered
    for every lamp at once, rather than by opening each in turn."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MIN_MIREDS: 200,
                                             OPTION_CT_LEARNED_MAX_MIREDS: 454}))
    label = _labels(plug.getCurrentExports())[str(device_id)]
    assert "2203-5000K" in label
    assert "measured" in label


def test_current_exports_says_nothing_for_the_generic_ct_range(plug, devices):
    """A note every un-calibrated lamp carries identically is one nobody can
    scan past — silence is what makes the calibrated rows visible."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight"))
    assert "K" not in _labels(plug.getCurrentExports())[str(device_id)].split("→")[1]


def test_current_exports_says_nothing_about_bounds_for_a_non_ct_role(plug, devices):
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "dimmableLight"))
    assert "K" not in _labels(plug.getCurrentExports())[str(device_id)].split("→")[1]


def test_current_exports_marks_a_seeded_range_without_calling_it_measured(plug, devices):
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_MIN_MIREDS: 200,
                                             OPTION_CT_MAX_MIREDS: 454}))
    label = _labels(plug.getCurrentExports())[str(device_id)]
    assert "2203-5000K" in label
    assert "measured" not in label


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
# Colour-temperature bounds — dialog round trip (issue #293)
# ---------------------------------------------------------------------------
def _ct_device(devices, device_id=106, name="CT Lamp", **kwargs):
    devices.add(DimmerDevice(device_id, name, supportsWhiteTemperature=True,
                             whiteTemperature=2700, **kwargs))
    return device_id


def test_device_changed_loads_the_saved_seed_as_kelvin_the_reciprocal_way(plug, devices):
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_MIN_MIREDS: 200,
                                             OPTION_CT_MAX_MIREDS: 400}))
    values = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    # 200 mireds (the COOLEST bound) is the HIGHER Kelvin (5000K); 400 mireds
    # (the WARMEST bound) is the LOWER Kelvin (2500K) — get this backwards
    # and a user reading their own saved seed would see it inverted.
    assert values["exportCtCoolestK"] == "5000"
    assert values["exportCtWarmestK"] == "2500"


def test_device_changed_seeds_blank_ct_fields_for_a_new_export(plug, devices):
    device_id = _ct_device(devices)
    values = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    assert values["exportCtWarmestK"] == ""
    assert values["exportCtCoolestK"] == ""


def test_device_changed_shows_a_learned_bound_in_the_kelvin_fields(plug, devices):
    """What six live devices needed and did not get: a calibrated lamp with no
    seed used to show two empty boxes, which reads as "the calibration did
    nothing" when the plugin had in fact learned the range."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    values = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    assert values["exportCtWarmestK"] == "2500"
    # The cool side has no evidence, so it shows the generic floor.
    assert values["exportCtCoolestK"] == "6535"


def test_device_changed_says_which_side_was_measured_and_which_was_typed(plug, devices):
    """The two numbers alone cannot answer it — 400 mireds looks identical
    whether the learner measured it or the user typed it."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400,
                                             OPTION_CT_MIN_MIREDS: 200,
                                             OPTION_CT_MAX_MIREDS: 450}))
    values = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    source = values["exportCtBoundsSource"]
    assert "2500K (measured from the device)" in source
    assert "5000K (as you set it)" in source


def test_device_changed_explains_the_generic_range_rather_than_pre_filling_it(plug, devices):
    """Empty is also how a seed is DELETED, so "nothing known" must not be
    dressed up as a declared 2000-6535K range."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight"))
    values = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    assert values["exportCtWarmestK"] == ""
    assert values["exportCtCoolestK"] == ""
    assert "No range set" in values["exportCtBoundsSource"]


def test_device_changed_never_shows_a_kelvin_the_fields_would_refuse(plug, devices):
    """153 mireds is 6536K — one Kelvin above what `_ct_seed_from_values`
    accepts. Shown raw, editing the OTHER field would bounce the save with a
    complaint about a number the plugin itself put on screen."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    values = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
                exportCtWarmestK="2700", exportCtCoolestK=values["exportCtCoolestK"],
                exportCtWarmestKShown=values["exportCtWarmestKShown"],
                exportCtCoolestKShown=values["exportCtCoolestKShown"]),
        "manageMatterExports")
    assert plug.exports.get(device_id).options[OPTION_CT_MIN_MIREDS] == 153


def test_device_changed_clears_ct_fields_for_an_excluded_pick(plug, devices):
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_MIN_MIREDS: 200,
                                             OPTION_CT_MAX_MIREDS: 400}))
    devices.add(HostilePluginIdDevice(556, "Flaky Plug"))
    values = plug.exportDeviceChanged(
        _values(exportDevice="556", exportCtWarmestK="2500", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert values["exportCtWarmestK"] == ""
    assert values["exportCtCoolestK"] == ""


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
# Colour-temperature bounds — Add/update (issue #293)
# ---------------------------------------------------------------------------
def test_add_stores_the_seed_from_kelvin_fields_the_reciprocal_way(plug, devices):
    device_id = _ct_device(devices)
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="2500", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {
        OPTION_CT_MIN_MIREDS: 200, OPTION_CT_MAX_MIREDS: 400}


def test_add_leaves_no_seed_when_both_kelvin_fields_are_blank(plug, devices):
    device_id = _ct_device(devices)
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight"),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {}


def test_add_rejects_an_incomplete_kelvin_pair(plug, devices):
    device_id = _ct_device(devices)
    values = plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="2500"),
        "manageMatterExports")
    assert "both" in values["exportStatus"].lower()
    assert device_id not in plug.exports


def test_add_rejects_kelvin_out_of_range(plug, devices):
    device_id = _ct_device(devices)
    values = plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="1000", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert "2000" in values["exportStatus"] and "6535" in values["exportStatus"]
    assert device_id not in plug.exports


def test_add_rejects_a_warmest_that_is_not_lower_than_the_coolest(plug, devices):
    """The reciprocal check: WARMEST must be the LOWER Kelvin value."""
    device_id = _ct_device(devices)
    values = plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="5000", exportCtCoolestK="3000"),
        "manageMatterExports")
    assert "warmest" in values["exportStatus"].lower()
    assert device_id not in plug.exports


def test_add_rejects_non_numeric_kelvin(plug, devices):
    device_id = _ct_device(devices)
    values = plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="warm", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert "whole numbers" in values["exportStatus"]
    assert device_id not in plug.exports


def test_add_records_ct_bounds_only_for_ct_roles(plug, devices):
    device_id = _ct_device(devices)
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="2500", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert OPTION_CT_MIN_MIREDS in plug.exports.get(device_id).options
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="dimmableLight",
               exportCtWarmestK="2500", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {}


def test_add_preserves_the_learned_bound_across_an_unrelated_update(plug, devices):
    """Issue #293's own regression trap: saving the dialog for ANY reason —
    here, just adding a name override — must not silently wipe the learner's
    own record, which is never a dialog field a user could re-enter."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportName="Renamed"),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {OPTION_CT_LEARNED_MAX_MIREDS: 400}
    assert plug.exports.get(device_id).name_override == "Renamed"


def _reopen(plug, device_id, **overrides):
    """The values a real save carries: whatever the dialog PUT on screen when
    the device was picked, plus whatever the user then changed.

    Tests that hand-build these two fields prove nothing about the round trip
    — it is the pre-filled pair that decides whether a save is an edit.
    """
    shown = plug.exportDeviceChanged(_values(exportDevice=str(device_id)), "manageMatterExports")
    values = _values(exportDevice=str(device_id), exportRole=shown["exportRole"],
                     exportCtWarmestK=shown["exportCtWarmestK"],
                     exportCtCoolestK=shown["exportCtCoolestK"],
                     exportCtWarmestKShown=shown["exportCtWarmestKShown"],
                     exportCtCoolestKShown=shown["exportCtCoolestKShown"])
    values.update(overrides)
    return values


def test_add_does_not_turn_a_measured_bound_into_a_seed(plug, devices):
    """The trap the pre-fill creates: the fields now SHOW the learner's own
    value, so saving the dialog for an unrelated reason (a rename) would copy
    evidence into a declaration — and a seed outlives the learner re-widening
    or forgetting the bound it was copied from."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(_reopen(plug, device_id, exportName="Renamed"),
                           "manageMatterExports")
    assert plug.exports.get(device_id).options == {OPTION_CT_LEARNED_MAX_MIREDS: 400}
    assert plug.exports.get(device_id).name_override == "Renamed"


def test_add_writes_a_seed_when_a_shown_value_is_actually_edited(plug, devices):
    """The other half: an edit IS a declaration, and must be stored as one."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(
        _reopen(plug, device_id, exportCtWarmestK="2200", exportCtCoolestK="5000"),
        "manageMatterExports")
    options = plug.exports.get(device_id).options
    assert options[OPTION_CT_MAX_MIREDS] == 455  # 2200K
    assert options[OPTION_CT_MIN_MIREDS] == 200
    assert options[OPTION_CT_LEARNED_MAX_MIREDS] == 400  # evidence survives the seed


def test_add_keeps_an_existing_seed_when_nothing_was_edited(plug, devices):
    """"Untouched" must not mean "deleted" either — the same two empty-vs-
    unchanged fields decide both."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_MIN_MIREDS: 200,
                                             OPTION_CT_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(_reopen(plug, device_id, exportName="Renamed"),
                           "manageMatterExports")
    assert plug.exports.get(device_id).options == {OPTION_CT_MIN_MIREDS: 200,
                                                   OPTION_CT_MAX_MIREDS: 400}


def test_clearing_both_shown_fields_still_deletes_the_seed(plug, devices):
    """Clearing is an edit, not an absence — the documented way to go back to
    the generic range."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_MIN_MIREDS: 200,
                                             OPTION_CT_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(
        _reopen(plug, device_id, exportCtWarmestK="", exportCtCoolestK=""),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {}


def test_add_preserves_the_learned_bound_alongside_a_new_seed(plug, devices):
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="2500", exportCtCoolestK="5000"),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {
        OPTION_CT_MIN_MIREDS: 200, OPTION_CT_MAX_MIREDS: 400,
        OPTION_CT_LEARNED_MAX_MIREDS: 400}


def test_add_carries_the_learned_bound_across_a_role_change_between_the_two_ct_roles(
        plug, devices):
    """The carry-forward comment's own claim: same physical bulb, same
    evidence, whichever of the two CT-bearing roles it is currently exported
    as — only a change AWAY from both CT roles drops it (the block that
    carries it forward never runs at all for a non-CT role)."""
    device_id = _ct_device(devices, supportsRGB=True)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MAX_MIREDS: 400}))
    plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="extendedColorLight"),
        "manageMatterExports")
    assert plug.exports.get(device_id).role == "extendedColorLight"
    assert plug.exports.get(device_id).options == {OPTION_CT_LEARNED_MAX_MIREDS: 400}


def test_add_carries_the_freshest_learned_bound_despite_a_race_with_the_top_of_handler_read(
        plug, devices):
    """Issue #294 review — the carry-forward must be built from a RE-READ of
    the store taken immediately before the write, not from the snapshot the
    handler read at the top: a learner adoption (a different thread) landing
    in between must survive the save, not be overwritten by the stale copy.
    """
    device_id = _ct_device(devices)
    stale_entry = ExportEntry(device_id, "colorTemperatureLight",
                              options={OPTION_CT_LEARNED_MAX_MIREDS: 400})
    fresh_entry = ExportEntry(device_id, "colorTemperatureLight",
                              options={OPTION_CT_LEARNED_MAX_MIREDS: 410})
    calls = {"n": 0}
    real_get = plug.exports.get

    def racy_get(dev_id):
        calls["n"] += 1
        # First read is the handler's own `previous` snapshot; every read
        # from here on (including the re-read this fix adds) sees what a
        # concurrent learner adoption landed in between.
        return stale_entry if calls["n"] == 1 else fresh_entry

    plug.exports.get = racy_get
    try:
        plug.exportAddOrUpdate(
            _values(exportDevice=str(device_id), exportRole="colorTemperatureLight"),
            "manageMatterExports")
    finally:
        plug.exports.get = real_get
    assert plug.exports.get(device_id).options == {OPTION_CT_LEARNED_MAX_MIREDS: 410}


def test_add_drops_a_stale_learned_bound_that_would_invert_the_effective_range(plug, devices):
    """Issue #294 review — the coherence guard: a stale learned bound the
    fresh seed now conflicts with must not survive the save silently
    producing an inverted/collapsed effective range. The user's fresh seed
    is the newer intent; the learner re-earns the evidence."""
    device_id = _ct_device(devices)
    plug.exports.upsert(ExportEntry(device_id, "colorTemperatureLight",
                                    options={OPTION_CT_LEARNED_MIN_MIREDS: 250}))
    # warm_k=5000 -> ctMaxMireds=200; cool_k=6250 -> ctMinMireds=160. The
    # stale learned min (250) would make the effective range 250-200 —
    # inverted — once combined with this fresh seed.
    values = plug.exportAddOrUpdate(
        _values(exportDevice=str(device_id), exportRole="colorTemperatureLight",
               exportCtWarmestK="5000", exportCtCoolestK="6250"),
        "manageMatterExports")
    assert plug.exports.get(device_id).options == {
        OPTION_CT_MIN_MIREDS: 160, OPTION_CT_MAX_MIREDS: 200}
    assert OPTION_CT_LEARNED_MIN_MIREDS not in plug.exports.get(device_id).options
    assert "learn" in values["exportStatus"].lower()


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

    def test_a_mappable_device_is_shown_with_its_reason_not_dropped(self, plug, devices):
        """Regression, live-reported 2026-08-18. ADR-0012 gave `classify` a third
        outcome; this row builder tested NEGATIVELY for `Excluded`, so a
        `MappableDevice` fell through to `verdict.eligible_roles` and raised —
        which the caller turns into a dropped row. Every Texecom zone (229
        devices on the reference database) vanished from BOTH device pickers
        with no reason shown, which is the exact failure XAC9 exists to stop.
        """
        devices.add(fakes.texecom_zone(301, "Gate Contact"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="contactSensor")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert "301" not in labels, "unselectable — inheriting never asks for a mapping"
        assert "x-301" in labels, "but PRESENT, with a reason"
        assert "needs a state mapping first" in labels["x-301"]

    def test_a_mappable_device_is_shown_in_the_migrate_picker_too(self, plug, devices):
        """Same row builder, other dialog — the bug hit both."""
        devices.add(fakes.texecom_zone(302, "Kitchen PIR"))
        _bridge_with(plug, attached=True)
        plug.exports.upsert(ExportEntry(103, "occupancySensor"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "103"}))
        assert "302" not in labels
        assert "needs a state mapping first" in labels["x-302"]

    def test_migrating_onto_a_mappable_device_is_refused_with_a_next_step(self, plug, devices):
        """The Execute-time half. Retargeting an entry onto an unmapped device
        would publish nothing — the accessory goes dark rather than moves."""
        devices.add(fakes.texecom_zone(303, "Gate Contact"))
        _bridge_with(plug, attached=True)
        plug.exports.upsert(ExportEntry(103, "occupancySensor"))
        errors = {}
        assert plug._migrate_pick_device(103, plug.exports.get(103),
                                         {"migrateDevice": "303"}, errors) is None
        assert "needs a state mapping" in errors["migrateDevice"]

    def test_an_already_mapped_device_is_a_selectable_target(self, plug, devices):
        """Reported live 2026-08-18: "Gate contact is already exported. I'm
        migrating to an existing export." A custom device that HAS been
        exported carries its mapping in its own entry — asking the classifier
        without it read a fully-configured zone back as one still needing a
        mapping, so it stayed unselectable with advice the user had already
        followed.
        """
        devices.add(fakes.texecom_zone(320, "Gate Contact"))
        plug.exports.upsert(ExportEntry(320, "contactSensor",
                                        options={"stateKey": "status", "stateInvert": True}))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="contactSensor")
        plug.runtime = _FakeRuntime(result=[orphan])
        labels = _labels(plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id}))
        assert "320" in labels, "selectable — it is already mapped"
        assert labels["320"] == "● Gate Contact"

    def test_rows_are_alphabetical_within_each_block(self, plug, devices):
        """Reported live 2026-08-18: "not fully alphabetical but also not
        published devices first either". Both pickers walked `indigo.devices`,
        whose order is Indigo's Finder-style collation ("Apple TV Power Socket"
        before "AP_78:8a:20:b3:cc:4f") — and concatenating two such runs reads
        as an alphabet that restarts halfway down. Sorted WITHIN each block, so
        eligible-first and its never-truncated guarantee are untouched.
        """
        devices.add(RelayDevice(310, "Zulu Plug"))
        devices.add(RelayDevice(311, "alpha Plug"))       # lower case leads: case-insensitive
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        selectable = [label for key, label in options if not key.startswith("x-")]
        explained = [label for key, label in options if key.startswith("x-")]
        assert selectable == sorted(selectable, key=str.lower)
        assert explained == sorted(explained, key=str.lower)
        # ...and every selectable row still precedes every explained one.
        keys = [key for key, _label in options]
        assert all(not k.startswith("x-") for k in keys[:len(selectable)])

    def test_the_exported_marker_does_not_file_a_device_under_that_symbol(self, plug, devices):
        """The sort key is the device NAME, not the row label — the label
        carries the ● marker and the trailing reason, so sorting on it would
        file every exported device together under "●"."""
        devices.add(RelayDevice(312, "Aardvark Plug"))
        plug.exports.upsert(ExportEntry(312, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        selectable = [label for key, label in options if not key.startswith("x-")]
        assert selectable[0] == "● Aardvark Plug", selectable

    def test_a_name_matching_device_leads_the_selectable_block(self, plug, devices):
        """Issue #247. The orphan's label is the same evidence the bridge
        node's own nudge fires on (`noteReadoptableMatch`), so the device the
        log already pointed at must not sit several screens down the alphabet.
        """
        devices.add(RelayDevice(313, "Aardvark Plug"))    # alphabetically first
        devices.add(RelayDevice(314, "Old Plug"))         # ...but THIS is the orphan's name
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit", label="Old Plug")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        selectable = [label for key, label in options if not key.startswith("x-")]
        assert selectable[0] == "Old Plug", selectable

    def test_an_exported_device_outranks_an_unexported_one(self, plug, devices):
        """The other half of #247: ● is the "deleted, recreated and re-exported
        before noticing" case #219 exists for."""
        devices.add(RelayDevice(315, "Aardvark Plug"))
        devices.add(RelayDevice(316, "Zulu Plug"))
        plug.exports.upsert(ExportEntry(316, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        selectable = [label for key, label in options if not key.startswith("x-")]
        assert selectable[0] == "● Zulu Plug", selectable

    def test_a_name_match_outranks_a_bare_export_and_both_beat_the_alphabet(self, plug, devices):
        """Full rank order: name-match+●, then name-match, then ●, then A→Z."""
        devices.add(RelayDevice(317, "Aardvark Plug"))            # neither
        devices.add(RelayDevice(318, "Zulu Plug"))                # exported only
        plug.exports.upsert(ExportEntry(318, "onOffPlugInUnit"))
        devices.add(RelayDevice(319, "Old Plug"))                 # name match only
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit", label="Old Plug")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        selectable = [label for key, label in options if not key.startswith("x-")]
        assert selectable[:3] == ["Old Plug", "● Zulu Plug", "Aardvark Plug"], selectable

    def test_a_labelless_orphan_matches_nothing_rather_than_everything(self, plug, devices):
        """A pre-PR5 orphan records no label. An absence of evidence must not
        read as evidence about every device.

        The nameless device is the whole test: without the `bool(label)` guard,
        `name == label` is True for it, and a device that matches NOTHING is
        promoted above one with real evidence. An all-named fixture cannot
        detect that — `"" == "Aardvark Plug"` is False either way.

        The comparison is against an EXPORTED device rather than the first row
        outright, because a blank name also sorts first alphabetically: leading
        the list is not on its own proof of a bogus name match. Outranking a ●
        device is, since only a name match can do that.
        """
        devices.add(RelayDevice(324, ""))
        devices.add(RelayDevice(325, "Zulu Plug"))
        plug.exports.upsert(ExportEntry(325, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit", label=None)
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        keys = [key for key, _label in options if not key.startswith("x-")]
        assert keys[0] == "325", "the nameless device outranked a ● on an absent label"

    def test_relevance_never_promotes_an_explained_row_above_a_selectable_one(self, plug, devices):
        """#247 REORDERS the selectable block; it must not disturb the
        eligible-first rule. An ineligible device sharing the orphan's name is
        the sharpest case — the strongest name evidence on an unpickable row.
        """
        devices.add(DimmerDevice(322, "Old Plug"))        # cannot take onOffPlugInUnit
        devices.add(RelayDevice(323, "Zulu Plug"))
        _bridge_with(plug, attached=True)
        orphan = _orphan(role="onOffPlugInUnit", label="Old Plug")
        plug.runtime = _FakeRuntime(result=[orphan])
        options = plug.getReadoptDevices(valuesDict={"readoptOrphan": orphan.unique_id})
        keys = [key for key, _label in options]
        assert keys.index("323") < keys.index("x-322")

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
        assert "returns under its original identity and number" in said
        assert "no duplicate is created" in said
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

    def test_cross_role_readopt_drops_the_old_roles_ct_bounds(self, plug, devices):
        """Issue #293 review: `export_catalog._dimmer` makes a dimmer eligible
        for both a CT light role and `windowCovering`. Carrying the device's
        previous colorTemperatureLight export's learned CT bounds onto a
        windowCovering identity would hand `upsert`'s options validation a
        pair `windowCovering` cannot lawfully carry — the re-adopt must
        succeed anyway, with those keys dropped rather than the whole save
        failing."""
        devices.add(DimmerDevice(106, "Garage Dimmer", supportsWhiteTemperature=True))
        plug.exports.upsert(ExportEntry(
            106, "colorTemperatureLight",
            options={OPTION_CT_LEARNED_MIN_MIREDS: 153, OPTION_CT_LEARNED_MAX_MIREDS: 370}))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="windowCovering", label="Old Blind")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "106"})
        assert ok is True
        entry = plug.exports.get(106)
        assert entry.role == "windowCovering"
        assert entry.options == {}
        # PR #295 review: the dropped keys are not silent — a covering's
        # `invert` (or here, a light's learned CT bounds) vanishing on
        # re-adopt is user-invisible until the accessory misbehaves, so the
        # confirmation WARNING names what was dropped and why.
        said = _said(plug.logger.warning.call_args_list)
        assert OPTION_CT_LEARNED_MIN_MIREDS in said and OPTION_CT_LEARNED_MAX_MIREDS in said
        assert "do not apply to a" in said and "were not carried over" in said

    def test_cross_role_readopt_drops_the_old_roles_invert(self, plug, devices):
        """Mirrored case: a windowCovering's `invert` carried onto a light
        role it means nothing on."""
        devices.add(DimmerDevice(106, "Garage Dimmer", supportsWhiteTemperature=True))
        plug.exports.upsert(ExportEntry(106, "windowCovering", options={OPTION_INVERT: True}))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="colorTemperatureLight", label="Old Light")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "106"})
        assert ok is True
        entry = plug.exports.get(106)
        assert entry.role == "colorTemperatureLight"
        assert entry.options == {}
        said = _said(plug.logger.warning.call_args_list)
        assert OPTION_INVERT in said
        assert "do not apply to a" in said and "were not carried over" in said

    def test_same_role_readopt_still_carries_options_unchanged(self, plug, devices):
        """The common case the cross-role guard above must not disturb: a
        re-adopt onto the SAME role keeps every option verbatim."""
        devices.add(fakes.texecom_zone(320, "Gate Contact"))
        plug.exports.upsert(ExportEntry(
            320, "contactSensor", options={"stateKey": "status", "stateInvert": True}))
        _bridge_with(plug, attached=True)
        orphan = _orphan(unique_id="indigo-901", role="contactSensor", label="Old Contact")
        plug.runtime = _FakeRuntime(result=[orphan])
        ok, _values_out = plug.menuReadoptExport(
            {"readoptConfirm": True, "readoptOrphan": orphan.unique_id, "readoptDevice": "320"})
        assert ok is True
        entry = plug.exports.get(320)
        assert entry.options == {"stateKey": "status", "stateInvert": True}
        # Nothing was dropped on a same-role re-adopt, so no dropped-options
        # clause should appear in the confirmation WARNING.
        said = _said(plug.logger.warning.call_args_list)
        assert "were not carried over" not in said


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


# ---------------------------------------------------------------------------
# Migrate an exported accessory… (#246 design §3, issue #246)
# ---------------------------------------------------------------------------
def _endpoint_status(device_id, number, role):
    """A minimal ``StatusReport`` naming one live endpoint — enough for
    ``_previous_accessory_number`` to answer, without a bridge round trip."""
    return bridge_protocol.StatusReport(
        commissioned=True, fabrics=[], endpoint_count=1,
        endpoints=[bridge_protocol.EndpointSummary(
            indigo_device_id=device_id, endpoint_number=number, role=role)],
        drift=[], drift_checked=True)


class TestMigrateMenuExists:
    def test_the_migrate_menu_exists_and_is_wired_to_a_callback(self, plugin_mod):
        item = _menu_item_by_id("migrateExport")
        assert item.findtext("Name") == "Migrate an exported accessory…"
        assert item.findtext("CallbackMethod") == "menuMigrateExport"
        assert hasattr(plugin_mod.Plugin, "menuMigrateExport")

    def test_the_fields_have_the_expected_types(self, plug):
        item = _menu_item_by_id("migrateExport")
        fields = {f.get("id"): f for f in item.find("ConfigUI").findall("Field")}
        assert fields["migrateSource"].get("type") == "menu"     # needs a CallbackMethod
        assert fields["migrateDevice"].get("type") == "menu"
        assert fields["migrateConfirm"].get("type") == "checkbox"
        assert fields["migrateConfirm"].get("defaultValue") == "false"
        assert fields["migrateSource"].findtext("CallbackMethod") == "migrateSourceChanged"
        list_el = fields["migrateSource"].find("List")
        assert list_el.get("class") == "self" and list_el.get("dynamicReload") == "true"
        assert callable(getattr(plug, list_el.get("method")))
        list_el = fields["migrateDevice"].find("List")
        assert list_el.get("class") == "self" and list_el.get("dynamicReload") == "true"
        assert callable(getattr(plug, list_el.get("method")))

    def test_it_is_immediately_before_the_readopt_menu_item(self):
        """The design-note pairing (#246): migrate is the live-source path,
        placed right before re-adopt, its recovery-fallback twin."""
        ids = [item.get("id") for item in ET.parse(MENU_ITEMS_XML).getroot().findall("MenuItem")]
        assert ids.index("migrateExport") + 1 == ids.index("readoptExport")

    def test_seeding_matches_the_pickers_no_selection_row(self, plug, plugin_mod):
        values = plug.get_menu_action_config_ui_values("migrateExport")
        assert values["migrateSource"] == plugin_mod.NO_SELECTION_ID
        assert values["migrateDevice"] == plugin_mod.NO_SELECTION_ID
        assert values["migrateConfirm"] is False


class TestGetMigrateSources:
    """§3.2 — the source picker's contents. Local store truth: no bridge
    round trip, unlike the re-adopt orphan picker."""

    def test_before_startup_says_so(self, plug, plugin_mod):
        plug.exports = None
        options = plug.getMigrateSources(valuesDict={})
        assert options == [(plugin_mod.NO_SELECTION_ID,
                            "(plugin still starting — re-open this dialog in a moment)")]

    def test_nothing_exported_is_a_single_row(self, plug, plugin_mod):
        options = plug.getMigrateSources(valuesDict={})
        assert options == [(plugin_mod.NO_SELECTION_ID,
                            "(nothing is currently exported — nothing to migrate)")]

    def test_a_full_entry_is_formatted_with_role_and_identity(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        options = plug.getMigrateSources(valuesDict={})
        assert options == [("101", "Study Plug — "
                            + export_catalog.role_label("onOffPlugInUnit")
                            + " — accessory indigo-101")]

    def test_a_custom_published_identity_is_shown_verbatim(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit", published_as="indigo-101~2"))
        label = plug.getMigrateSources(valuesDict={})[0][1]
        assert "accessory indigo-101~2" in label

    def test_the_accessory_number_is_appended_when_the_client_knows_it(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        client = _bridge_with(plug, attached=True)
        client.status = _endpoint_status(101, 9, "onOffPlugInUnit")
        label = plug.getMigrateSources(valuesDict={})[0][1]
        assert label.endswith("(accessory #9)")

    def test_the_number_is_omitted_when_unknown(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        options = plug.getMigrateSources(valuesDict={})
        assert "#" not in options[0][1]

    def test_a_deleted_source_device_is_still_a_legitimate_source(self, plug):
        """The allow-list is never auto-pruned when a device disappears — so
        an entry for a gone device is a real migrate source, not stale junk."""
        plug.exports.upsert(ExportEntry(987654, "onOffPlugInUnit"))
        options = plug.getMigrateSources(valuesDict={})
        assert options == [("987654", "(device 987654 no longer exists) — "
                            + export_catalog.role_label("onOffPlugInUnit")
                            + " — accessory indigo-987654")]

    def test_multiple_entries_each_get_their_own_row(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        plug.exports.upsert(ExportEntry(102, "dimmableLight"))
        options = plug.getMigrateSources(valuesDict={})
        assert [key for key, _label in options] == ["101", "102"]

    def test_it_says_so_when_it_fails_outright(self, plug, plugin_mod, monkeypatch):
        """Issue #246 review finding 4 — the same outer try/except discipline
        every sibling picker (``getExportCandidates``, ``getMigrateDevices``,
        ``getReadoptOrphans``) already has, so a broken store degrades to one
        error row rather than taking the whole dialog down."""
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        monkeypatch.setattr(plug.exports, "all", Mock(side_effect=RuntimeError("boom")))
        assert plug.getMigrateSources(valuesDict={}) == [plugin_mod.LIST_ERROR_OPTION]
        plug.logger.exception.assert_called()


class TestGetMigrateDevices:
    """§3.2 — the target picker: reuses `_readopt_device_row`'s per-device
    rule verbatim, keyed off the SOURCE ENTRY's role, plus one migrate-only
    rule: the source device itself is absent."""

    def test_empty_without_a_picked_source(self, plug):
        assert plug.getMigrateDevices(valuesDict={}) == []
        assert plug.getMigrateDevices(valuesDict={"migrateSource": "0"}) == []

    def test_before_startup_says_so_rather_than_showing_no_devices(self, plug, plugin_mod):
        plug.exports = None
        options = plug.getMigrateDevices(valuesDict={"migrateSource": "101"})
        assert options == [(plugin_mod.NO_SELECTION_ID,
                            "(plugin still starting — re-open this dialog in a moment)")]

    def test_a_vanished_source_yields_no_devices(self, plug):
        assert plug.getMigrateDevices(valuesDict={"migrateSource": "999999"}) == []

    def test_only_devices_offering_the_sources_role_are_selectable(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "101"}))
        assert "104" not in labels and "x-104" in labels  # SprinklerDevice never exports

    def test_a_role_ineligible_device_is_shown_with_its_reason_not_hidden(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(106, "onOffPlugInUnit"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "106"}))
        assert "102" not in labels, "still unselectable"
        assert labels["x-102"] == ("Hall Dimmer — cannot appear as a "
                                   + export_catalog.role_label("onOffPlugInUnit"))

    def test_an_unexportable_target_is_shown_with_the_classifiers_reason(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "101"}))
        assert labels["x-104"].startswith("Irrigation — not exportable: ")

    def test_xac6_our_own_device_stays_absent(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "101"}))
        assert not any("105" in option_id for option_id in labels)

    def test_the_source_device_itself_is_absent_not_explained(self, plug, devices):
        """Absent, not merely excluded-with-a-reason: it is the row directly
        above in the dialog, never a target for itself."""
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(106, "onOffPlugInUnit"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "106"}))
        assert "106" not in labels and "x-106" not in labels

    def test_already_exported_device_is_marked(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        plug.exports.upsert(ExportEntry(106, "onOffPlugInUnit"))
        labels = _labels(plug.getMigrateDevices(valuesDict={"migrateSource": "101"}))
        assert labels["106"] == "● Garage Plug"


def test_migrate_source_changed_is_a_pure_passthrough(plug):
    values = {"migrateSource": "101"}
    assert plug.migrateSourceChanged(values) is values


class TestMenuMigrateExportValidation:
    """#246 design §3.2's eight-step order, one refusal at a time."""

    def test_step1_refuses_without_the_tick(self, plug):
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport({"migrateConfirm": False})
        assert ok is False and "migrateConfirm" in errors

    def test_step2_refuses_when_not_attached(self, plug):
        _bridge_with(plug, attached=False)
        ok, _values_out, errors = plug.menuMigrateExport({"migrateConfirm": True})
        assert ok is False and "migrateConfirm" in errors
        assert "Start it, let the plugin attach, then re-open this dialog" \
            in _said(plug.logger.warning.call_args_list)

    def test_step2_refuses_with_no_bridge_at_all(self, plug):
        plug.export_bridge = None
        ok, _values_out, errors = plug.menuMigrateExport({"migrateConfirm": True})
        assert ok is False and "migrateConfirm" in errors

    def test_refuses_without_a_source_selected(self, plug):
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "0"})
        assert ok is False and "migrateSource" in errors

    def test_step3_refuses_a_source_gone_from_the_store_at_execute_time(self, plug):
        """Re-read at Execute time, never trusted from the picker's own read
        (#246 design §3.2 step 3) — something un-exported it mid-dialog."""
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "102"})
        assert ok is False and "migrateSource" in errors
        assert "no longer exported" in errors["migrateSource"]
        assert plug.exports.get(102) is None, "nothing was changed"

    def test_refuses_without_a_device_selected(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "0"})
        assert ok is False and "migrateDevice" in errors

    def test_step4_refuses_a_deleted_target_device(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "999999"})
        assert ok is False and "migrateDevice" in errors
        assert "no longer exists" in errors["migrateDevice"]

    def test_step4_refuses_an_unexportable_target(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "104"})
        assert ok is False and "migrateDevice" in errors
        assert "cannot be exported" in errors["migrateDevice"]

    def test_step4_refuses_cross_role(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "102"})
        assert ok is False and "migrateDevice" in errors
        assert '"Hall Dimmer" cannot appear as a' in errors["migrateDevice"]
        assert "becomes a new accessory" in errors["migrateDevice"]

    def test_step5_refuses_the_target_being_the_source(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "101"})
        assert ok is False and "migrateDevice" in errors
        assert "already exporting this accessory" in errors["migrateDevice"]
        assert plug.exports.get(101).role == "onOffPlugInUnit", "nothing was changed"

    def test_refuses_an_unselectable_row_by_pointing_at_its_reason(self, plug):
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "x-104"})
        assert ok is False and "migrateDevice" in errors
        assert "listed with the reason" in errors["migrateDevice"]

    def test_a_failed_store_write_is_reported_not_claimed_as_success(self, plug, monkeypatch, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        monkeypatch.setattr(plug.exports, "replace_all",
                            Mock(side_effect=RuntimeError("disk full")))
        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})
        assert ok is False and "migrateDevice" in errors
        assert "FAILED" in errors["migrateDevice"]


class TestMenuMigrateExportSuccess:
    """#246 design §3.2's closing paragraph — the atomic commit, the nudge,
    and the confirmation WARNING."""

    def test_the_identity_moves_from_source_to_target_in_one_atomic_write(
            self, plug, monkeypatch, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)
        upsert_spy = Mock(wraps=plug.exports.upsert)
        monkeypatch.setattr(plug.exports, "upsert", upsert_spy)
        replace_all_spy = Mock(wraps=plug.exports.replace_all)
        monkeypatch.setattr(plug.exports, "replace_all", replace_all_spy)

        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert ok is True
        upsert_spy.assert_not_called(), "the commit must be ONE replace_all, never two upserts"
        replace_all_spy.assert_called_once()
        assert plug.exports.get(101) is None, "the source entry leaves the list"
        target = plug.exports.get(106)
        assert target.role == "onOffPlugInUnit"
        assert target.published_as == "indigo-101"
        # No entry may ever claim an identity another entry also claims —
        # checked against the one list actually written, not an intermediate
        # state nothing outside `replace_all` can observe anyway.
        (written_entries,) = replace_all_spy.call_args.args
        identities = [e.published_as or f"indigo-{e.indigo_device_id}" for e in written_entries]
        assert len(identities) == len(set(identities))

    def test_name_override_and_options_are_carried_from_the_source(self, plug, devices):
        # A DimmerDevice (not RelayDevice), and "windowCovering" (not
        # "onOffPlugInUnit"): `invert` is only a lawful option on a role in
        # `INVERTIBLE_ROLES` (`export_store._validate_options`) — the store
        # now enforces that on `upsert`/`replace_all` too (issue #294), not
        # just on load, so the source entry itself has to be a role that
        # legitimately carries `invert`.
        devices.add(DimmerDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "windowCovering", name_override="Front Plug",
                                        options={"invert": True}))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)
        plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})
        target = plug.exports.get(106)
        assert target.name_override == "Front Plug"
        assert target.options == {"invert": True}

    def test_a_pre_existing_target_entry_is_discarded_with_its_accessory(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit", name_override="Front Plug"))
        plug.exports.upsert(ExportEntry(106, "onOffPlugInUnit", name_override="Garage Own Name"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)
        plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})
        target = plug.exports.get(106)
        assert target.name_override == "Front Plug", "the source's config wins, not the target's"
        assert target.published_as == "indigo-101"

    def test_nudges_via_reattach_not_the_incremental_crud_calls(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        bridge = plug.export_bridge
        bridge.reattach = Mock(return_value=True)
        plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})
        bridge.reattach.assert_called_once_with()
        bridge.upsert.assert_not_called()
        bridge.replace.assert_not_called()

    def test_confirmation_log_names_source_role_number_and_devices(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        client = _bridge_with(plug, attached=True)
        client.status = _endpoint_status(101, 7, "onOffPlugInUnit")
        plug.export_bridge.reattach = Mock(return_value=True)

        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert ok is True
        said = _said(plug.logger.warning.call_args_list)
        assert 'MIGRATED accessory "Study Plug"' in said
        assert export_catalog.role_label("onOffPlugInUnit") in said
        assert "accessory number 7" in said
        assert "from Indigo device 101" in said
        assert 'to "Garage Plug" (id 106)' in said
        assert "identity never lapsed and nothing about it was removed or re-added" in said
        assert "previous accessory" not in said   # device 106 was never exported before

    def test_confirmation_log_names_the_targets_own_previous_accessory(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        plug.exports.upsert(ExportEntry(106, "onOffPlugInUnit"))
        client = _bridge_with(plug, attached=True)
        client.status = _endpoint_status(106, 11, "onOffPlugInUnit")
        plug.export_bridge.reattach = Mock(return_value=True)

        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert ok is True
        said = _said(plug.logger.warning.call_args_list)
        assert '"Garage Plug"\'s own previous accessory (number 11) has been removed ' \
            "from your ecosystems." in said

    def test_confirmation_removal_sentence_survives_an_unknown_number(self, plug, devices):
        """Issue #246 review finding 3 — gated on whether the target's own
        accessory was actually REMOVED (a pre-existing entry discarded by the
        atomic commit), not on whether ``_previous_accessory_number`` could
        read its number from a cached ``StatusReport``. The default fake
        client here has no status configured at all, so the number is
        unknown — the sentence must still appear, just without one."""
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        plug.exports.upsert(ExportEntry(106, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)   # client.status left unset -> unreadable
        plug.export_bridge.reattach = Mock(return_value=True)

        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert ok is True
        said = _said(plug.logger.warning.call_args_list)
        assert '"Garage Plug"\'s own previous accessory has been removed from your ecosystems.' \
            in said
        assert "(number" not in said

    def test_confirmation_headline_present_without_a_removal_sentence_when_the_target_was_bare(
            self, plug, devices):
        """The inverse: no pre-existing target entry -> nothing was removed,
        so the removal sentence stays absent, but the (now correctly scoped)
        headline still fires."""
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)

        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert ok is True
        said = _said(plug.logger.warning.call_args_list)
        assert "identity never lapsed and nothing about it was removed or re-added" in said
        assert "previous accessory" not in said

    def test_a_deleted_source_device_still_commits_with_a_placeholder_label(self, plug, devices):
        """The allow-list entry is the legitimate migrate source even once its
        Indigo device is gone (:meth:`ServerMenuMixin.getMigrateSources`'s own
        contract) — the commit must not depend on a live source device, and
        the confirmation has to fall back to `_migrate_source_display_name`'s
        "device <id>" placeholder rather than raising."""
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(999999, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)

        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "999999", "migrateDevice": "106"})

        assert ok is True
        assert plug.exports.get(999999) is None
        assert plug.exports.get(106).published_as == "indigo-999999"
        said = _said(plug.logger.warning.call_args_list)
        assert 'MIGRATED accessory "device 999999"' in said
        assert "from Indigo device 999999" in said

    def test_a_successful_migrate_refreshes_the_exported_ids_hot_path_set(self, plug, devices):
        """``_exports_changed`` (plugin.py:361) is the seam every store write
        must call — it refreshes ``_exported_ids``, the set ``deviceUpdated``
        consults on Indigo's own callback thread. A migrate has to call it
        too: source out, target in."""
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)

        plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert 101 not in plug._exported_ids
        assert 106 in plug._exported_ids


class TestMigrateOutcomeTruthfulness:
    def test_a_failed_nudge_is_not_reported_as_a_successful_migrate(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=False)

        ok, _values_out, errors = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})

        assert ok is False and "migrateDevice" in errors
        assert "the bridge node was not told" in errors["migrateDevice"]
        # The store write DID land — the message says "Saved", and this is
        # what makes that true.
        assert plug.exports.get(106).published_as == "indigo-101"
        assert plug.exports.get(101) is None
        assert "MIGRATED" not in _said(plug.logger.warning.call_args_list)


# ---------------------------------------------------------------------------
# Re-exporting/role-changing after a migrate — no stolen identities
# (issue #246 review finding 1)
# ---------------------------------------------------------------------------
class TestReExportAfterMigrateIdentityCollisionGuard:
    """A migrate moves a device's accessory identity onto ANOTHER device's
    entry. Re-exporting the original device (or role-changing either side)
    afterwards through the ordinary "Manage Matter Exports…" dialog must
    never try to publish an identity another entry already claims — the node
    refuses a duplicate ``publishedAs`` for the WHOLE attach
    (``parseEndpointSpecs``, ``malformed_args``), taking every export
    offline, not just the new one.
    """

    def _migrate_101_to_106(self, plug, devices):
        devices.add(RelayDevice(106, "Garage Plug"))
        plug.exports.upsert(ExportEntry(101, "onOffPlugInUnit"))
        _bridge_with(plug, attached=True)
        plug.export_bridge.reattach = Mock(return_value=True)
        ok, _values_out = plug.menuMigrateExport(
            {"migrateConfirm": True, "migrateSource": "101", "migrateDevice": "106"})
        assert ok is True
        assert plug.exports.get(106).published_as == "indigo-101"
        assert plug.exports.get(101) is None

    def test_reexporting_the_migrated_away_device_gets_a_fresh_identity(self, plug, devices):
        self._migrate_101_to_106(plug, devices)

        values = plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")

        entry = plug.exports.get(101)
        assert entry is not None
        assert entry.published_as == "indigo-101~2", "explicit, not None — indigo-101 is taken"
        identities = [e.published_as or f"indigo-{e.indigo_device_id}" for e in plug.exports.all()]
        assert len(identities) == len(set(identities)), "no duplicate identities in the store"
        assert "Added Study Plug" in values["exportStatus"]
        said = _said(plug.logger.info.call_args_list)
        assert "migrated to another device" in said
        assert "NEW accessory" in said
        assert "does not steal back the migrated one" in said

    def test_an_unmigrated_re_export_is_unaffected_and_still_stores_none(self, plug):
        """The common case (nobody has ever migrated anything) is untouched:
        a fresh export still stores ``published_as=None``, publishing the
        default derivation, exactly as before this guard existed."""
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        assert plug.exports.get(101).published_as is None

    def test_role_change_after_migrate_walks_past_a_claimed_generation(self, plug, devices):
        """B's migrated entry (indigo-101) role-changes while indigo-101~2 is
        ALSO claimed (by A, re-exported in the meantime) — B must land on
        indigo-101~3, not collide with A."""
        self._migrate_101_to_106(plug, devices)
        plug.exportAddOrUpdate(
            _values(exportDevice="101", exportRole="onOffPlugInUnit"), "manageMatterExports")
        assert plug.exports.get(101).published_as == "indigo-101~2"

        plug.exportAddOrUpdate(
            _values(exportDevice="106", exportRole="onOffLight"), "manageMatterExports")

        entry = plug.exports.get(106)
        assert entry.published_as == "indigo-101~3"
        identities = [e.published_as or f"indigo-{e.indigo_device_id}" for e in plug.exports.all()]
        assert len(identities) == len(set(identities))


# ---------------------------------------------------------------------------
# Custom-state mapping in the dialog (ADR-0012, issue #252)
# ---------------------------------------------------------------------------
def _zone(plug, devices, dev_id=201, name="Study PIR", **kwargs):
    zone = fakes.texecom_zone(dev_id, name, **kwargs)
    devices.add(zone)
    return zone


def test_a_mappable_device_is_selectable_in_the_picker(plug, devices):
    """Not an `x-` row. The whole point is that the user CAN export it — the
    label carries the extra step instead of a refusal."""
    _zone(plug, devices)
    labels = _labels(plug.getExportCandidates(valuesDict=_values()))
    assert "201" in labels
    assert "needs a state" in labels["201"]


def test_selecting_a_mappable_device_opens_the_mapping_fields(plug, devices):
    _zone(plug, devices)
    values = plug.exportDeviceChanged(_values(exportDevice="201"))
    assert values["exportNeedsMapping"] == "true"
    assert values["exportStateKey"] == "status"
    # The role is seeded too. This line used to assert "" — "no role until a
    # state is picked" — which was true of the DESIGN and false of the screen:
    # a state has just been picked, on the user's behalf, so leaving the role
    # blank made every zone of a panel a manual role selection.
    assert values["exportRole"] == "occupancySensor"     # "Study PIR"


def test_no_roles_are_offered_until_a_state_is_chosen(plug, devices):
    _zone(plug, devices)
    assert plug.getExportRoles(valuesDict=_values(exportDevice="201")) == []
    offered = plug.getExportRoles(valuesDict=_values(exportDevice="201", exportStateKey="status"))
    assert [role for role, _label in offered] == list(export_catalog.BINARY_SENSOR_ROLES)


def test_choosing_a_state_seeds_the_default_role(plug, devices):
    _zone(plug, devices)
    values = plug.exportStateKeyChanged(_values(exportDevice="201", exportStateKey="status"))
    assert values["exportRole"] == "occupancySensor"


def test_adding_without_a_state_is_refused(plug, devices):
    """An entry with no state key would fall back to reading `onState`, which
    this device does not have, and publish nothing forever."""
    _zone(plug, devices)
    values = plug.exportAddOrUpdate(_values(exportDevice="201", exportRole="occupancySensor"))
    assert "needs a state" in values["exportStatus"]
    assert 201 not in plug.exports


def test_a_mapped_export_is_saved_with_its_mapping(plug, devices):
    _zone(plug, devices)
    plug.exportAddOrUpdate(_values(exportDevice="201", exportStateKey="status",
                                   exportRole="occupancySensor"))
    entry = plug.exports.get(201)
    assert entry.role == "occupancySensor"
    assert entry.options["stateKey"] == "status"


def test_reselecting_a_mapped_export_shows_its_mapping(plug, devices):
    """Classified WITH its saved options, or the device being bridged right
    now would be reported as one still waiting to be set up."""
    _zone(plug, devices)
    plug.exportAddOrUpdate(_values(exportDevice="201", exportStateKey="status",
                                   exportRole="occupancySensor"))
    values = plug.exportDeviceChanged(_values(exportDevice="201"))
    assert values["exportStateKey"] == "status"
    assert values["exportRole"] == "occupancySensor"
    assert "is exported as" in values["exportStatus"]


def test_the_fleet_recipe_prefills_the_next_zone(plug, devices):
    """The 24-zone motivation: having answered "the boolean is `status`" once,
    the rest of the panel should only need a role."""
    _zone(plug, devices, 201, "Study PIR")
    _zone(plug, devices, 202, "Front Door")
    plug.exportAddOrUpdate(_values(exportDevice="201", exportStateKey="status",
                                   exportRole="occupancySensor"))
    values = plug.exportDeviceChanged(_values(exportDevice="202"))
    assert values["exportStateKey"] == "status"
    # The state key comes from the FLEET; the role is still decided per zone,
    # from this device's own name — the panel shares a boolean, not a meaning.
    assert values["exportRole"] == "contactSensor"       # "Front Door"


def test_the_fleet_recipe_does_not_cross_device_types(plug, devices):
    _zone(plug, devices, 201, "Study PIR")
    other = fakes.CustomDevice(202, "Camera", states={"recording": False},
                               plugin_id="org.cynic.indigo.securityspy",
                               deviceTypeId="camera")
    devices.add(other)
    plug.exportAddOrUpdate(_values(exportDevice="201", exportStateKey="status",
                                   exportRole="occupancySensor"))
    values = plug.exportDeviceChanged(_values(exportDevice="202"))
    assert values["exportStateKey"] == "recording"


def test_a_standard_sensor_never_shows_the_mapping_fields(plug, devices):
    values = plug.exportDeviceChanged(_values(exportDevice="103"))
    assert values["exportNeedsMapping"] == "false"
    assert values["exportStateKey"] == ""


def test_a_stale_state_key_is_not_saved_onto_a_non_binary_role(plug, devices):
    """`ExportEntry` refuses a mapping on a numeric role, so saving one would
    produce an entry that survives until the next load and is then dropped —
    the worst possible time to find out."""
    devices.add(SensorDevice(106, "Loft Temperature", supportsSensorValue=True,
                             displayStateValUi="19.5 °C"))
    plug.exportAddOrUpdate(_values(exportDevice="106", exportStateKey="status",
                                   exportRole="temperatureSensor"))
    assert plug.exports.get(106).options == {}


# ---------------------------------------------------------------------------
# Migrating onto a mapped device (issue #252 follow-up)
# ---------------------------------------------------------------------------
def test_migrate_takes_the_state_mapping_from_the_target_not_the_source(plug, devices):
    """The mapping describes the HARDWARE, not the accessory.

    Live case: an accessory on a Masquerade proxy (no mapping — the proxy has
    a real `onState`) migrated onto a Texecom zone. Carrying the source's
    empty mapping across left the zone's entry with no state key, so it
    classified as merely mappable, the endpoint provider skipped it, and the
    accessory went dark instead of moving.
    """
    devices.add(SensorDevice(330, "msqGateContact", supportsOnState=True))
    devices.add(fakes.texecom_zone(331, "Gate Contact"))
    plug.exports.upsert(ExportEntry(330, "contactSensor"))                       # the accessory
    plug.exports.upsert(ExportEntry(331, "contactSensor",                        # the zone
                                    options={"stateKey": "status", "stateInvert": True}))
    client = _bridge_with(plug, attached=True)
    errors = {}
    plug._migrate_commit(client, 330, plug.exports.get(330),
                         devices[331], errors, _values())
    moved = plug.exports.get(331)
    assert moved.published_as == "indigo-330", "the accessory identity moved"
    assert moved.options["stateKey"] == "status", "and the zone kept its own mapping"
    assert moved.options["stateInvert"] is True
    assert 330 not in plug.exports, "the source entry is gone"


def test_migrate_drops_a_mapping_the_new_device_cannot_honour(plug, devices):
    """The mirror. Migrating OFF a mapped custom device onto ordinary hardware
    must not carry a state key naming a state the new device never publishes —
    that would classify as excluded and go dark just as surely."""
    devices.add(fakes.texecom_zone(340, "Gate Contact"))
    devices.add(SensorDevice(341, "Z-Wave Gate Contact", supportsOnState=True))
    plug.exports.upsert(ExportEntry(340, "contactSensor",
                                    options={"stateKey": "status", "stateInvert": True}))
    client = _bridge_with(plug, attached=True)
    plug._migrate_commit(client, 340, plug.exports.get(340),
                         devices[341], {}, _values())
    moved = plug.exports.get(341)
    assert "stateKey" not in moved.options
    assert "stateInvert" not in moved.options


def test_migrate_still_carries_non_mapping_options_from_the_source():
    """Window-covering polarity and anything else still follows the accessory —
    only the state mapping is device-scoped."""
    from server_menu_mixin import _migrated_options
    carried = _migrated_options({"invert": True, "stateKey": "old"},
                                {"stateKey": "status", "stateInvert": True})
    assert carried == {"invert": True, "stateKey": "status", "stateInvert": True}


# ---------------------------------------------------------------------------
# Contact-sensor polarity (issue #252, live 2026-08-19)
# ---------------------------------------------------------------------------
def test_a_new_contact_export_starts_inverted(plug, devices):
    """Matter's own asymmetry. Indigo's `onState` means TRIPPED for every
    binary sensor — motion seen, leak seen, door OPEN — and Matter agrees for
    all of them except contact, where BooleanState is "TRUE = closed". Exported
    straight through, a Z-Wave door sensor called every shut door open."""
    devices.add(SensorDevice(401, "Pantry Door", supportsOnState=True))
    values = plug.exportDeviceChanged(_values(exportDevice="401"))
    assert values["exportRole"] == "contactSensor"
    assert values["exportStateInvert"] is True


def test_a_new_occupancy_export_does_not(plug, devices):
    devices.add(SensorDevice(402, "Landing PIR", supportsOnState=True))
    values = plug.exportDeviceChanged(_values(exportDevice="402"))
    assert values["exportRole"] == "occupancySensor"
    assert values["exportStateInvert"] is False


def test_changing_the_role_re_derives_the_polarity(plug, devices):
    """The same boolean means different things under different roles, so the
    tick follows the role rather than persisting a value chosen for another."""
    devices.add(SensorDevice(403, "Landing PIR", supportsOnState=True))
    values = plug.exportRoleChanged(_values(exportDevice="403", exportRole="contactSensor"))
    assert values["exportStateInvert"] is True
    values = plug.exportRoleChanged(_values(exportDevice="403", exportRole="occupancySensor"))
    assert values["exportStateInvert"] is False


def test_an_existing_exports_polarity_is_never_re_derived(plug, devices):
    """A saved entry's polarity is a decision already made, possibly
    hand-corrected — re-deriving it on the way past would undo that."""
    devices.add(SensorDevice(404, "Pantry Door", supportsOnState=True))
    plug.exports.upsert(ExportEntry(404, "contactSensor"))       # deliberately NOT inverted
    values = plug.exportRoleChanged(_values(exportDevice="404", exportRole="contactSensor",
                                            exportStateInvert=False))
    assert values["exportStateInvert"] is False


def test_polarity_applies_to_a_plain_onstate_sensor(plug, devices):
    """The half that was missing entirely: inversion used to apply only on the
    mapped path, so an ordinary sensor had no way to correct its polarity."""
    import export_handlers
    dev = SensorDevice(405, "Pantry Door", supportsOnState=True, onState=False)
    handler = export_handlers.handler_for("contactSensor")
    assert handler.states_for(dev, {}) == {"contact": False}
    assert handler.states_for(dev, {"stateInvert": True}) == {"contact": True}
