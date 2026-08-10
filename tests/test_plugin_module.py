"""M0 acceptance: the plugin module imports and its XML wiring is consistent.

Validates that every CallbackMethod referenced in Actions.xml / MenuItems.xml
exists on the Plugin class, and that all bundle XML is well-formed. This catches
the classic "renamed a handler but not the XML" breakage before it reaches Indigo.
"""
from __future__ import annotations

import re
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


# ---------------------------------------------------------------------------
# issue #134 — the Plugins ▸ Matter menu's five sections
# ---------------------------------------------------------------------------

MENU_SECTIONS = [
    # 1 · Matter devices Indigo controls
    ["Commission device by setup code (advanced)…",
     "Decommission Matter device…"],
    # 2 · Matter devices Indigo publishes
    ["Manage Matter Exports…",
     "Pair Matter Bridge…",
     "Unpair an Ecosystem…"],
    # 3 · the Matter controller (matter-server)
    ["Install/update the Matter controller (matter-server)",
     "Restart the Matter controller",
     "Reinstall the Matter controller (clean)…",
     "Open the Matter controller log…"],
    # 4 · the Matter bridge node
    ["Install/update the Matter bridge",
     "Reinstall the Matter bridge (clean)…",
     "Stop the Matter bridge…"],
    # 5 · backup and recovery
    ["Back up the Matter fabric…",
     "Restore a fabric backup…",
     "Rebuild Matter Endpoint Map…",
     "Reset Matter Bridge Pairings…"],
]


def _menu_items():
    return ET.parse(SERVER_PLUGIN / "MenuItems.xml").getroot().findall("MenuItem")


def _menu_sections() -> list[list[str]]:
    """MenuItems.xml split on its separators.

    A separator is an EMPTY, self-closing ``<MenuItem id="…"/>`` — undocumented
    (the canonical reference lists ``<Name>`` as required) but what Perceptive
    Automation's own bundled plugins use.
    """
    sections: list[list[str]] = [[]]
    for item in _menu_items():
        name = item.findtext("Name")
        if name is None:
            sections.append([])
        else:
            sections[-1].append(name)
    return sections


def test_menu_is_grouped_into_the_documented_sections():
    """The ORDER is the change (#134), so the order is what is asserted.

    A membership-only check would pass just as happily with all sixteen items
    back in one flat list, which is the state this exists to prevent.
    """
    assert _menu_sections() == MENU_SECTIONS


def test_the_two_install_items_are_separated():
    """#134: 'Install/update the Matter controller (matter-server)' and
    'Install/update the Matter bridge' sat eleven apart in one flat list and
    were clicked for one another live — reinstalling the inbound controller
    when the outbound bridge was meant. They are separately versioned,
    separately installed npm packages, so they must not share a section."""
    sections = _menu_sections()
    controller = next(i for i, s in enumerate(sections)
                       if "Install/update the Matter controller (matter-server)" in s)
    bridge = next(i for i, s in enumerate(sections)
                  if "Install/update the Matter bridge" in s)
    assert controller != bridge


def test_separators_carry_nothing_but_a_unique_id():
    """Give a separator a <Name> and it becomes a clickable item that does
    nothing; give it a CallbackMethod and the wiring test above starts
    demanding a method that should not exist."""
    separators = [i for i in _menu_items() if i.findtext("Name") is None]
    assert len(separators) == len(MENU_SECTIONS) - 1
    for sep in separators:
        assert sep.get("id"), "a separator still needs an id"
        assert list(sep) == [], f"separator {sep.get('id')} must be empty"


def test_every_menu_item_id_is_unique():
    ids = [i.get("id") for i in _menu_items()]
    assert len(ids) == len(set(ids)), f"duplicate MenuItem ids: {ids}"


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


def test_validate_allows_unstamped_device(plugin_cls, mock_indigo_base, mock_logger):
    # Pre-guard or manually created device — no stamp, no opinion. matterRelay
    # carries settings since #186, so this now runs the settings path too.
    mock_indigo_base.devices = {5: _dev({})}
    stub = _settings_stub(plugin_cls, mock_logger, limits={}, attr_lists={})
    result = plugin_cls.validateDeviceConfigUi(stub, {}, "matterRelay", 5)
    assert result[0] is True


def test_validate_allows_unknown_device_id(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {}
    stub = _settings_stub(plugin_cls, mock_logger, limits={}, attr_lists={})
    result = plugin_cls.validateDeviceConfigUi(stub, {}, "matterRelay", 999)
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


def _action_stub(mock_logger, supported=3, runtime=None, matter=None):
    """Stand-in for the #85 action's Plugin. Since #186 the action submits a
    VERIFIED write onto the loop instead of calling _send_matter_command."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    stub = SimpleNamespace(
        device_sync=MagicMock(), logger=mock_logger,
        runtime=runtime if runtime is not None else MagicMock(),
        matter=matter if matter is not None else MagicMock(),
        _send_matter_command=MagicMock(return_value=True),
    )
    stub.device_sync.sensitivity_levels_supported.return_value = supported
    stub._apply_setting = MagicMock(return_value="coro")
    return stub


def test_action_set_sensitivity_level_submits_a_verified_write(plugin_cls, mock_logger):
    """Since #186 the action shares the dialog's write-and-verify path — ONE
    mechanism, two entry points."""
    from types import SimpleNamespace
    dev = _sensitivity_dev()
    stub = _action_stub(mock_logger)
    plugin_cls.actionSetSensitivityLevel(stub, SimpleNamespace(props={"level": "2"}), dev)

    assert stub.runtime.submit.call_count == 1
    node_id, endpoint_id, dev_id, plan = stub._apply_setting.call_args.args
    assert (node_id, endpoint_id, dev_id) == (45, 1, dev.id)
    assert (plan.setting.cluster, plan.setting.attribute) == (0x0080, 0x0000)
    assert (plan.value, plan.previous) == (2, 1)


def test_action_set_sensitivity_level_no_longer_echoes_optimistically(plugin_cls, mock_logger):
    """The echo was the #186 bug in miniature: a device that ACKs and ignores
    left Indigo reporting a sensitivity it never adopted. The state is now
    written only after the read-back proves it."""
    from types import SimpleNamespace
    dev = _sensitivity_dev()
    stub = _action_stub(mock_logger)
    plugin_cls.actionSetSensitivityLevel(stub, SimpleNamespace(props={"level": "1"}), dev)
    dev.updateStateOnServer.assert_not_called()


def test_action_set_sensitivity_level_reports_a_down_connection(plugin_cls, mock_logger):
    from types import SimpleNamespace
    dev = _sensitivity_dev()
    stub = _action_stub(mock_logger, runtime=None, matter=None)
    stub.runtime = None
    stub.matter = None
    plugin_cls.actionSetSensitivityLevel(stub, SimpleNamespace(props={"level": "1"}), dev)
    assert any("not running" in str(c) for c in mock_logger.error.call_args_list)


def test_action_set_sensitivity_level_rejects_out_of_range(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    stub = _action_stub(mock_logger)
    action = SimpleNamespace(props={"level": "5"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub.runtime.submit.assert_not_called()
    dev.updateStateOnServer.assert_not_called()
    assert mock_logger.error.called


def test_action_set_sensitivity_level_rejects_invalid_level(plugin_cls, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _sensitivity_dev()
    stub = _action_stub(mock_logger)
    action = SimpleNamespace(props={"level": "not-a-number"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub.runtime.submit.assert_not_called()
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
    dev = _sensitivity_dev()
    stub = _action_stub(mock_logger, supported=None)
    action = SimpleNamespace(props={"level": "9"})
    plugin_cls.actionSetSensitivityLevel(stub, action, dev)
    stub.runtime.submit.assert_called_once()


@pytest.fixture
def plugin_module(mock_indigo_base):
    import importlib
    import plugin as pm
    importlib.reload(pm)
    return pm


class TestServerLocation:
    def test_explicit_local(self, plugin_module):
        assert plugin_module.server_location({"serverLocation": "local"}) == "local"

    def test_explicit_remote(self, plugin_module):
        assert plugin_module.server_location({"serverLocation": "remote"}) == "remote"

    def test_migrates_managed_to_local(self, plugin_module):
        # pre-2026.6 prefs: no serverLocation, LaunchAgent was managed
        assert plugin_module.server_location({"manageLaunchAgent": True}) == "local"

    def test_migrates_remote_host_to_remote(self, plugin_module):
        # pre-2026.6 prefs: not managed, host on another machine → connect-only
        assert plugin_module.server_location(
            {"manageLaunchAgent": False, "matterServerHost": "192.168.1.20"}) == "remote"

    def test_empty_prefs_default_local(self, plugin_module):
        # fresh install → turnkey local (matches PluginConfig.xml default)
        assert plugin_module.server_location({}) == "local"

    def test_loopback_self_run_is_local(self, plugin_module):
        # not managed but host is loopback → fold into turnkey local
        assert plugin_module.server_location(
            {"manageLaunchAgent": False, "matterServerHost": "localhost"}) == "local"


class TestSanitizeHost:
    def test_strips_scheme_and_embedded_port(self, plugin_module):
        # the exact paste from the field
        assert plugin_module.sanitize_host("http://jobs2.local:8176") == "jobs2.local"

    def test_strips_path(self, plugin_module):
        assert plugin_module.sanitize_host("jobs2.local/ws") == "jobs2.local"

    def test_plain_host_untouched(self, plugin_module):
        assert plugin_module.sanitize_host("192.168.1.20") == "192.168.1.20"

    def test_trims_whitespace(self, plugin_module):
        assert plugin_module.sanitize_host("  jobs2.local  ") == "jobs2.local"

    def test_ipv6_literal_preserved(self, plugin_module):
        # multiple colons → not treated as host:port
        assert plugin_module.sanitize_host("fd57::1") == "fd57::1"

    def test_blank_stays_blank(self, plugin_module):
        assert plugin_module.sanitize_host("") == ""


# ---------------------------------------------------------------------------
# Issue #147 — retired menu names must not survive anywhere a user reads
# ---------------------------------------------------------------------------

#: Menu names retired by the #147 terminology sweep. A message or doc that
#: still uses one sends the user to a menu item that no longer exists — and
#: the #147 review found exactly that, twice, in strings WRAPPED across
#: source lines, which no grep of the source text can see. Python strings are
#: therefore collected from the AST (adjacent literals concatenate) and docs
#: are scanned with newlines collapsed.
RETIRED_MENU_NAMES = [
    "Install/update matter-server",
    "Restart matter-server",
    "Reinstall matter-server (clean)…",
    "Open matter-server log…",
    "Install/update the Matter export bridge",
    "Reinstall the Matter export bridge (clean)…",
    "Stop the Matter export bridge…",
    "Reset Matter Export Pairings…",
]

#: Docs a user (or the next session) follows today. The PRDs are deliberately
#: absent: they are frozen planning records whose milestone tables name the
#: menus as they were when each milestone landed.
SCANNED_DOCS = [
    Path(__file__).parent.parent / "README.md",
    Path(__file__).parent.parent / "bridge-node" / "README.md",
    Path(__file__).parent.parent / "docs" / "INSTALL.md",
    Path(__file__).parent.parent / "docs" / "MATTER.md",
]


def _py_string_constants(path):
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@pytest.mark.parametrize("retired", RETIRED_MENU_NAMES)
def test_no_python_string_names_a_retired_menu_item(retired):
    offenders = []
    for py in sorted(SERVER_PLUGIN.glob("*.py")):
        for value in _py_string_constants(py):
            if retired in value:
                offenders.append(py.name)
                break
    assert not offenders, (
        f"{offenders} still reference the retired menu name {retired!r} — "
        "the menu was renamed in #147; users cannot find the old name")


@pytest.mark.parametrize("doc", SCANNED_DOCS, ids=lambda p: p.name)
def test_no_current_doc_names_a_retired_menu_item(doc):
    # Newlines collapsed so a name wrapped across lines cannot hide — the
    # exact way both #147 review criticals escaped the original sweep.
    text = " ".join(doc.read_text(encoding="utf-8").split())
    hits = [name for name in RETIRED_MENU_NAMES if name in text]
    assert not hits, f"{doc.name} still references retired menu name(s): {hits}"


#: Matches `import plugin` / `from plugin import ...` but not `plugin_constants`
#: or any other `plugin_`-prefixed module — the trailing `\b` fails right after
#: "plugin" when the next character is the word-character `_`.
_BACK_IMPORT_RE = re.compile(r"^\s*(import plugin\b|from plugin\b\s+import)")


def test_no_mixin_module_imports_plugin():
    """The dependency arrows point away from plugin.py — a back-import would
    make the extraction a cycle waiting to happen (issue #146)."""
    for path in SERVER_PLUGIN.rglob("*.py"):
        if path.name == "plugin.py":
            continue
        src = path.read_text(encoding="utf-8")
        offenders = [line for line in src.splitlines() if _BACK_IMPORT_RE.match(line)]
        assert not offenders, f"{path.name} back-imports plugin: {offenders}"


def test_plugin_modules_eviction_tuple_matches_real_files():
    """conftest's ``_PLUGIN_MODULES`` eviction uses ``raising=False``, so a
    typo'd or renamed entry would silently no-op forever — pin each entry to a
    real module file (issue #146)."""
    from conftest import _PLUGIN_MODULES

    for name in _PLUGIN_MODULES:
        assert (SERVER_PLUGIN / f"{name}.py").is_file(), (
            f"conftest._PLUGIN_MODULES entry {name!r} has no matching Server Plugin file"
        )


# ---------------------------------------------------------------------------
# issue #186 — the Edit Device dialog's Device Settings section
#
# These exercise the Indigo-facing callbacks; the decision logic they delegate
# to is covered in test_device_settings.py.
# ---------------------------------------------------------------------------

async def _async_none(*_args, **_kwargs):
    return None


def _async_value(value):
    async def _read(*_args, **_kwargs):
        return value
    return _read


_FP300_LIMITS = {
    (0x0406, 0x0004): {"0": 1, "1": 300, "2": 10},   # real wire shape
    (0x0080, 0x0001): 3,
}

#: AttributeList (0xFFFB) per cluster, as the live FP300 reports it — the
#: capability gate. Occupancy carries HoldTime (3); BooleanStateConfiguration
#: carries CurrentSensitivityLevel (0).
_FP300_ATTR_LISTS = {
    0x0406: [0, 1, 2, 3, 4, 65528, 65529, 65531, 65532, 65533],
    0x0080: [0, 1, 2, 65528, 65529, 65531, 65532, 65533],
}


def _settings_stub(plugin_cls, mock_logger, limits=None, runtime=None, matter=None,
                   attr_lists=None):
    """A Plugin stand-in wired to a limits table instead of a live node.

    ``_setting_limits_lookup`` is bound from the real class rather than mocked:
    it is the seam every settings callback goes through, so stubbing it would
    test nothing.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    table = _FP300_LIMITS if limits is None else limits
    device_sync = MagicMock()
    device_sync.setting_limits.side_effect = (
        lambda node, ep, cluster, attr: table.get((cluster, attr)))
    lists = _FP300_ATTR_LISTS if attr_lists is None else attr_lists
    device_sync.attribute_list.side_effect = (
        lambda node, ep, cluster: lists.get(cluster))
    stub = SimpleNamespace(device_sync=device_sync, logger=mock_logger,
                           runtime=runtime, matter=matter)
    stub._setting_limits_lookup = (
        lambda props: plugin_cls._setting_limits_lookup(stub, props))
    stub._device_states = lambda dev_id: plugin_cls._device_states(stub, dev_id)
    stub._setting_attribute_list_lookup = (
        lambda props: plugin_cls._setting_attribute_list_lookup(stub, props))
    stub._has_matter_node = plugin_cls._has_matter_node
    stub._log_setting_skips = lambda dev_id: plugin_cls._log_setting_skips(stub, dev_id)
    return stub


def _fp300_dev(states=None):
    from types import SimpleNamespace
    return SimpleNamespace(
        id=5, name="Landing Presence", deviceTypeId="matterMotionSensor",
        pluginProps={"nodeId": "56", "endpointId": "1"},
        states={"holdTime": 10, "sensitivityLevel": 1} if states is None else states,
    )


def test_config_ui_values_seed_the_dialog_from_device_state(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    values, _errors = plugin_cls.getDeviceConfigUiValues(
        stub, {"nodeId": "56", "endpointId": "1"}, "matterMotionSensor", 5)
    assert values["hasHoldTime"] == "yes"
    assert values["holdTime"] == "10"
    assert values["holdTimeRange"] == "1-300 seconds"


def test_config_ui_values_hide_settings_the_device_lacks(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger, limits={(0x0080, 0x0001): 3})
    values, _errors = plugin_cls.getDeviceConfigUiValues(
        stub, {"nodeId": "56", "endpointId": "1"}, "matterMotionSensor", 5)
    assert values["hasHoldTime"] == "no"
    assert values["hasSensitivity"] == "yes"


def test_config_ui_values_survive_a_device_that_has_no_node_yet(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {}
    stub = _settings_stub(plugin_cls, mock_logger)
    values, _errors = plugin_cls.getDeviceConfigUiValues(stub, {}, "matterMotionSensor", 0)
    assert values["hasAnySetting"] == "no"


def test_config_ui_values_keep_the_dialog_alive_if_the_lookup_explodes(plugin_cls, mock_indigo_base, mock_logger):
    """A broken settings section must never stop a user opening the dialog."""
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    stub.device_sync.setting_limits.side_effect = RuntimeError("boom")
    values, _errors = plugin_cls.getDeviceConfigUiValues(
        stub, {"nodeId": "56", "endpointId": "1"}, "matterMotionSensor", 5)
    assert values["nodeId"] == "56"          # the rest of the dialog is intact
    assert values["hasHoldTime"] == "no"     # and the settings simply hide


def test_validate_rejects_an_out_of_range_hold_time(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    ok, _values, errors = plugin_cls.validateDeviceConfigUi(
        stub, {"nodeId": "56", "endpointId": "1", "holdTime": "9999"},
        "matterMotionSensor", 5)
    assert ok is False
    assert "1 and 300" in errors["holdTime"]


def test_validate_accepts_an_in_range_hold_time(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    result = plugin_cls.validateDeviceConfigUi(
        stub, {"nodeId": "56", "endpointId": "1", "holdTime": "45"},
        "matterMotionSensor", 5)
    assert result[0] is True


def test_settings_picker_offers_the_devices_own_level_count(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    rows = plugin_cls.getSettingOptions(
        stub, filter="sensitivityLevel",
        valuesDict={"nodeId": "56", "endpointId": "1"},
        typeId="matterMotionSensor", targetId=5)
    assert [value for value, _label in rows] == ["0", "1", "2"]


def test_settings_picker_is_empty_when_the_device_does_not_support_it(plugin_cls, mock_indigo_base, mock_logger):
    """Unlike the #85 ACTION's picker, which must always offer something: this
    field is not shown at all unless the bounds are known, so there is nothing
    to guess about."""
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger, limits={})
    rows = plugin_cls.getSettingOptions(
        stub, filter="sensitivityLevel",
        valuesDict={"nodeId": "56", "endpointId": "1"},
        typeId="matterMotionSensor", targetId=5)
    assert rows == []


def test_closing_the_dialog_submits_only_changed_settings(plugin_cls, mock_indigo_base, mock_logger):
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {5: _fp300_dev()}
    runtime = MagicMock()
    stub = _settings_stub(plugin_cls, mock_logger, runtime=runtime, matter=MagicMock())
    stub._apply_setting = MagicMock(return_value="coro")
    plugin_cls.closedDeviceConfigUi(
        stub, {"nodeId": "56", "endpointId": "1", "holdTime": "30",
               "sensitivityLevel": "1"},
        False, "matterMotionSensor", 5)
    assert runtime.submit.call_count == 1
    plan = stub._apply_setting.call_args.args[3]
    assert (plan.setting.key, plan.value) == ("holdTime", 30)


def test_cancelling_the_dialog_writes_nothing(plugin_cls, mock_indigo_base, mock_logger):
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {5: _fp300_dev()}
    runtime = MagicMock()
    stub = _settings_stub(plugin_cls, mock_logger, runtime=runtime, matter=MagicMock())
    plugin_cls.closedDeviceConfigUi(
        stub, {"nodeId": "56", "endpointId": "1", "holdTime": "30"},
        True, "matterMotionSensor", 5)
    runtime.submit.assert_not_called()


def test_saving_an_unchanged_dialog_writes_nothing(plugin_cls, mock_indigo_base, mock_logger):
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {5: _fp300_dev()}
    runtime = MagicMock()
    stub = _settings_stub(plugin_cls, mock_logger, runtime=runtime, matter=MagicMock())
    plugin_cls.closedDeviceConfigUi(
        stub, {"nodeId": "56", "endpointId": "1", "holdTime": "10",
               "sensitivityLevel": "1"},
        False, "matterMotionSensor", 5)
    runtime.submit.assert_not_called()


def test_a_device_type_with_no_settings_never_touches_the_settings_path(plugin_cls, mock_indigo_base, mock_logger):
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {}
    runtime = MagicMock()
    stub = _settings_stub(plugin_cls, mock_logger, runtime=runtime, matter=MagicMock())
    plugin_cls.closedDeviceConfigUi(stub, {"nodeId": "56", "endpointId": "1"},
                                    False, "matterTemperatureSensor", 5)
    runtime.submit.assert_not_called()
    stub.device_sync.setting_limits.assert_not_called()


def test_settings_are_not_applied_while_the_connection_is_down(plugin_cls, mock_indigo_base, mock_logger):
    """Better an error naming the reason than a write silently dropped."""
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger, runtime=None, matter=None)
    plugin_cls.closedDeviceConfigUi(
        stub, {"nodeId": "56", "endpointId": "1", "holdTime": "30"},
        False, "matterMotionSensor", 5)
    assert any("not running" in str(c) for c in mock_logger.error.call_args_list)


def _run_apply(plugin_cls, stub, plan, dev_id=5):
    import asyncio
    return asyncio.run(plugin_cls._apply_setting(stub, 56, 1, dev_id, plan))


def _hold_time_plan(value=30, previous=10):
    from matter_handlers.settings import settings_for_type
    from device_settings import PlannedWrite
    setting = next(s for s in settings_for_type("matterMotionSensor")
                   if s.key == "holdTime")
    return PlannedWrite(setting, value, previous)


def test_a_verified_setting_is_written_through_to_the_device_state(plugin_cls, mock_indigo_base, mock_logger):
    """Not an optimistic echo — the read-back already proved the value is on
    the device before this state write happens."""
    from unittest.mock import MagicMock
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger, matter=MagicMock())
    stub.matter.write = _async_none
    stub.matter.read = _async_value(30)
    _run_apply(plugin_cls, stub, _hold_time_plan())
    stub.device_sync.apply_states.assert_called_once_with(
        5, [{"key": "holdTime", "value": 30}])
    assert mock_logger.info.called


def test_a_lying_device_leaves_the_state_alone_and_flags_the_device(plugin_cls, mock_indigo_base, mock_logger):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    dev = _fp300_dev()
    dev.setErrorStateOnServer = MagicMock()
    mock_indigo_base.devices = {5: dev}
    stub = _settings_stub(plugin_cls, mock_logger, matter=MagicMock())
    stub.matter.write = _async_none
    stub.matter.read = _async_value(10)     # ACKed the write, kept the old value
    _run_apply(plugin_cls, stub, _hold_time_plan())
    assert mock_logger.error.called
    dev.setErrorStateOnServer.assert_called_once_with("setting failed")
    # The requested value is NEVER written — that would be the optimistic echo
    # this feature exists to kill. What IS written is what the read-back proved
    # the device actually holds, so the dialog (which re-seeds from state)
    # cannot show a value the device never adopted.
    stub.device_sync.apply_states.assert_called_once_with(
        5, [{"key": "holdTime", "value": 10}])


def test_a_crash_in_the_write_path_never_kills_the_loop(plugin_cls, mock_indigo_base,
                                                        mock_logger, monkeypatch):
    import functools
    from unittest.mock import MagicMock
    import device_settings
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger, matter=MagicMock())

    async def boom(*_args, **_kwargs):
        raise RuntimeError("nope")

    # Skip the real retry backoff — this test is about the failure being
    # reported rather than escaping, not about how long we wait first.
    monkeypatch.setattr(device_settings, "apply_setting",
                        functools.partial(device_settings.apply_setting, sleep=_async_none))
    stub.matter.write = boom
    stub.matter.read = boom
    _run_apply(plugin_cls, stub, _hold_time_plan())
    assert mock_logger.exception.called or mock_logger.error.called
    stub.device_sync.apply_states.assert_not_called()


def test_device_config_ui_callbacks_have_snake_case_aliases(plugin_cls):
    """Indigo 2025.2's PluginBase carries both spellings and does not document
    which it dispatches on. camelCase is what this build calls, so these aliases
    guard against a future build preferring the other spelling."""
    for camel, snake in (
        ("getDeviceConfigUiValues", "get_device_config_ui_values"),
        ("validateDeviceConfigUi", "validate_device_config_ui"),
        ("closedDeviceConfigUi", "closed_device_config_ui"),
    ):
        assert hasattr(plugin_cls, camel), f"missing {camel}"
        assert hasattr(plugin_cls, snake), f"missing snake_case alias {snake}"


def test_config_ui_values_are_returned_as_an_indigo_dict(plugin_cls, mock_indigo_base, mock_logger):
    """Regression, found live on jarvis (#186): returning a plain dict fails
    inside Indigo's C++ bridge —

        Error in plugin execution UiGetValues2: No registered converter was able
        to extract a C++ reference to type CXmlDict from this Python object of
        type dict

    — and seeds NOTHING, so a section gated on hidden marker fields never
    appears and the only clue names an internal symbol, not this method."""
    import indigo
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    result = plugin_cls.getDeviceConfigUiValues(
        stub, {"nodeId": "56", "endpointId": "1"}, "matterMotionSensor", 5)
    assert isinstance(result, tuple) and len(result) == 2
    for part in result:
        assert isinstance(part, indigo.Dict)


def test_the_snake_case_alias_seeds_the_same_values(plugin_cls, mock_indigo_base, mock_logger):
    mock_indigo_base.devices = {5: _fp300_dev()}
    stub = _settings_stub(plugin_cls, mock_logger)
    stub.getDeviceConfigUiValues = (
        lambda props, type_id, dev_id: plugin_cls.getDeviceConfigUiValues(
            stub, props, type_id, dev_id))
    values, _errors = plugin_cls.get_device_config_ui_values(
        stub, {"nodeId": "56", "endpointId": "1"}, "matterMotionSensor", 5)
    assert values["hasHoldTime"] == "yes"
    assert values["holdTime"] == "10"


# ---------------------------------------------------------------------------
# Registry ↔ Devices.xml parity (issue #186)
#
# Indigo builds ConfigUI from static XML per device type with no runtime field
# injection, so the registry and the XML are kept in step BY HAND. Nothing
# checked that until now — which is how a renamed picker method shipped, and
# what settings.py's "the registry can never promise a field the XML does not
# have" was asserting without enforcing.
# ---------------------------------------------------------------------------

def _device_config_fields(device_type_id):
    """{field id: <Field> element} for one <Device>'s ConfigUI."""
    root = ET.parse(SERVER_PLUGIN / "Devices.xml").getroot()
    for device in root.findall("Device"):
        if device.get("id") != device_type_id:
            continue
        config = device.find("ConfigUI")
        return {f.get("id"): f for f in (config.findall("Field") if config is not None else [])}
    raise AssertionError(f"no <Device id={device_type_id!r}> in Devices.xml")


def _device_state_ids(device_type_id):
    root = ET.parse(SERVER_PLUGIN / "Devices.xml").getroot()
    for device in root.findall("Device"):
        if device.get("id") != device_type_id:
            continue
        states = device.find("States")
        return {s.get("id") for s in (states.findall("State") if states is not None else [])}
    raise AssertionError(f"no <Device id={device_type_id!r}> in Devices.xml")


def _declared_settings():
    from matter_handlers.settings import SETTINGS
    return SETTINGS


def test_every_declared_setting_has_its_configui_field(plugin_cls):
    from device_settings import _marker_field
    for setting in _declared_settings():
        for type_id in sorted(setting.device_types):
            fields = _device_config_fields(type_id)
            assert setting.key in fields, \
                f"{type_id} declares setting {setting.key!r} with no <Field id={setting.key!r}>"
            marker = _marker_field(setting)
            assert marker in fields, \
                f"{type_id}: setting {setting.key!r} needs its hidden marker <Field id={marker!r}>"
            assert fields[setting.key].get("visibleBindingId") == marker, \
                f"{type_id}: {setting.key!r} must be gated on {marker!r}"


def test_every_declared_setting_has_its_state(plugin_cls):
    """The dialog seeds current values from device STATE, so a setting with no
    state can never show what the device holds."""
    for setting in _declared_settings():
        for type_id in sorted(setting.device_types):
            assert setting.key in _device_state_ids(type_id), \
                f"{type_id} declares setting {setting.key!r} but no <State id={setting.key!r}>"


def test_numeric_settings_have_a_range_field_and_choices_have_a_picker(plugin_cls):
    for setting in _declared_settings():
        for type_id in sorted(setting.device_types):
            fields = _device_config_fields(type_id)
            if setting.is_choice:
                field = fields[setting.key]
                assert field.get("type") == "menu", f"{setting.key} should be a menu"
                lst = field.find("List")
                assert lst is not None and lst.get("filter") == setting.key, \
                    f"{setting.key}'s <List> must carry filter={setting.key!r}"
            else:
                assert f"{setting.key}Range" in fields, \
                    f"{type_id}: numeric setting {setting.key!r} needs a {setting.key}Range field"


def test_no_orphan_setting_fields_in_the_xml(plugin_cls):
    """The other direction: an XML field with no declaration behind it renders
    as a control that does nothing."""
    from device_settings import _marker_field
    declared = {}
    for setting in _declared_settings():
        for type_id in setting.device_types:
            declared.setdefault(type_id, set()).update(
                {setting.key, _marker_field(setting)})
            if not setting.is_choice:
                declared[type_id].add(f"{setting.key}Range")
    for type_id, keys in declared.items():
        fields = set(_device_config_fields(type_id))
        settings_fields = {f for f in fields
                           if f.startswith("has") or f in keys or f.endswith("Range")}
        orphans = settings_fields - keys - {"hasAnySetting"}
        assert not orphans, f"{type_id} has settings fields with no declaration: {orphans}"
