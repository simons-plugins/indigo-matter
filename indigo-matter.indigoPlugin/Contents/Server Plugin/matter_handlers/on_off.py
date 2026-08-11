"""OnOff cluster (0x0006) → Indigo Relay device.

If LevelControl (0x0008) is also present on the endpoint, the dimmer handler is
preferred and this handler defers (returns no device) — though it still owns
OnOff attribute updates and on/off commands as part of the dimmer in later
milestones. v1 (M4) only wires the relay path.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ENDPOINT_OWNER_CLUSTERS, ClusterHandler, IndigoDeviceSpec, MatterCommand
from .electrical import CLUSTER_ELECTRICAL_ENERGY, CLUSTER_ELECTRICAL_POWER
from .settings import ATTR_START_UP_ON_OFF

CLUSTER_LEVEL_CONTROL = 0x0008
CLUSTER_COLOR_CONTROL = 0x0300


class OnOffHandler(ClusterHandler):
    cluster_id = 0x0006
    cluster_name = "OnOff"
    device_type_id = "matterRelay"

    ATTR_ON_OFF = 0x0000
    CMD_OFF = "Off"
    CMD_ON = "On"
    CMD_TOGGLE = "Toggle"

    def is_primary_for(self, node: Any, endpoint: Any) -> bool:
        # A richer lighting handler (dimmer OR colour — a colour light is not
        # required to carry LevelControl) owns this endpoint; a rich actuator
        # cluster (fan/thermostat/covering/lock/valve) present → that handler
        # owns it and this OnOff is its subordinate power switch, not a
        # standalone relay (issue #58 — duplicate-device class).
        if endpoint.has(CLUSTER_LEVEL_CONTROL) or endpoint.has(CLUSTER_COLOR_CONTROL):
            return False
        return not any(endpoint.has(c) for c in ENDPOINT_OWNER_CLUSTERS)

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        if not self.is_primary_for(node, endpoint):
            return []
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        props: dict = {
            "nodeId": str(node.node_id),
            "endpointId": str(endpoint.endpoint_id),
            "vendorName": node.vendor_name,
            "productName": node.product_name,
        }
        # Energy support must be set as device props at creation: Indigo does not
        # apply static <Supports*> Devices.xml elements to API-created devices
        # (same lesson as colour support; issue #56). When these
        # props are True, Indigo automatically adds curEnergyLevel / accumEnergyTotal
        # states that ElectricalPowerHandler / ElectricalEnergyHandler then update.
        if endpoint.has(CLUSTER_ELECTRICAL_POWER):
            props["SupportsPowerMeter"] = True
        if endpoint.has(CLUSTER_ELECTRICAL_ENERGY):
            props["SupportsEnergyMeter"] = True
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props=props,
                initial_states={"onOffState": False},
            )
        ]

    #: Attribute id → Indigo state, for the writable Lighting-feature settings
    #: (issue #186). Subscribed so the Edit Device dialog can show the current
    #: value without a live read, and so a change made from another ecosystem
    #: still reaches Indigo. Conformance LT: a device that does not implement it
    #: simply never reports it, and the AttributeList gate keeps the field
    #: hidden. Subscribing to an attribute a device lacks costs nothing here
    #: because the plugin takes matter-server's whole start_listening firehose
    #: and these lists are only a future filtering aid (see matter_client) — no
    #: per-attribute subscription is actually issued.
    #:
    #: OnTime was here and was withdrawn with its setting (#197): a state that
    #: usually reads 0 — it only moves while another admin on the fabric has
    #: put the device into Timed On — is worse than no state, because it looks
    #: like an answer.
    SETTING_STATES = {
        ATTR_START_UP_ON_OFF: "startUpOnOff",
    }

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_ON_OFF, *self.SETTING_STATES]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == self.ATTR_ON_OFF:
            return {"onOffState": bool(value)}
        state_key = self.SETTING_STATES.get(attribute_id)
        if state_key is None or value is None:
            return {}
        try:
            number = int(value)
        except (TypeError, ValueError):
            # StartUpOnOff is nullable ("restore previous state") and a null
            # arrives as something unparseable. There is no integer that means
            # it, so the state is left alone rather than given a value the
            # device does not hold.
            return {}
        # Guard: relays fielded before #186 have no such state until Indigo
        # rebuilds their state list (deviceStartComm) — same reason as holdTime.
        if state_key not in getattr(indigo_dev, "states", {}):
            return {}
        return {state_key: number}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        device_action = action.deviceAction
        if device_action == indigo.kDeviceAction.TurnOn:
            command = self.CMD_ON
        elif device_action == indigo.kDeviceAction.TurnOff:
            command = self.CMD_OFF
        elif device_action == indigo.kDeviceAction.Toggle:
            command = self.CMD_TOGGLE
        else:
            return None
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id,
            cluster=self.cluster_id, command=command, args={},
        )
