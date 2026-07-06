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
    assert pairs["CFBundleIdentifier"] == "com.simons-plugins.indigo-matter"
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


def test_action_control_device_sends_each_command_of_a_list(plugin_cls):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    stub = SimpleNamespace(device_sync=MagicMock(), _send_matter_command=MagicMock())
    stub._send_built_commands = lambda commands, dev: plugin_cls._send_built_commands(stub, commands, dev)
    pair = ["cmd-a", "cmd-b"]
    stub.device_sync.build_command.return_value = pair
    plugin_cls.actionControlDevice(stub, action=SimpleNamespace(), dev="dev")
    sent = [c[0][0] for c in stub._send_matter_command.call_args_list]
    assert sent == pair


# ---------------------------------------------------------------------------
# issue #85 — Set Sensitivity Level (the plugin's first custom device action)
# ---------------------------------------------------------------------------

def _sensitivity_dev(node_id="45", endpoint_id="1", states=None):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = SimpleNamespace(
        id=5, name="Landing Presence",
        pluginProps={"nodeId": node_id, "endpointId": endpoint_id},
        # Real 0x0080-bearing devices carry the XML-declared state; the action
        # callback rejects devices without it (deviceFilter="self" fallback).
        states={"sensitivityLevel": 1} if states is None else states,
        updateStateOnServer=MagicMock(),
    )
    return dev


def test_action_set_sensitivity_level_rejects_device_without_state(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev(states={})  # e.g. a relay picked via deviceFilter="self"
    stub = SimpleNamespace(
        device_sync=MagicMock(), logger=mock_logger,
        _send_matter_command=MagicMock(return_value=True),
    )
    plugin_cls.actionSetSensitivityLevel(stub, SimpleNamespace(props={"level": "1"}), dev)
    stub._send_matter_command.assert_not_called()
    dev.updateStateOnServer.assert_not_called()
    assert any("does not support sensitivity" in str(c) for c in mock_logger.error.call_args_list)


def test_get_sensitivity_levels_confirmed_three_gets_friendly_labels(plugin_cls, mock_indigo_base, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    mock_indigo_base.devices = {5: dev}
    stub = SimpleNamespace(device_sync=MagicMock(), logger=mock_logger)
    stub.device_sync.sensitivity_levels_supported.return_value = 3
    options = plugin_cls.getSensitivityLevels(stub, targetId=5)
    assert options == [("0", "Low (0)"), ("1", "Standard (1)"), ("2", "High (2)")]
    stub.device_sync.sensitivity_levels_supported.assert_called_once_with(45, 1)


def test_get_sensitivity_levels_confirmed_other_count_gets_generic_labels(plugin_cls, mock_indigo_base, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    mock_indigo_base.devices = {5: dev}
    stub = SimpleNamespace(device_sync=MagicMock(), logger=mock_logger)
    stub.device_sync.sensitivity_levels_supported.return_value = 5
    options = plugin_cls.getSensitivityLevels(stub, targetId=5)
    assert options == [("0", "Level 0"), ("1", "Level 1"), ("2", "Level 2"),
                        ("3", "Level 3"), ("4", "Level 4")]


def test_get_sensitivity_levels_unknown_count_falls_back_generic(plugin_cls, mock_indigo_base, mock_logger):
    """Unknown support (device not yet reconciled) must NOT presume the FP300's
    Low/Standard/High semantics even though the fallback also has 3 entries."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    mock_indigo_base.devices = {5: dev}
    stub = SimpleNamespace(device_sync=MagicMock(), logger=mock_logger)
    stub.device_sync.sensitivity_levels_supported.return_value = None
    options = plugin_cls.getSensitivityLevels(stub, targetId=5)
    assert options == [("0", "Level 0"), ("1", "Level 1"), ("2", "Level 2")]


def test_get_sensitivity_levels_degrades_when_device_lookup_fails(plugin_cls, mock_indigo_base, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {}  # targetId not found
    stub = SimpleNamespace(device_sync=MagicMock(), logger=mock_logger)
    options = plugin_cls.getSensitivityLevels(stub, targetId=999)
    assert options == [("0", "Level 0"), ("1", "Level 1"), ("2", "Level 2")]


def test_action_set_sensitivity_level_sends_matter_write_and_echoes_state(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from protocol import MatterWrite
    dev = _sensitivity_dev()
    stub = SimpleNamespace(
        device_sync=MagicMock(), logger=mock_logger,
        _send_matter_command=MagicMock(return_value=True),
    )
    stub.device_sync.sensitivity_levels_supported.return_value = 3
    action = SimpleNamespace(props={"level": "2"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)

    assert stub._send_matter_command.call_count == 1
    write, sent_dev = stub._send_matter_command.call_args[0]
    assert isinstance(write, MatterWrite)
    assert (write.node_id, write.endpoint, write.cluster, write.attribute, write.value) == (45, 1, 0x0080, 0x0000, 2)
    assert sent_dev is dev
    dev.updateStateOnServer.assert_called_once_with("sensitivityLevel", 2)


def test_action_set_sensitivity_level_no_echo_when_send_fails(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    stub = SimpleNamespace(
        device_sync=MagicMock(), logger=mock_logger,
        _send_matter_command=MagicMock(return_value=False),
    )
    stub.device_sync.sensitivity_levels_supported.return_value = 3
    action = SimpleNamespace(props={"level": "1"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    dev.updateStateOnServer.assert_not_called()


def test_action_set_sensitivity_level_rejects_out_of_range(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    stub = SimpleNamespace(
        device_sync=MagicMock(), logger=mock_logger,
        _send_matter_command=MagicMock(return_value=True),
    )
    stub.device_sync.sensitivity_levels_supported.return_value = 3
    action = SimpleNamespace(props={"level": "5"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub._send_matter_command.assert_not_called()
    dev.updateStateOnServer.assert_not_called()
    assert mock_logger.error.called


def test_action_set_sensitivity_level_rejects_invalid_level(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    stub = SimpleNamespace(device_sync=MagicMock(), logger=mock_logger,
                           _send_matter_command=MagicMock())
    action = SimpleNamespace(props={"level": "not-a-number"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub._send_matter_command.assert_not_called()
    assert mock_logger.error.called


def test_action_set_sensitivity_level_rejects_device_with_no_node(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev(node_id="", endpoint_id="")
    stub = SimpleNamespace(device_sync=MagicMock(), logger=mock_logger,
                           _send_matter_command=MagicMock())
    action = SimpleNamespace(props={"level": "1"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub._send_matter_command.assert_not_called()
    assert mock_logger.error.called


def test_action_set_sensitivity_level_allows_any_level_when_support_unknown(plugin_cls, mock_logger):
    """No confirmed SupportedSensitivityLevels yet — don't block the write;
    matter-server/the device is the final arbiter."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    stub = SimpleNamespace(
        device_sync=MagicMock(), logger=mock_logger,
        _send_matter_command=MagicMock(return_value=True),
    )
    stub.device_sync.sensitivity_levels_supported.return_value = None
    action = SimpleNamespace(props={"level": "9"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub._send_matter_command.assert_called_once()
