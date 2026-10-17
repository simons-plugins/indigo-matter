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

    @property
    def shape_matches(self) -> bool:
        """True when every device on both nodes found a partner."""
        return not self.unmatched_old and not self.unmatched_new


def _key(ref: DeviceRef) -> tuple[int, str]:
    return (ref.endpoint_id, ref.type_id)


def plan_replacement(old_node: int, old_refs: list[DeviceRef],
                     new_node: int, new_refs: list[DeviceRef]) -> ReplacementPlan:
    """Pair devices on (endpoint_id, type_id); whatever is left over is unmatched.

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
    return ReplacementPlan(old_node, new_node, tuple(pairs), tuple(unmatched_old), tuple(unmatched_new))


def refusal_reasons(plan: ReplacementPlan, *, allow_mismatch: bool, exported_ids: set[int],
                    references: dict[int, list[str]], reference_errors: dict[int, str]) -> list[str]:
    """Why the replacement must not run; empty means go.

    Ordered most-fundamental first: same node, no pairs, shape mismatch, then
    per paired NEW device (they are about to be deleted) exported, referenced,
    and reference lookup failed. Unmatched new devices are kept, so their
    exports and references are irrelevant. A failed lookup refuses: an
    unanswered "is it referenced?" is not a "no".
    """
    reasons: list[str] = []
    if plan.old_node == plan.new_node:
        reasons.append("The old and new node are the same; pick a different node to sync from.")
    if not plan.pairs:
        reasons.append("No device on the new node matches a device on the old node (by endpoint and type).")
    if not plan.shape_matches and not allow_mismatch:
        reasons.append("The two nodes have different device shapes; tick 'replace anyway' to rebind only "
                       "the devices that match.")

    new_devices = [new for _old, new in plan.pairs]
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


def preview_rows(plan: ReplacementPlan, refusals: list[str]) -> list[tuple[str, str]]:
    """(value, label) rows for the read-only preview list.

    Indigo list values must be unique, so each row's value is a distinct tag.
    The last row states what the reference check cannot see.
    """
    rows: list[tuple[str, str]] = []
    for old, new in plan.pairs:
        rows.append((f"pair:{old.dev_id}", f"{old.name}  ←  {new.name} (deleted)"))
    for old in plan.unmatched_old:
        rows.append((f"old:{old.dev_id}", f"{old.name}  ←  (no match on the new node; left unchanged)"))
    for new in plan.unmatched_new:
        rows.append((f"new:{new.dev_id}", f"(no match on the old node)  {new.name}  (kept)"))
    for index, reason in enumerate(refusals):
        rows.append((f"refuse:{index}", f"REFUSED: {reason}"))
    rows.append(("note:blind-spot",
                 "Note: the reference check cannot see references inside plugin-action settings "
                 "(for example Lamplighter zones)."))
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
