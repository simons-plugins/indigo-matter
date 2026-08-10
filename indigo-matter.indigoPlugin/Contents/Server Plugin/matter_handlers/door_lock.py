"""DoorLock cluster (0x0101) → Indigo Relay device.

Matter DoorLock maps to an Indigo relay where **on = locked** and **off = unlocked**.
This gives lock/unlock buttons, triggers, and control-page support natively.

A custom ``lockState`` string state (locked / unlocked / jammed) is maintained
alongside ``onOffState`` for finer trigger conditions.

Commands arrive as cluster invokes (same pattern as OnOff):

- LockDoor   (0x00) — no PIN required over our own trusted fabric
- UnlockDoor (0x01) — no PIN required over our own trusted fabric

The DoorLock cluster has no Toggle command, so Toggle is resolved from the
current Indigo device state rather than delegated to the device.
"""
from __future__ import annotations

from typing import Any, Optional

from .base import ClusterHandler, IndigoDeviceSpec, MatterCommand

CLUSTER_DOOR_LOCK = 0x0101

#: LockState attribute id (DoorLock cluster, mandatory, nullable).
ATTR_LOCK_STATE = 0x0000

#: LockState enum values (Matter spec §5.2.5.1):
#:   0 = NotFullyLocked, 1 = Locked, 2 = Unlocked; None = null (nullable attr).
_LOCK_STATE_LOCKED = 1
_LOCK_STATE_UNLOCKED = 2
_LOCK_STATE_NOT_FULLY_LOCKED = 0


class DoorLockHandler(ClusterHandler):
    cluster_id = CLUSTER_DOOR_LOCK
    cluster_name = "DoorLock"
    device_type_id = "matterLock"

    CMD_LOCK = "LockDoor"
    CMD_UNLOCK = "UnlockDoor"

    def create_indigo_devices(self, node: Any, endpoint: Any) -> list[IndigoDeviceSpec]:
        name = node.suggested_name or node.product_name or f"Matter Lock {node.node_id}"
        return [
            IndigoDeviceSpec(
                device_type_id=self.device_type_id,
                name=name,
                props={
                    "nodeId": str(node.node_id),
                    "endpointId": str(endpoint.endpoint_id),
                    "vendorName": node.vendor_name,
                    "productName": node.product_name,
                    # Indigo does not apply static Devices.xml <IsLockSubType> to
                    # API-created devices; set it here so the device gets the lock
                    # UI (Lock/Unlock buttons, triggers, control-page) instead of
                    # the generic switch UI — same pattern as ColorControlHandler
                    # sets SupportsColor/SupportsRGB in props (issue #56).
                    "IsLockSubType": True,
                },
                initial_states={},  # defer to first attribute_updated from matter-server
            )
        ]

    def attributes_to_subscribe(self) -> list[int]:
        return [ATTR_LOCK_STATE]

    def on_attribute_update(self, indigo_dev: Any, attribute_id: int, value: Any) -> dict:
        if attribute_id != ATTR_LOCK_STATE:
            return {}

        if value is None:
            # Nullable attribute — do NOT fabricate a state from a null value;
            # leaving the current Indigo state unchanged is safer than guessing.
            return {}

        # Coerce to int: matter-server may deliver the attribute as a float or
        # string (e.g. "1", 1.0) — same codebase-wide idiom as sensors.py and
        # thermostat.py use for every numeric attribute comparison.
        ivalue = int(value)

        if ivalue == _LOCK_STATE_LOCKED:
            return {"onOffState": True, "lockState": "locked"}

        if ivalue == _LOCK_STATE_UNLOCKED:
            return {"onOffState": False, "lockState": "unlocked"}

        # ivalue == _LOCK_STATE_NOT_FULLY_LOCKED (0) or any unexpected numeric value.
        # Treated as "not locked" (off) with a jammed indicator.
        # NOTE: surfacing jammed as an Indigo *error* state needs a handler-contract
        # change (setErrorStateOnServer) and is deliberately deferred.
        return {"onOffState": False, "lockState": "jammed"}

    def handle_indigo_action(self, indigo_dev: Any, action: Any) -> Optional[MatterCommand]:
        import indigo  # provided by the Indigo runtime (and the test mock)
        node_id = int(indigo_dev.pluginProps["nodeId"])
        endpoint_id = int(indigo_dev.pluginProps["endpointId"])
        device_action = action.deviceAction

        if device_action == indigo.kDeviceAction.Lock:
            # Lock sub-type devices receive Lock/Unlock from Indigo's lock UI.
            command = self.CMD_LOCK
        elif device_action == indigo.kDeviceAction.Unlock:
            command = self.CMD_UNLOCK
        elif device_action == indigo.kDeviceAction.TurnOn:
            # Alias for devices created before the lock sub-type fix, or generic
            # on/off actions fired from scripts/triggers (on = locked).
            command = self.CMD_LOCK
        elif device_action == indigo.kDeviceAction.TurnOff:
            # Alias: off = unlocked.
            command = self.CMD_UNLOCK
        elif device_action == indigo.kDeviceAction.Toggle:
            # Matter DoorLock has no Toggle command — resolve from current device state.
            # on = locked; if currently locked → unlock, else → lock.
            currently_locked = getattr(indigo_dev, "onState", False)
            command = self.CMD_UNLOCK if currently_locked else self.CMD_LOCK
        else:
            return None

        # No PIN argument — over our own trusted fabric the spec allows omitting the
        # optional PINCode field entirely; both LockDoor and UnlockDoor accept args={}.
        return MatterCommand(
            node_id=node_id, endpoint=endpoint_id,
            cluster=CLUSTER_DOOR_LOCK, command=command, args={},
        )
