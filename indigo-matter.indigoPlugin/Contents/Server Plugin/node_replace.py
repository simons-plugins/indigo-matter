"""Pure planner for "Replace a Matter node" (issue #364).

When a device is factory-reset and paired again, Matter hands it a new node id
and the plugin builds a fresh set of Indigo devices for it. The replace flow
repoints the OLD devices (which everything references by Indigo id) at the new
node and deletes the new duplicates. This module decides *what pairs with
what*, *whether to refuse*, and *what to show*; it never touches Indigo.

Kept free of ``indigo``, I/O and matter-server so every rule is unit-testable
with plain ``DeviceRef`` values. ``DeviceSync`` owns the ordering-sensitive
mutation (delete duplicates first, rebind last); see
docs/plans/issue-364-replace-node.md for the two traps that ordering avoids.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from matter_model import node_id_to_str

__all__ = ["DeviceRef", "ReplacementPlan", "plan_replacement", "refusal_reasons", "preview_rows", "rebound_props"]


@dataclass(frozen=True)
class DeviceRef:
    """One Indigo device this plugin owns, as the planner needs to see it."""
    dev_id: int
    endpoint_id: int
    type_id: str
    name: str


@dataclass(frozen=True)
class ReplacementPlan:
    """Old devices paired with their new twins, by (endpoint, device type)."""
    old_node: int
    new_node: int
    pairs: tuple[tuple[DeviceRef, DeviceRef], ...]   # (old, new), sorted by (endpoint_id, type_id)
    unmatched_old: tuple[DeviceRef, ...]
    unmatched_new: tuple[DeviceRef, ...]
    #: New-node devices deleted although unpaired (D8): the new node's matterNode when
    #: the old node's was removed by hand. Checked and previewed like paired new devices.
    extra_deletes: tuple[DeviceRef, ...] = ()

    @property
    def shape_matches(self) -> bool:
        """True when every device on both nodes found a partner."""
        return not self.unmatched_old and not self.unmatched_new


def _key(ref: DeviceRef) -> tuple[int, str]:
    return (ref.endpoint_id, ref.type_id)


def plan_replacement(old_node: int, old_refs: list[DeviceRef],
                     new_node: int, new_refs: list[DeviceRef],
                     drop_new_node_device: bool = False) -> ReplacementPlan:
    """Pair devices on (endpoint_id, type_id); whatever is left over is unmatched.

    ``drop_new_node_device`` (D8: the old node's node device was removed by hand):
    an UNPAIRED new matterNode moves from ``unmatched_new`` to ``extra_deletes``,
    so it is deleted, is subject to the export/reference checks, and its absence on
    the old side is not a shape mismatch.

    If two devices on one side share a key (not expected), they pair in dev_id
    order and the surplus is unmatched, so the result is deterministic.
    """
    new_by_key: dict[tuple[int, str], list[DeviceRef]] = {}
    for ref in sorted(new_refs, key=lambda r: (_key(r), r.dev_id)):
        new_by_key.setdefault(_key(ref), []).append(ref)

    pairs: list[tuple[DeviceRef, DeviceRef]] = []
    unmatched_old: list[DeviceRef] = []
    for old in sorted(old_refs, key=lambda r: (_key(r), r.dev_id)):
        candidates = new_by_key.get(_key(old))
        if candidates:
            pairs.append((old, candidates.pop(0)))
        else:
            unmatched_old.append(old)

    unmatched_new = sorted((r for rest in new_by_key.values() for r in rest),
                           key=lambda r: (_key(r), r.dev_id))
    extra_deletes: list[DeviceRef] = []
    if drop_new_node_device:
        extra_deletes = [r for r in unmatched_new if r.endpoint_id == 0 and r.type_id == "matterNode"]
        unmatched_new = [r for r in unmatched_new if r not in extra_deletes]
    return ReplacementPlan(old_node, new_node, tuple(pairs), tuple(unmatched_old), tuple(unmatched_new),
                           tuple(extra_deletes))


def refusal_reasons(plan: ReplacementPlan, *, allow_mismatch: bool, exported_ids: set[int],
                    references: dict[int, list[str]], reference_errors: dict[int, str]) -> list[str]:
    """Why the replacement must not run; empty means go.

    Ordered most-fundamental first: same node, no pairs, a device on both sides, shape mismatch, then per NEW
    device about to be deleted (paired, or ``extra_deletes``) exported, referenced,
    and reference lookup failed. Unmatched new devices are kept, so their
    exports and references are irrelevant. A failed lookup refuses: an
    unanswered "is it referenced?" is not a "no".
    """
    reasons: list[str] = []
    if plan.old_node == plan.new_node:
        reasons.append("The old and new node are the same; pick a different node to sync from.")
    if not plan.pairs:
        reasons.append("No device on the new node matches a device on the old node (by endpoint and type).")
    old_ids = {old.dev_id for old, _new in plan.pairs} | {ref.dev_id for ref in plan.unmatched_old}
    new_ids = ({new.dev_id for _old, new in plan.pairs} | {ref.dev_id for ref in plan.unmatched_new}
               | {ref.dev_id for ref in plan.extra_deletes})
    if old_ids & new_ids:
        # A stale index can list one device under both nodes; deleting "the duplicate" would
        # delete the device being kept.
        reasons.append("An Indigo device appears on both nodes (a stale lookup); "
                       "restart the plugin or run a resync, then try again.")
    if not plan.shape_matches and not allow_mismatch:
        reasons.append("The two nodes have different device shapes; tick 'replace anyway' to rebind only "
                       "the devices that match.")

    new_devices = [new for _old, new in plan.pairs] + list(plan.extra_deletes)
    for new in new_devices:
        if new.dev_id in exported_ids:
            reasons.append(f'"{new.name}" is exported as a Matter accessory and would be deleted. '
                           'Move its accessory with "Migrate an exported accessory…" first.')
    for new in new_devices:
        referrers = references.get(new.dev_id)
        if referrers:
            reasons.append(f'"{new.name}" is referenced by: {", ".join(referrers)}. '
                           "Resolve those references first, or delete the device by hand.")
    for new in new_devices:
        error = reference_errors.get(new.dev_id)
        if error:
            reasons.append(f'Could not check whether "{new.name}" is referenced ({error}); '
                           "refusing rather than guessing.")
    return reasons


#: Short role names for the preview rows. Mirrors ``device_sync._ROLE_LABELS`` (not importable
#: here: device_sync imports ``indigo``) plus the node device, which that map leaves out.
_ROLE_LABELS = {
    "matterNode": "Node device",
    "matterRelay": "Switch",
    "matterDimmer": "Dimmer",
    "matterColorDimmer": "Light",
    "matterTemperatureSensor": "Temperature",
    "matterHumiditySensor": "Humidity",
    "matterMotionSensor": "Motion",
    "matterContactSensor": "Contact",
    "matterIlluminanceSensor": "Illuminance",
    "matterPressureSensor": "Pressure",
    "matterFlowSensor": "Flow",
    "matterThermostat": "Thermostat",
    "matterFan": "Fan",
    "matterWindowCovering": "Window Covering",
    "matterLock": "Lock",
    "matterValve": "Valve",
    "matterButton": "Button",
    "matterSmokeCOAlarm": "Smoke/CO",
    "matterAirQualitySensor": "Air Quality",
    "matterCO2Sensor": "CO₂",
    "matterPM25Sensor": "PM2.5",
    "matterTVOCSensor": "TVOC",
    "matterEnergyMeter": "Energy",
    "matterUnknown": "Unsupported",
}


def _role(ref: DeviceRef) -> str:
    """Short role label for a device's Indigo type; the bare type id when unknown."""
    return _ROLE_LABELS.get(ref.type_id, ref.type_id)


def preview_rows(plan: ReplacementPlan, refusals: list[str]) -> list[tuple[str, str]]:
    """(value, label) rows for the read-only preview list.

    The pickers above already show the device names, so a row names the part
    (its role) and the outcome. Indigo list values must be unique, so each row's
    value is a distinct tag. Refusals collapse to ONE short row: the full reasons
    come from the Replace click's error.
    """
    rows: list[tuple[str, str]] = []
    if refusals:
        rows.append(("refuse", "✗ Can't replace: click Replace for the reason"))
        return rows
    for old, _new in plan.pairs:
        rows.append((f"pair:{old.dev_id}", f"{_role(old)} → {node_id_to_str(plan.new_node)}"))
    for old in plan.unmatched_old:
        rows.append((f"old:{old.dev_id}", f"{_role(old)}: no match, stays"))
    for new in plan.unmatched_new:
        rows.append((f"new:{new.dev_id}", f"{_role(new)} (new): kept"))
    for new in plan.extra_deletes:
        rows.append((f"extra:{new.dev_id}", f"{_role(new)} (new): deleted"))
    return rows


def rebound_props(old_props: dict[str, Any], new_node: int, *, software_version: Optional[str]) -> dict[str, Any]:
    """Copy of ``old_props`` pointed at ``new_node``; everything else is kept.

    ``softwareVersion`` is written only when given (the caller passes it for a
    matterNode device alone), so other devices never gain the key.
    """
    out = dict(old_props)
    out["nodeId"] = str(new_node)
    out["address"] = node_id_to_str(new_node)
    if software_version is not None:
        out["softwareVersion"] = software_version
    return out
