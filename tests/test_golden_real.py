"""Golden regression tests from REAL matter-server v0.6.2 frames.

Captured on 2026-06-09 from matter-server running on jarvis with a commissioned
matter.js OnOff device (node 1). These are verbatim wire payloads — if the
plugin's parsing ever drifts from the real server shape, these fail.

Source: `~/matter-probe.js` output (server_info, get_node, attribute_updated).
"""
from __future__ import annotations

import protocol
from protocol import Protocol
from matter_model import parse_node
from matter_handlers.registry import HandlerRegistry

# Real server_info handshake frame (bare object, pushed on connect).
REAL_SERVER_INFO = {
    "fabric_id": 1,
    "compressed_fabric_id": 10869497856935696000,
    "fabric_index": 1,
    "schema_version": 11,
    "min_supported_schema_version": 11,
    "sdk_version": "matter-server/0.6.2 (matter.js/0.17.0-alpha.0-20260426-5bf9dab53)",
    "wifi_credentials_set": False,
    "thread_credentials_set": False,
    "bluetooth_enabled": False,
}

# Real node-details object (faithful subset of the captured get_node result:
# the BasicInformation identity on endpoint 0 and the OnOff application on
# endpoint 1, plus the cluster/device-type descriptors). Note: NO `endpoints`
# key — only the flat `attributes` map.
REAL_NODE = {
    "node_id": 1,
    "available": True,
    "is_bridge": False,
    "attributes": {
        # endpoint 0 — RootNode + BasicInformation (cluster 40)
        "0/40/1": "matter-node.js",          # VendorName
        "0/40/2": 65521,                     # VendorID
        "0/40/3": "node-matter OnOff Light",  # ProductName
        "0/40/4": 32768,                     # ProductID
        "0/40/10": "0",                      # SoftwareVersionString
        "0/29/0": [{"0": 22, "1": 3}],        # Descriptor DeviceTypeList (RootNode=22)
        "0/29/1": [40, 31, 63, 48, 60, 62, 51, 29],
        # endpoint 1 — OnOff application
        "1/3/0": 0,                          # Identify
        "1/4/0": 128,                        # Groups
        "1/6/0": False,                      # OnOff.OnOff
        "1/6/16384": True,                   # OnOff.GlobalSceneControl (feature attr)
        "1/29/0": [{"0": 256, "1": 3}],       # DeviceTypeList (OnOff Light=256)
        "1/29/1": [3, 4, 6, 29],             # ServerList
    },
}

# Real attribute_updated event after toggling the device ON.
REAL_ATTRIBUTE_UPDATED = {"event": "attribute_updated", "data": [1, "1/6/0", True]}


def test_real_server_info_fields():
    # the fields _status_body relies on
    assert REAL_SERVER_INFO["sdk_version"].startswith("matter-server/0.6.2")
    assert REAL_SERVER_INFO["fabric_id"] == 1
    assert REAL_SERVER_INFO["bluetooth_enabled"] is False


def test_real_node_parses_to_relay():
    node = parse_node(REAL_NODE)
    assert node.node_id == 1
    assert node.vendor_name == "matter-node.js"
    assert node.product_name == "node-matter OnOff Light"
    assert node.vendor_id == 65521
    assert node.product_id == 32768

    by_id = {e.endpoint_id: e for e in node.endpoints}
    assert set(by_id) == {0, 1}
    assert by_id[1].has(0x0006) and not by_id[1].has(0x0008)
    assert by_id[1].device_types == (256,)

    specs = HandlerRegistry().handlers_for_endpoint(node, by_id[1])
    assert len(specs) == 1
    assert specs[0].device_type_id == "matterRelay"
    assert specs[0].props["nodeId"] == "1"
    assert specs[0].props["endpointId"] == "1"
    assert specs[0].props["vendorName"] == "matter-node.js"


def test_real_attribute_updated_maps_to_onoff_state():
    evt = Protocol().parse_event(REAL_ATTRIBUTE_UPDATED)
    assert evt.kind == protocol.EVT_ATTRIBUTE_UPDATED
    assert (evt.node_id, evt.endpoint, evt.cluster, evt.attribute) == (1, 1, 6, 0)
    assert evt.value is True

    handler = HandlerRegistry().handler_for_cluster(evt.cluster)
    assert handler.on_attribute_update(None, evt.attribute, evt.value) == {"onOffState": True}
