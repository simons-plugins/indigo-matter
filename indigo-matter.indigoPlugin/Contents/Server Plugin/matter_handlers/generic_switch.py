"""Generic Switch cluster (0x003B) → Indigo button sensor device.

Matter buttons and scene controllers (IKEA SOMRIG/RODRET, Aqara wireless
buttons, Tuya scene switches) expose the Switch cluster. Button presses arrive
as *cluster events*, not attribute updates, so this handler relies on the
``on_node_event`` path introduced alongside it; ``on_attribute_update`` handles
the ``CurrentPosition`` attribute (subscribed for completeness, but transient —
no stable state to map in v1).

Matter spec refs:
  GenericSwitch cluster 0x003B, Matter 1.2 §1.13
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand

CLUSTER_SWITCH = 0x003B

# Attributes (subscribe CurrentPosition; NumberOfPositions and MultiPressMax
# are read-only config, not runtime state worth tracking in Indigo).
ATTR_NUMBER_OF_POSITIONS = 0x0000  # read-only config
ATTR_CURRENT_POSITION    = 0x0001  # transient — subscribed, but v1 maps nothing
ATTR_MULTI_PRESS_MAX     = 0x0002  # read-only config

# Event ids (Matter 1.2 §1.13.7)
EVT_INITIAL_PRESS       = 0x01
EVT_LONG_PRESS          = 0x02
EVT_SHORT_RELEASE       = 0x03
EVT_MULTI_PRESS_COMPLETE = 0x06

# Wire field name for MultiPressComplete event payload (verified from
# @matter/types/dist/esm/clusters/switch.d.ts and the camelCase conversion
# in @matter-server/ws-controller convertMatterToWebSocketNameBased).
_FIELD_TOTAL_PRESSES = "totalNumberOfPressesCounted"


class GenericSwitchHandler(ClusterHandler):
    """Maps the GenericSwitch cluster (0x003B) to an Indigo button sensor.

    One Indigo ``matterButton`` device per Switch endpoint (multi-gang = one
    endpoint each — the bridge hands each button as a separate endpoint; the
    per-endpoint naming and multi-endpoint suffix logic is handled centrally by
    ``DeviceSync.create_devices``).

    Attribute path: ``CurrentPosition`` is subscribed so matter-server opens
    the subscription, but the value is transient (it resets once the button
    is released), so v1 maps nothing from it — ``on_attribute_update`` returns
    ``{}``.

    Event path (``on_node_event``):
      ShortRelease        → lastButtonEvent = "shortPress"
      LongPress           → lastButtonEvent = "longPress"
      MultiPressComplete  → lastButtonEvent = "doublePress" / "triplePress" /
                            "multiPressN" (N = totalNumberOfPressesCounted)
      InitialPress        → {} (too noisy; fires at the leading edge before we
                            know whether it will become short/long/multi — we
                            let the terminal events carry the meaningful label).

    ``pressCount`` increments on every mapped event even when ``lastButtonEvent``
    repeats the same string, because Indigo triggers fire only on state *change*.
    A counter that always advances guarantees triggers fire on every button press.
    """

    cluster_id     = CLUSTER_SWITCH
    cluster_name   = "GenericSwitch"
    device_type_id = "matterButton"

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props={
                    "nodeId":      str(node.node_id),
                    "endpointId":  str(endpoint.endpoint_id),
                    "vendorName":  node.vendor_name,
                    "productName": node.product_name,
                },
                initial_states={
                    "lastButtonEvent": "",
                    "pressCount":      0,
                },
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        # Subscribe CurrentPosition so matter-server opens the subscription
        # channel and delivers events. NumberOfPositions/MultiPressMax are
        # static config — not worth subscribing.
        return [ATTR_CURRENT_POSITION]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        # CurrentPosition is transient (resets on release). No stable state to
        # surface in v1 — events carry the actionable signal.
        return {}

    def on_node_event(self, indigo_dev: Any, event_id: int, data: Any) -> dict:
        """Map a GenericSwitch cluster event to Indigo state updates.

        Returns ``{"lastButtonEvent": <label>, "pressCount": <n>}`` for
        meaningful events, or ``{}`` to suppress an event entirely.
        """
        if event_id == EVT_INITIAL_PRESS:
            # InitialPress fires at the leading edge, before we know whether
            # it will be a short/long/multi press — too noisy to expose as a
            # trigger-worthy event, so we silently discard it and wait for the
            # terminal event (ShortRelease, LongPress, MultiPressComplete).
            return {}

        if event_id == EVT_SHORT_RELEASE:
            label = "shortPress"
        elif event_id == EVT_LONG_PRESS:
            label = "longPress"
        elif event_id == EVT_MULTI_PRESS_COMPLETE:
            count = int((data or {}).get(_FIELD_TOTAL_PRESSES, 1))
            if count == 2:
                label = "doublePress"
            elif count == 3:
                label = "triplePress"
            elif count > 3:
                label = f"multiPress{count}"
            else:
                # count ≤ 1 from a MultiPressComplete is unexpected per spec
                # (that would be a ShortRelease); treat it as a short press.
                label = "shortPress"
        else:
            # Unknown event id — unsubscribed events (LongRelease, MultiPressOngoing)
            # or future cluster revisions. Return {} to ignore gracefully.
            return {}

        # pressCount increments on every mapped event (even when the label
        # repeats) so that Indigo's state-change triggers fire on every press.
        current_count = int(indigo_dev.states.get("pressCount", 0))
        return {
            "lastButtonEvent": label,
            "pressCount": current_count + 1,
        }

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        # GenericSwitch is input-only — buttons have no actionable commands.
        return None
