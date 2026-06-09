"""M4: node parsing, registry composition, and the OnOff handler."""
from __future__ import annotations

import protocol
from matter_model import parse_node
from matter_handlers.base import IndigoDeviceSpec
from matter_handlers.registry import HandlerRegistry
from matter_handlers.on_off import OnOffHandler


# Real matter-server v0.6.2 node-details shape: a flat `attributes` map keyed by
# "endpoint/cluster/attribute" (decimal). There is NO `endpoints` key — endpoints
# and clusters are derived from the attribute paths. Endpoint 0 is the root.
RELAY_NODE = {
    "node_id": 42,
    "available": True,
    "is_bridge": False,
    "attributes": {
        "0/40/1": "TP-Link",            # BasicInformation.VendorName
        "0/40/2": 4488,                 # VendorID
        "0/40/3": "Tapo P125M",         # ProductName
        "0/40/4": 4660,                 # ProductID
        "0/40/10": "1.2.3",             # SoftwareVersionString
        "1/29/0": [{"0": 266, "1": 2}],  # Descriptor.DeviceTypeList (OnOffPlugInUnit=266)
        "1/6/0": False,                 # OnOff.OnOff → cluster 6 on endpoint 1
        "1/3/0": 0,                     # Identify
    },
}

DIMMER_NODE = {
    "node_id": 7,
    "attributes": {
        "1/29/0": [{"0": 257, "1": 2}],  # DimmableLight=257
        "1/6/0": False,                 # OnOff
        "1/8/0": 254,                   # LevelControl.CurrentLevel → cluster 8
        "1/3/0": 0,
    },
}


def _endpoint(node, endpoint_id):
    return next(ep for ep in node.endpoints if ep.endpoint_id == endpoint_id)


def test_parse_node_extracts_endpoints_and_identity():
    node = parse_node(RELAY_NODE, suggested_name="Office Plug")
    assert node.node_id == 42
    assert node.suggested_name == "Office Plug"
    assert node.vendor_name == "TP-Link"
    assert node.product_name == "Tapo P125M"
    assert node.vendor_id == 4488
    assert node.sw_version == "1.2.3"
    # root endpoint 0 + application endpoint 1, derived from attribute paths
    assert {ep.endpoint_id for ep in node.endpoints} == {0, 1}
    ep1 = _endpoint(node, 1)
    assert ep1.has(0x0006) and ep1.has(29)
    assert not ep1.has(0x0008)
    assert ep1.device_types == (266,)


def test_registry_creates_relay_for_onoff_only_endpoint():
    node = parse_node(RELAY_NODE, "Office Plug")
    reg = HandlerRegistry()
    specs = reg.handlers_for_endpoint(node, _endpoint(node, 1))
    assert len(specs) == 1
    spec = specs[0]
    assert isinstance(spec, IndigoDeviceSpec)
    assert spec.device_type_id == "matterRelay"
    assert spec.name == "Office Plug"
    assert spec.props["nodeId"] == "42"
    assert spec.props["endpointId"] == "1"
    assert spec.props["vendorName"] == "TP-Link"
    assert spec.initial_states == {"onOffState": False}


def test_onoff_plus_levelcontrol_makes_a_dimmer():
    node = parse_node(DIMMER_NODE, "Lamp")
    reg = HandlerRegistry()
    # OnOff defers to LevelControl → a single matterDimmer (not a relay)
    specs = reg.handlers_for_endpoint(node, _endpoint(node, 1))
    assert len(specs) == 1
    assert specs[0].device_type_id == "matterDimmer"


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
