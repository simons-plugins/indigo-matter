"""Generic Switch cluster (0x003B) → Indigo button sensor device.

Matter buttons and scene controllers (IKEA SOMRIG/RODRET, Aqara wireless
buttons, Tuya scene switches) expose the Switch cluster. Button presses arrive
as *cluster events*, not attribute updates, so this handler relies on the
``on_node_event`` path introduced alongside it; ``on_attribute_update`` handles
the ``CurrentPosition`` attribute (subscribed for completeness, but transient —
no stable state to map in v1).

Matter spec refs:
  GenericSwitch cluster 0x003B, Matter 1.2 §1.12
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

# Event ids (Matter 1.2 §1.12.6)
EVT_INITIAL_PRESS       = 0x01
EVT_LONG_PRESS          = 0x02
EVT_SHORT_RELEASE       = 0x03
EVT_MULTI_PRESS_COMPLETE = 0x06

# FeatureMap (global attribute 0xFFFC) and the Switch feature bits it carries
# (Matter 1.2 §1.12.4). Which events a switch can EVER emit is decided here and
# nowhere else — the Events table's Conformance column (§1.12.6) gates each one
# on a feature.
ATTR_FEATURE_MAP              = 0xFFFC
FEATURE_LATCHING_SWITCH       = 0x01  # LS  — SwitchLatched
FEATURE_MOMENTARY_SWITCH      = 0x02  # MS  — InitialPress
FEATURE_MOMENTARY_RELEASE     = 0x04  # MSR — ShortRelease
FEATURE_MOMENTARY_LONG_PRESS  = 0x08  # MSL — LongPress, LongRelease
FEATURE_MOMENTARY_MULTI_PRESS = 0x10  # MSM — MultiPressOngoing, MultiPressComplete

#: The features whose events END a press. A momentary switch declaring NONE of
#: them emits InitialPress and nothing else, ever — so for those devices
#: InitialPress is not a leading edge to wait past, it is the whole press.
_TERMINAL_PRESS_FEATURES = (FEATURE_MOMENTARY_RELEASE
                            | FEATURE_MOMENTARY_LONG_PRESS
                            | FEATURE_MOMENTARY_MULTI_PRESS)

#: Device prop holding the endpoint's Switch FeatureMap, stamped at creation and
#: healed onto older devices by ``DeviceSync._reassert_capability_props``. Stored
#: as a string, like every other numeric prop this plugin writes (``nodeId``,
#: ``endpointId``) — Indigo round-trips prop values as text.
PROP_SWITCH_FEATURES = "switchFeatureMap"

#: The Indigo device type this handler owns. Named here so device_sync can gate
#: the prop heal on it without importing the class.
DEVICE_TYPE_BUTTON = "matterButton"


def switch_features(node: Any, endpoint: Any) -> Optional[int]:
    """The Switch FeatureMap for ``endpoint``, or None if the node has not
    reported it yet (ADR-0003: absence of evidence is not evidence)."""
    value = node.attributes.get(
        (int(endpoint.endpoint_id), CLUSTER_SWITCH, ATTR_FEATURE_MAP))
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def maps_initial_press(features: Optional[int]) -> bool:
    """Whether InitialPress is this switch's ONLY press signal.

    True only for a momentary switch (MS) declaring no release, long-press or
    multi-press feature. Matter 1.2 §1.12.7.3 ("Supports InitialPress (but not
    LongPress, ShortRelease and LongRelease)") is normative for this shape: such
    a switch "SHALL generate a single InitialPress event for one interaction
    cycle" and "SHALL NOT generate any of the ShortRelease, LongPress and
    LongRelease events". An unknown
    FeatureMap answers False: the safe default is the pre-#231 behaviour
    (suppress), since mapping InitialPress on a switch that ALSO emits
    ShortRelease double-counts every press — the exact regression issue #76
    fixed.
    """
    if features is None:
        return False
    return bool(features & FEATURE_MOMENTARY_SWITCH) and not (features & _TERMINAL_PRESS_FEATURES)

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
      InitialPress        → {} on a switch that emits a terminal event
                            (too noisy; it fires at the leading edge before we
                            know whether it will become short/long/multi — we
                            let the terminal events carry the meaningful label).
                            On an MS-ONLY switch — one whose FeatureMap
                            declares no MSR/MSL/MSM — there IS no terminal
                            event, so InitialPress is the press and maps to
                            "shortPress" (issue #231).

    ``pressCount`` increments on every mapped event even when ``lastButtonEvent``
    repeats the same string, because Indigo triggers fire only on state *change*.
    A counter that always advances guarantees triggers fire on every button press.
    """

    cluster_id     = CLUSTER_SWITCH
    cluster_name   = "GenericSwitch"
    device_type_id = DEVICE_TYPE_BUTTON
    # With BOTH Supports* False, Indigo falls back to the Devices.xml
    # <UiDisplayStateId> (lastButtonEvent) even for API-created devices —
    # the props-driven built-in display only takes precedence when one of
    # these is True. Verified live on jarvis (issue #56 follow-up).
    display_props = {"SupportsOnState": False, "SupportsSensorValue": False}

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter {node.node_id}"
        features = switch_features(node, endpoint)
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props={
                    "nodeId":      str(node.node_id),
                    "endpointId":  str(endpoint.endpoint_id),
                    "vendorName":  node.vendor_name,
                    "productName": node.product_name,
                    # Which events this switch can emit at all (issue #231) —
                    # on_node_event has only the Indigo device to go on, so the
                    # answer has to be carried on it. Omitted, not guessed,
                    # when the node has not reported the FeatureMap yet; the
                    # reconcile heal adds it once it has.
                    # Truthy, not "is not None", so this matches
                    # DeviceSync._capability_props exactly — a heal that
                    # disagreed with creation about the zero case would write a
                    # prop on the first reconcile of every button. A Switch
                    # cluster always declares at least one feature, so 0 means
                    # "not reported" either way.
                    **({PROP_SWITCH_FEATURES: str(features)} if features else {}),
                    **self.display_props,
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
            # On a switch that emits a terminal event, InitialPress fires at
            # the leading edge, before we know whether it will be a
            # short/long/multi press — too noisy to expose as a trigger-worthy
            # event, so we discard it and wait for the terminal one
            # (ShortRelease, LongPress, MultiPressComplete).
            #
            # But a momentary switch declaring NO terminal feature never sends
            # one (§1.12.6 gates each event on its feature), so
            # waiting for it means waiting forever: the Indigo device sits at
            # its creation state for the life of the install and the button
            # looks dead. That is issue #231 — an Aqara Light Switch H2 whose
            # two wireless gangs report FeatureMap 0x02, MS and nothing else.
            # For those, InitialPress IS the press.
            if not self._maps_initial_press(indigo_dev):
                return {}
            # "shortPress", not a new label: such a switch cannot tell short
            # from long, so this is the only press it has — and reusing the
            # existing vocabulary keeps every trigger, control page and doc
            # written against these devices working unchanged.
            label = "shortPress"
        elif event_id == EVT_SHORT_RELEASE:
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
                # count ≤ 1 is the NORMAL close of a single press on a
                # multi-press-capable switch, not an anomaly: the device
                # already emitted ShortRelease for it ~0.5s earlier and this
                # handler already counted that (issue #76 — live BILRESA
                # evidence: every clean single click emits the pair, so
                # treating this as a second shortPress double-bumped
                # pressCount on every press). ShortRelease stays the instant
                # signal (it is what makes scrolling feel live); this event
                # only carries new information when it reports a genuine
                # multi-count.
                return {}
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

    @staticmethod
    def _maps_initial_press(indigo_dev: Any) -> bool:
        """Read the device's stamped FeatureMap and ask :func:`maps_initial_press`.

        Tolerant of the prop being absent (a device created before #231, until
        the next reconcile heals it) or unparseable — both answer "no", which
        is the pre-#231 behaviour, never a double-count.

        Deliberately does NOT guard the ``pluginProps`` read: ``DeviceSync``'s
        ``_on_node_event`` already wraps this whole call in an ``except
        Exception`` that logs device, endpoint, cluster and event id. Catching
        here would convert a logged fault — a deleted device, a bridge error, a
        refactor passing something that is not an Indigo device — into an
        unlogged wrong answer that looks exactly like "this switch is not
        momentary-only".
        """
        raw = (indigo_dev.pluginProps or {}).get(PROP_SWITCH_FEATURES)
        if raw in (None, ""):
            return False
        try:
            return maps_initial_press(int(raw))
        except (TypeError, ValueError):
            return False

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        # GenericSwitch is input-only — buttons have no actionable commands.
        return None
