"""M4: node parsing, registry composition, and the OnOff handler."""
from __future__ import annotations

import protocol
from matter_model import parse_node
from matter_handlers.base import IndigoDeviceSpec
from matter_handlers.basic_information import (
    ATTR_NODE_LABEL,
    ATTR_SW_VERSION_STRING,
    BasicInformationHandler,
    CLUSTER_BASIC_INFORMATION,
)
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


def test_parse_node_available_defaults_true_and_reads_flag():
    assert parse_node(RELAY_NODE).available is True            # omitted → reachable
    assert parse_node({**RELAY_NODE, "available": False}).available is False


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


# ---------------------------------------------------------------------------
# BasicInformationHandler (0x0028) — issue #204, ADR-0008
# ---------------------------------------------------------------------------

class _NodeDev:
    """Minimal Indigo device stub for BasicInformationHandler tests."""
    def __init__(self, states=None):
        self.states = states if states is not None else {"nodeLabel": "", "softwareVersion": ""}


def test_basic_information_cluster_id():
    assert BasicInformationHandler.cluster_id == CLUSTER_BASIC_INFORMATION == 0x0028


def test_basic_information_is_non_primary():
    h = BasicInformationHandler()
    assert h.is_primary_for(None, None) is False


def test_basic_information_creates_no_devices():
    h = BasicInformationHandler()
    assert h.create_indigo_devices(None, None) == []


def test_basic_information_device_type_id_is_matterNode():
    assert BasicInformationHandler.device_type_id == "matterNode"


def test_basic_information_not_node_scoped():
    # Unlike PowerSource: this cluster lives on the SAME endpoint (0) as the
    # device it describes, so ordinary same-endpoint dispatch is correct.
    assert BasicInformationHandler.node_scoped is False


def test_basic_information_subscribes_to_both_attrs():
    h = BasicInformationHandler()
    subs = h.attributes_to_subscribe()
    assert ATTR_NODE_LABEL in subs
    assert ATTR_SW_VERSION_STRING in subs


def test_node_label_maps_to_state():
    h = BasicInformationHandler()
    assert h.on_attribute_update(_NodeDev(), ATTR_NODE_LABEL, "Landing Presence") == {
        "nodeLabel": "Landing Presence"}


def test_sw_version_maps_to_state():
    h = BasicInformationHandler()
    assert h.on_attribute_update(_NodeDev(), ATTR_SW_VERSION_STRING, "1.2.3") == {
        "softwareVersion": "1.2.3"}


def test_sw_version_value_is_coerced_to_str():
    # matter-server could in principle hand back a non-string; the state is
    # declared String in Devices.xml, so the handler must not pass a raw type through.
    h = BasicInformationHandler()
    assert h.on_attribute_update(_NodeDev(), ATTR_SW_VERSION_STRING, 123) == {
        "softwareVersion": "123"}


def test_basic_information_unmapped_attribute_returns_empty():
    h = BasicInformationHandler()
    assert h.on_attribute_update(_NodeDev(), 0x0001, "Aqara") == {}  # VendorName — not subscribed


def test_basic_information_none_value_returns_empty():
    h = BasicInformationHandler()
    assert h.on_attribute_update(_NodeDev(), ATTR_NODE_LABEL, None) == {}
    assert h.on_attribute_update(_NodeDev(), ATTR_SW_VERSION_STRING, None) == {}


def test_basic_information_missing_state_guard():
    # A matterNode device that (hypothetically) lacks one of these states must
    # not be handed a key Indigo will reject — same idiom as every other
    # merge-in/state-guarded handler in this package.
    h = BasicInformationHandler()
    bare = _NodeDev(states={})
    assert h.on_attribute_update(bare, ATTR_NODE_LABEL, "x") == {}
    assert h.on_attribute_update(bare, ATTR_SW_VERSION_STRING, "1.0") == {}


def test_basic_information_handle_action_returns_none():
    h = BasicInformationHandler()
    assert h.handle_indigo_action(None, object()) is None


def test_basic_information_registered_in_default_handlers():
    reg = HandlerRegistry()
    handler = reg.handler_for_cluster(CLUSTER_BASIC_INFORMATION)
    assert isinstance(handler, BasicInformationHandler)


def test_basic_information_never_produces_specs_via_the_registry():
    """handlers_for_endpoint must never create a device through this handler
    (device_sync._ensure_node_device owns matterNode creation) — regression
    guard for the is_primary_for=False contract."""
    reg = HandlerRegistry()
    node = parse_node(RELAY_NODE, suggested_name="Office Plug")
    ep0 = _endpoint(node, 0)
    specs = reg.handlers_for_endpoint(node, ep0)
    assert not any(s.device_type_id == "matterNode" for s in specs)
