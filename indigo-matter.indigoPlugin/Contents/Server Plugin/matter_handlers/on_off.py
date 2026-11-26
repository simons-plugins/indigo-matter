"""OnOff cluster (0x0006) → Indigo Relay device.

If LevelControl (0x0008) is also present on the endpoint, the dimmer handler is
preferred and this handler defers (returns no device) — though it still owns
OnOff attribute updates and on/off commands as part of the dimmer in later
milestones. v1 (M4) only wires the relay path.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import (
    ENDPOINT_OWNER_CLUSTERS,
    ClusterHandler,
    IndigoDeviceSpec,
    MatterCommand,
    base_props,
    node_endpoint,
    toggle_command_name,
)
from .electrical import meter_props
from .settings import ATTR_START_UP_ON_OFF, CLUSTER_ON_OFF, SETTINGS

CLUSTER_LEVEL_CONTROL = 0x0008
CLUSTER_COLOR_CONTROL = 0x0300


class OnOffHandler(ClusterHandler):
    cluster_id = 0x0006
    cluster_name = "OnOff"
    device_type_id = "matterRelay"

    ATTR_ON_OFF = 0x0000
    CMD_OFF = "Off"
    CMD_ON = "On"

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
        props: dict = base_props(node, endpoint)
        # See electrical.meter_props for why these must be set at creation.
        props.update(meter_props(endpoint))
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

    #: Settings on this cluster, by attribute id — for their null handling.
    _SETTINGS_BY_ATTRIBUTE = {s.attribute: s for s in SETTINGS if s.cluster == CLUSTER_ON_OFF}

    #: A null on these is an answer (the setting declares what it means), so
    #: ``device_sync`` lets it through; a null OnOff itself is still dropped (#374).
    accepts_null_attributes = frozenset(
        a for a, s in _SETTINGS_BY_ATTRIBUTE.items() if s.null_state is not None)

    def attributes_to_subscribe(self) -> list[int]:
        return [self.ATTR_ON_OFF, *self.SETTING_STATES]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id == self.ATTR_ON_OFF:
            return {"onOffState": bool(value)}
        state_key = self.SETTING_STATES.get(attribute_id)
        if state_key is None:
            return {}
        if value is None:
            # A null reading ("restore the previous state") is a real answer,
            # recorded as the setting's declared null state — dropping it left
            # Indigo's default 0 ("Off") showing for it. A setting that does not
            # declare one keeps the old behaviour: nothing is written.
            setting = self._SETTINGS_BY_ATTRIBUTE.get(attribute_id)
            if setting is None or setting.null_state is None:
                return {}
            # Same fielded-device guard as below.
            if state_key not in getattr(indigo_dev, "states", {}):
                return {}
            return {state_key: setting.null_state}
        try:
            number = int(value)
        except (TypeError, ValueError):
            # StartUpOnOff is nullable ("restore previous state") and a null
            # arrives as something unparseable. There is no integer that means
            # it, so the state is left alone rather than given a value the
            # device does not hold.
            return {}
        # Guard: relays fielded before #186 (and dimmers before #360) have no such
        # state until Indigo
        # rebuilds their state list (deviceStartComm) — same reason as holdTime.
        if state_key not in getattr(indigo_dev, "states", {}):
            return {}
        return {state_key: number}

    def format_kv(self, states: dict) -> list:
        """Attach the null state's display string ("previous state")."""
        by_key = {s.key: s for s in self._SETTINGS_BY_ATTRIBUTE.values()}
        return [by_key[key].state_entry(value) if key in by_key
                else {"key": key, "value": value} for key, value in states.items()]

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id, endpoint_id = node_endpoint(indigo_dev)
        device_action = action.deviceAction
        if device_action == indigo.kDeviceAction.TurnOn:
            command = self.CMD_ON
        elif device_action == indigo.kDeviceAction.TurnOff:
            command = self.CMD_OFF
        elif device_action == indigo.kDeviceAction.Toggle:
            command = toggle_command_name(indigo_dev)
        else:
            return None
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id,
            cluster=self.cluster_id, command=command, args={},
        )
