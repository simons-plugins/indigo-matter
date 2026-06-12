"""M0 acceptance: the plugin module imports and its XML wiring is consistent.

Validates that every CallbackMethod referenced in Actions.xml / MenuItems.xml
exists on the Plugin class, and that all bundle XML is well-formed. This catches
the classic "renamed a handler but not the XML" breakage before it reaches Indigo.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

BUNDLE = (
    Path(__file__).parent.parent
    / "indigo-matter.indigoPlugin"
    / "Contents"
)
SERVER_PLUGIN = BUNDLE / "Server Plugin"

XML_FILES = [
    BUNDLE / "Info.plist",
    SERVER_PLUGIN / "Devices.xml",
    SERVER_PLUGIN / "Actions.xml",
    SERVER_PLUGIN / "MenuItems.xml",
    SERVER_PLUGIN / "PluginConfig.xml",
    SERVER_PLUGIN / "Events.xml",
]


@pytest.fixture
def plugin_cls(mock_indigo_base):
    import importlib

    import plugin as plugin_module
    importlib.reload(plugin_module)
    return plugin_module.Plugin


@pytest.mark.parametrize("xml_path", XML_FILES, ids=lambda p: p.name)
def test_bundle_xml_is_well_formed(xml_path):
    assert xml_path.exists(), f"missing {xml_path}"
    ET.parse(xml_path)  # raises on malformed XML


def _callback_methods(xml_path: Path) -> list[str]:
    root = ET.parse(xml_path).getroot()
    return [el.text.strip() for el in root.iter("CallbackMethod") if el.text]


def test_action_callbacks_exist_on_plugin(plugin_cls):
    for method in _callback_methods(SERVER_PLUGIN / "Actions.xml"):
        assert hasattr(plugin_cls, method), f"Actions.xml references missing method {method}()"


def test_menu_callbacks_exist_on_plugin(plugin_cls):
    for method in _callback_methods(SERVER_PLUGIN / "MenuItems.xml"):
        assert hasattr(plugin_cls, method), f"MenuItems.xml references missing method {method}()"


def test_core_lifecycle_methods_present(plugin_cls):
    for method in (
        "startup", "shutdown", "runConcurrentThread",
        "deviceStartComm", "deviceStopComm",
        "actionControlDevice", "actionControlSensor", "actionControlThermostat",
        "closedPrefsConfigUi",
    ):
        assert hasattr(plugin_cls, method), f"Plugin missing lifecycle method {method}()"


def test_http_handlers_present(plugin_cls):
    for handler in ("http_status", "http_commission", "http_decommission", "http_diagnostics"):
        assert hasattr(plugin_cls, handler), f"Plugin missing IWS handler {handler}()"


def test_info_plist_bundle_id_and_version(plugin_cls):
    root = ET.parse(BUNDLE / "Info.plist").getroot()
    # plist: <dict> of alternating <key>/<value> — collect into a map
    d = root.find("dict")
    pairs = {}
    key = None
    for child in d:
        if child.tag == "key":
            key = child.text
        elif key is not None:
            pairs[key] = child.text
            key = None
    assert pairs["CFBundleIdentifier"] == "com.simon.indigo-matter"
    assert pairs["PluginVersion"]  # present and non-empty


def test_dynamic_list_methods_exist_on_plugin(plugin_cls):
    """<List class="self" method="..."> references are invisible to the
    CallbackMethod checks above; a renamed list method ships a silently
    empty picker. Validate them across all bundle XML."""
    checked = 0
    for xml_path in XML_FILES:
        if xml_path.suffix != ".xml":
            continue
        root = ET.parse(xml_path).getroot()
        for el in root.iter("List"):
            if el.get("class") == "self" and el.get("method"):
                checked += 1
                assert hasattr(plugin_cls, el.get("method")), \
                    f"{xml_path.name} references missing list method {el.get('method')}()"
    assert checked >= 2  # getFabricBackups + getMatterNodes at minimum


def test_relay_devices_do_not_redefine_native_onoffstate():
    """onOffState is native to the relay base type — redefining it in <States>
    makes Indigo reject the device ("native state keys cannot be overriden").
    Scoped to type="relay" only: sensor types (motion/contact/smoke) legitimately
    declare a custom onOffState because they set no SupportsOnState."""
    root = ET.parse(SERVER_PLUGIN / "Devices.xml").getroot()
    offenders = [
        dev.get("id")
        for dev in root.iter("Device")
        if dev.get("type") == "relay"
        and any(s.get("id") == "onOffState" for s in dev.iter("State"))
    ]
    assert not offenders, f"relay-type devices must not redefine native onOffState: {offenders}"


# ---------------------------------------------------------------------------
# fix/#58 — type-edit guard
# ---------------------------------------------------------------------------

def _dev(props, type_id="matterTemperatureSensor"):
    from types import SimpleNamespace
    return SimpleNamespace(id=5, name="Landing Sensor", deviceTypeId=type_id,
                           pluginProps=props)


def test_validate_rejects_type_change(plugin_cls, mock_indigo_base):
    mock_indigo_base.devices = {5: _dev({"createdTypeId": "matterTemperatureSensor"})}
    ok, _values, errors = plugin_cls.validateDeviceConfigUi(None, {}, "matterRelay", 5)
    assert ok is False
    assert "cannot be changed" in errors["showAlertText"]


def test_validate_allows_unchanged_type(plugin_cls, mock_indigo_base):
    mock_indigo_base.devices = {5: _dev({"createdTypeId": "matterTemperatureSensor"})}
    result = plugin_cls.validateDeviceConfigUi(None, {}, "matterTemperatureSensor", 5)
    assert result[0] is True


def test_validate_allows_unstamped_device(plugin_cls, mock_indigo_base):
    # Pre-guard or manually created device — no stamp, no opinion.
    mock_indigo_base.devices = {5: _dev({})}
    result = plugin_cls.validateDeviceConfigUi(None, {}, "matterRelay", 5)
    assert result[0] is True


def test_validate_allows_unknown_device_id(plugin_cls, mock_indigo_base):
    mock_indigo_base.devices = {}
    result = plugin_cls.validateDeviceConfigUi(None, {}, "matterRelay", 999)
    assert result[0] is True


def test_device_start_comm_warns_on_type_mismatch(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    stub = SimpleNamespace(logger=mock_logger, device_sync=MagicMock())
    dev = _dev({"createdTypeId": "matterTemperatureSensor"}, type_id="matterRelay")
    plugin_cls.deviceStartComm(stub, dev)
    assert mock_logger.warning.called
    fmt, *args = mock_logger.warning.call_args[0]
    message = fmt % tuple(args)
    assert "cannot be changed" in message and "Landing Sensor" in message
    stub.device_sync.note_device.assert_called_once_with(dev)


def test_device_start_comm_quiet_when_types_match(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    stub = SimpleNamespace(logger=mock_logger, device_sync=MagicMock())
    dev = _dev({"createdTypeId": "matterRelay"}, type_id="matterRelay")
    plugin_cls.deviceStartComm(stub, dev)
    assert not mock_logger.warning.called


def test_device_start_comm_refreshes_state_list(plugin_cls, mock_logger):
    # Indigo never re-reads Devices.xml for existing devices — deviceStartComm
    # must request the refresh so states added by upgrades exist (issue #60).
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    stub = SimpleNamespace(logger=mock_logger, device_sync=MagicMock())
    dev = _dev({"createdTypeId": "matterRelay"}, type_id="matterRelay")
    dev.stateListOrDisplayStateIdChanged = MagicMock()
    plugin_cls.deviceStartComm(stub, dev)
    dev.stateListOrDisplayStateIdChanged.assert_called_once_with()
