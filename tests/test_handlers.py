"""M4: node parsing, registry composition, and the OnOff handler."""
from __future__ import annotations

import protocol
from matter_model import parse_node
from matter_handlers.base import IndigoDeviceSpec
from matter_handlers.registry import HandlerRegistry
from matter_handlers.on_off import OnOffHandler


# Raw get_node shapes (IMPLEMENTATION §2.5)
RELAY_NODE = {
    "node_id": 42,
    "attributes": {
        "0/0x0028/0x0001": "TP-Link",      # VendorName
        "0/0x0028/0x0003": "Tapo P125M",   # ProductName
        "0/0x0028/0x0002": 4488,           # VendorID
        "0/0x0028/0x0004": 4660,           # ProductID
        "1/0x0006/0x0000": False,
    },
    "endpoints": {
        "1": {"device_types": [{"device_type": 266, "revision": 2}], "clusters": [6, 29, 3]},
    },
}

DIMMER_NODE = {
    "node_id": 7,
    "attributes": {},
    "endpoints": {
        "1": {"device_types": [], "clusters": [6, 8, 29]},  # OnOff + LevelControl
    },
}


def test_parse_node_extracts_endpoints_and_identity():
    node = parse_node(RELAY_NODE, suggested_name="Office Plug")
    assert node.node_id == 42
    assert node.suggested_name == "Office Plug"
    assert node.vendor_name == "TP-Link"
    assert node.product_name == "Tapo P125M"
    assert node.vendor_id == 4488
    assert len(node.endpoints) == 1
    ep = node.endpoints[0]
    assert ep.endpoint_id == 1
    assert ep.has(0x0006) and ep.has(29)
    assert not ep.has(0x0008)


def test_registry_creates_relay_for_onoff_only_endpoint():
    node = parse_node(RELAY_NODE, "Office Plug")
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, node.endpoints[0])
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, IndigoDeviceSpec)
    assert spec.device_type_id == "matterRelay"
    assert spec.name == "Office Plug"
    assert spec.props["nodeId"] == "42"
    assert spec.props["endpointId"] == "1"
    assert spec.props["vendorName"] == "TP-Link"
    assert spec.initial_states == {"onOffState": False}


def test_onoff_defers_when_levelcontrol_present():
    node = parse_node(DIMMER_NODE, "Lamp")
    reg = HandlerRegistry()
    # OnOff defers to a dimmer handler; none wired in M4 → no device produced
    specs = reg.handlers_for_endpoint(node, node.endpoints[0])
    assert specs == []


def test_registry_lookup_by_cluster_and_device_type():
    reg = HandlerRegistry()
    assert isinstance(reg.handler_for_cluster(0x0006), OnOffHandler)
    assert reg.handler_for_cluster(0x9999) is None

    class Dev:
        deviceTypeId = "matterRelay"
    assert isinstance(reg.handler_for_device(Dev()), OnOffHandler)


def test_onoff_attribute_update_maps_to_state():
    handler = OnOffHandler()
    assert handler.on_attribute_update(None, 0x0000, True) == {"onOffState": True}
    assert handler.on_attribute_update(None, 0x0000, 0) == {"onOffState": False}
    assert handler.on_attribute_update(None, 0x0099, True) == {}


def test_onoff_handle_action_builds_matter_command(mock_indigo_base):
    # mock_indigo_base provides indigo.kDeviceAction.* as distinct MagicMock attrs
    import indigo
    handler = OnOffHandler()

    class Dev:
        pluginProps = {"nodeId": "42", "endpointId": "1"}

    class Action:
        deviceAction = indigo.kDeviceAction.TurnOn

    cmd = handler.handle_indigo_action(Dev(), Action())
    assert cmd.node_id == 42 and cmd.endpoint == 1
    assert cmd.cluster == 0x0006 and cmd.command == "On"

    Action.deviceAction = indigo.kDeviceAction.TurnOff
    assert handler.handle_indigo_action(Dev(), Action()).command == "Off"

    Action.deviceAction = indigo.kDeviceAction.Toggle
    assert handler.handle_indigo_action(Dev(), Action()).command == "Toggle"

    # an unmapped action yields no command
    Action.deviceAction = indigo.kDeviceAction.SetBrightness
    assert handler.handle_indigo_action(Dev(), Action()) is None
