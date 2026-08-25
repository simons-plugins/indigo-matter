"""ValveConfigurationAndControl cluster (0x0081) → Indigo Relay device.

Matter 1.3 water valves (Moen/Flo shut-off valves, irrigation valves, LinkTap)
use the ValveConfigurationAndControl cluster. This handler maps the CurrentState
attribute to an Indigo relay device so valves behave like any other on/off
device in scenes, triggers, and schedules.

Deferred scope (not implemented here):
  - "Open for duration" command (openDuration parameter) — needs an Actions.xml
    custom action; deferred until irrigation-use demand is confirmed.
  - Variable-flow valves (CurrentLevel/TargetLevel percent, feature 0x01) —
    deferred until a real device with that feature justifies a dimmer mapping.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand, base_props, node_endpoint

# Cluster identifier — ValveConfigurationAndControl (Matter 1.3 spec §9.6)
CLUSTER_VALVE = 0x0081

# Attributes
ATTR_CURRENT_STATE = 0x0004  # CurrentState: 0=Closed, 1=Open, 2=Transitioning, nullable

# CurrentState enum values
STATE_CLOSED = 0
STATE_OPEN = 1
STATE_TRANSITIONING = 2

# Command names (Matter spec §9.6.7)
CMD_OPEN = "Open"
CMD_CLOSE = "Close"


class ValveHandler(ClusterHandler):
    """Maps the ValveConfigurationAndControl cluster to an Indigo relay device.

    Semantics: Indigo ``onOffState = True`` means the valve is open;
    ``onOffState = False`` means closed. The custom ``valveState`` state carries
    the full three-value enum (open/closed/transitioning) for triggers and
    control pages.
    """

    cluster_id = CLUSTER_VALVE
    cluster_name = "ValveConfigurationAndControl"
    device_type_id = "matterValve"

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props=base_props(node, endpoint),
                initial_states={},  # valve state is unknown until first update
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [ATTR_CURRENT_STATE]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        """Translate a CurrentState change to Indigo state updates.

        State mapping:
          1 (Open)          → onOffState=True,  valveState="open"
          0 (Closed)        → onOffState=False, valveState="closed"
          2 (Transitioning) → valveState="transitioning"
                              onOffState deliberately keeps its last value —
                              Indigo relay state should reflect the last stable
                              position, not an intermediate motion, so callers
                              can still tell which way the valve was heading.
          None (nullable)   → {} — do not fabricate state from a null read;
                              let the device stay at its last known state.
        """
        if attribute_id != ATTR_CURRENT_STATE:
            return {}

        if value is None:
            return {}

        if value == STATE_OPEN:
            return {"onOffState": True, "valveState": "open"}
        if value == STATE_CLOSED:
            return {"onOffState": False, "valveState": "closed"}
        if value == STATE_TRANSITIONING:
            # onOffState not updated — preserves last stable state mid-motion
            return {"valveState": "transitioning"}

        # Unknown enum value — ignore rather than corrupt state
        return {}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        """Translate an Indigo device action to a Matter Open or Close command.

        Toggle resolves the current valve state from the device's ``onOffState``
        and sends the opposite command — same pattern as on_off.py.
        """
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id, endpoint_id = node_endpoint(indigo_dev)
        device_action = action.deviceAction

        if device_action == indigo.kDeviceAction.TurnOn:
            command = CMD_OPEN
        elif device_action == indigo.kDeviceAction.TurnOff:
            command = CMD_CLOSE
        elif device_action == indigo.kDeviceAction.Toggle:
            # Resolve toggle from current on/off state (relay pattern from on_off.py)
            currently_on = getattr(indigo_dev, "onState", None)
            if currently_on is None:
                return None  # never guess a water valve's direction from unknown state
            command = CMD_CLOSE if currently_on else CMD_OPEN
        else:
            return None

        return MatterCommand(
            node_id=node_id,
            endpoint=endpoint_id,
            cluster=CLUSTER_VALVE,
            command=command,
            args={},
        )
