""""Replace a Matter node…" menu item (issue #364).

When a Matter device is factory-reset and paired again it gets a new node id, and
the plugin builds a fresh set of Indigo devices for it. Everything the user built
(triggers, schedules, control pages) points at the OLD Indigo devices, so this
dialog repoints those at the new node and deletes the new duplicates.

The decisions (what pairs with what, when to refuse, what the preview says) live
in :mod:`node_replace` and the ordering-sensitive mutation in
``DeviceSync.replace_node``; this mixin only gathers inputs, enforces the
"refuse before touching anything" rule, then runs the resync and the optional
old-node decommission, which need the runtime and the matter client. See
docs/plans/issue-364-replace-node.md.

Lives in its own mixin because every XML-named callback must resolve as a flat
attribute on the ``Plugin`` class (issue #146).
"""
# Indigo's callback signatures are fixed (filter/typeId/targetId/menuId/devId), used or not.
# pylint: disable=unused-argument,redefined-builtin
from __future__ import annotations

import functools
from concurrent.futures import CancelledError as FuturesCancelledError
from typing import Any, Optional

import indigo  # provided by the Indigo runtime

from commission_jobs import node_id_to_str
from node_replace import plan_replacement, preview_rows, refusal_reasons
from plugin_constants import RECONCILE_TIMEOUT

#: The six lists ``indigo.device.getDependencies`` returns; each entry is
#: ``{"ID": int, "Name": str}``. The dict itself is never empty (issue #364, D6).
_DEPENDENCY_KINDS = ("actionGroups", "controlPages", "devices", "schedules", "triggers", "variables")

_SHAPE_DIFFERS = " (differs)"


def _degrades_to_empty_list(func):
    """Log and return ``[]`` when a list callback fails, so the dialog still opens.

    Deliberately NOT ``plugin_constants.degrades_to_list_error``: the replace
    pickers must degrade to a plain empty list (issue #364 spec). The exception
    log keeps the evidence; ``functools.wraps`` keeps the name Indigo resolves
    (issue #146).
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: any lookup/format failure while building a picker on the UI
            # thread. Safe because the exception is logged and the dialog still opens
            # (an escape is a raw traceback dialog); Execute re-validates everything.
            self.logger.exception(exc)
            return []
    return wrapper


def _as_node_id(value: Any) -> Optional[int]:
    """The dialog's menu value as an int node id, or None when absent or not an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class NodeReplaceMenuMixin:
    """Replace a Matter node… (issue #364)."""

    # ------------------------------------------------------------------
    # Pickers and preview
    # ------------------------------------------------------------------
    @_degrades_to_empty_list
    def getReplaceOldNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """"Indigo devices to keep:" — nodes that have Indigo devices, unreachable first.

        The node being replaced is almost always the one that stopped answering,
        so it leads; the rest follow by node id.
        """
        rows = []
        for node_id, names in self.device_sync.list_nodes():  # pylint: disable=no-member
            if not names:
                continue
            unreachable = self.device_sync.node_unreachable(node_id) is True  # pylint: disable=no-member
            label = f"{node_id_to_str(node_id)} · {', '.join(names)}"
            if unreachable:
                label += " — unreachable"
            rows.append((0 if unreachable else 1, node_id, str(node_id), label))
        rows.sort(key=lambda row: (row[0], row[1]))
        return [(value, label) for _rank, _node, value, label in rows]

    @_degrades_to_empty_list
    def getReplaceNewNodes(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """"Sync using node:" (Available nodes) — every other commissioned node (D5).

        Nodes whose shape matches the old node's lead; the rest are labelled
        "(differs)" but stay selectable, or "replace anyway" would be
        unreachable. The refusal, if any, happens on Execute.
        """
        old_node = _as_node_id((valuesDict or {}).get("replaceOld"))
        old_refs = self.device_sync.node_device_refs(old_node) if old_node is not None else []  # pylint: disable=no-member
        matching, other = [], []
        for node_id, names in self.device_sync.list_nodes():  # pylint: disable=no-member
            if node_id == old_node:
                continue
            label = f"{node_id_to_str(node_id)} · {', '.join(names) if names else '(no Indigo devices)'}"
            if old_node is None:
                other.append((str(node_id), label))
                continue
            plan = plan_replacement(old_node, old_refs, node_id, self.device_sync.node_device_refs(node_id),  # pylint: disable=no-member
                                    drop_new_node_device=self._old_node_device_tombstoned(old_node))
            if plan.shape_matches:
                matching.append((str(node_id), label))
            else:
                other.append((str(node_id), label + _SHAPE_DIFFERS))
        return matching + other

    def replaceNodeChanged(self, valuesDict, typeId="", devId=0):  # noqa: N802, ARG002
        """Both pickers' callback. Indigo round-trips to the plugin, which re-runs the
        dynamicReload preview (ConfigUI has no per-field change event), and this also
        sets the hidden ``replaceShapeDiffers`` so "Replace anyway" shows only when the
        two nodes' devices differ.

        The field only drives visibility; ``menuReplaceNode`` re-plans from scratch and
        never reads it. On any failure: log and leave the values as they are.
        """
        try:
            old_node, new_node = _as_node_id(valuesDict.get("replaceOld")), _as_node_id(valuesDict.get("replaceNew"))
            differs = False
            if old_node is not None and new_node is not None:
                differs = not self._plan_only(old_node, new_node).shape_matches
            valuesDict["replaceShapeDiffers"] = "true" if differs else "false"
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: a UI-thread callback; an escape is a raw traceback dialog. Safe
            # because the field is cosmetic and Replace validates from a fresh plan.
            self.logger.exception(exc)
        return valuesDict

    @_degrades_to_empty_list
    def getReplacePreview(self, filter="", valuesDict=None, typeId="", targetId=0):  # noqa: N802, A002, ARG002
        """Read-only preview rows (D4): what pairs with what, what gets deleted, what would refuse."""
        values = valuesDict or {}
        old_node, new_node = _as_node_id(values.get("replaceOld")), _as_node_id(values.get("replaceNew"))
        if old_node is None or new_node is None:
            return [("hint", "Pick both nodes above")]
        plan, refusals = self._replacement_plan(old_node, new_node, bool(values.get("replaceAnyway", False)))
        return preview_rows(plan, refusals)

    # ------------------------------------------------------------------
    # Planning (shared by the preview and Execute)
    # ------------------------------------------------------------------
    def _old_node_device_tombstoned(self, old_node: Optional[int]) -> bool:
        """D8: did the user delete the old node's node device by hand (#204 tombstone)?"""
        tombstones = getattr(self.device_sync, "node_tombstones", None)  # pylint: disable=no-member
        return old_node is not None and tombstones is not None and tombstones.is_tombstoned(old_node)

    def _plan_only(self, old_node: int, new_node: int):
        """The pairing plan alone (no reference lookups). D8: a carried node device is not a mismatch."""
        sync = self.device_sync  # pylint: disable=no-member
        return plan_replacement(old_node, sync.node_device_refs(old_node), new_node, sync.node_device_refs(new_node),
                                drop_new_node_device=self._old_node_device_tombstoned(old_node))

    def _replacement_plan(self, old_node: int, new_node: int, allow_mismatch: bool):
        """Build the plan and its refusal reasons without changing anything.

        ``allow_mismatch`` only matters when the shapes differ; a stale ticked box is inert.
        """
        plan = self._plan_only(old_node, new_node)
        references: dict[int, list[str]] = {}
        reference_errors: dict[int, str] = {}
        # A grouped device lists its own group siblings under "devices"
        # (observed live on 2026.2, 2026-10-17). Those are the new node's own
        # devices, about to be deleted together, so they are not references;
        # every other dependent still is.
        new_node_ids = ({new.dev_id for _o, new in plan.pairs} | {ref.dev_id for ref in plan.unmatched_new}
                        | {ref.dev_id for ref in plan.extra_deletes})
        for new in [new for _old, new in plan.pairs] + list(plan.extra_deletes):
            try:
                deps = indigo.device.getDependencies(new.dev_id)
                if not any(kind in deps for kind in _DEPENDENCY_KINDS):
                    # Shape drift: none of the six lists we read is present, so the answer
                    # says nothing about references. Treated as a failed lookup (refuses).
                    raise ValueError("unexpected getDependencies reply (none of the expected lists present)")
                referrers = [str(entry.get("Name", entry.get("ID", "?")))
                             for kind in _DEPENDENCY_KINDS for entry in (deps.get(kind) or [])
                             if not (kind == "devices" and entry.get("ID") in new_node_ids)]
            except Exception as exc:  # pylint: disable=broad-except
                # Absorbing: a failed lookup is recorded and becomes a REFUSAL via
                # reference_errors (D6) — never read as "nothing references it".
                reference_errors[new.dev_id] = str(exc) or type(exc).__name__
                continue
            if referrers:
                references[new.dev_id] = referrers
        refusals = refusal_reasons(
            plan, allow_mismatch=allow_mismatch,
            exported_ids=set(self._exported_ids),  # pylint: disable=no-member  # Plugin (D11)
            references=references, reference_errors=reference_errors)
        return plan, refusals

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------
    def menuReplaceNode(self, valuesDict, menuId=""):  # noqa: N802, ARG002
        # pylint: disable=too-many-return-statements
        """Menu callback: repoint the old node's devices at the new node.

        Returns ``(False, valuesDict, errors)`` for every refusal (nothing was
        changed) and for a partial result (what was skipped is listed);
        ``(True, valuesDict)`` only when every pair was rebound.
        """
        errors = indigo.Dict()
        if self.device_sync is None:  # pylint: disable=no-member
            errors["replaceOld"] = "The plugin is still starting — try again in a moment."
            return (False, valuesDict, errors)
        old_node, new_node = _as_node_id(valuesDict.get("replaceOld")), _as_node_id(valuesDict.get("replaceNew"))
        if old_node is None:
            errors["replaceOld"] = "Choose the Indigo devices to keep."
            return (False, valuesDict, errors)
        if new_node is None:
            errors["replaceNew"] = "Choose the node to sync using."
            return (False, valuesDict, errors)

        plan, refusals = self._replacement_plan(old_node, new_node, bool(valuesDict.get("replaceAnyway", False)))
        if refusals:
            errors["replaceNew"] = "Nothing was changed. " + " ".join(refusals)
            return (False, valuesDict, errors)
        if not valuesDict.get("confirm", False):
            errors["confirm"] = "Tick the box to confirm. The duplicate devices on the new node will be deleted."
            return (False, valuesDict, errors)

        try:
            result = self.device_sync.replace_node(plan)  # pylint: disable=no-member
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: replace_node absorbs per-device failures itself, so this is an
            # unexpected failure part-way. Safe because it becomes a logged dialog error
            # that says devices may have changed, instead of a raw traceback.
            self.logger.error("replace node %s -> %s failed: %s", node_id_to_str(old_node),
                              node_id_to_str(new_node), exc)
            self.logger.exception(exc)
            errors["replaceNew"] = ("Replacement failed part-way — see the Indigo event log. "
                                    "Some devices may already have changed; check them before retrying.")
            return (False, valuesDict, errors)

        if result.rebound:
            self._replace_resync(old_node, new_node)
        if valuesDict.get("replaceDecommissionOld", False):
            self._replace_decommission_old(old_node)

        old_names = {old.dev_id: old.name for old, _new in plan.pairs}
        if result.skipped:
            lines = [f'"{old_names.get(dev_id, f"device {dev_id}")}" was not rebound: {why}.'
                     for dev_id, why in result.skipped]
            self.logger.warning("Matter: replacing node %s with %s was partial: %s", node_id_to_str(old_node),
                                node_id_to_str(new_node), " ".join(lines))
            errors["replaceNew"] = (f"Partly done: {len(result.rebound)} device(s) rebound, "
                                    f"{len(result.skipped)} skipped. " + " ".join(lines))
            return (False, valuesDict, errors)
        if plan.unmatched_old:
            self.logger.info("Matter: %d device(s) on node %s had no match on node %s and were left unchanged.",
                             len(plan.unmatched_old), node_id_to_str(old_node), node_id_to_str(new_node))
        self.logger.info("Matter: replaced node %s with %s — %d Indigo device(s) rebound, %d duplicate(s) deleted.",
                         node_id_to_str(old_node), node_id_to_str(new_node), len(result.rebound), len(result.deleted))
        return (True, valuesDict)

    def _replace_resync(self, old_node: int, new_node: int) -> None:
        """Ask for a reconcile so the rebound devices pick up live state (D10).

        Failure is non-fatal: the devices are already bound and go live on the next
        reconnect. Claims "requested", never "done" (``_resync`` swallows its own errors).
        """
        try:
            # pylint: disable=no-member  # Plugin._resync (issue #146)
            self.runtime.submit(self._resync()).result(timeout=RECONCILE_TIMEOUT)
        except Exception as exc:  # pylint: disable=broad-except
            # Absorbing: submit/result plumbing failures ONLY (_resync handles its own).
            # Safe because the rebind already happened and is reported as such below.
            self.logger.error(
                "Matter: node %s's devices now use node %s, but the reconcile that refreshes them FAILED — %s. "
                "They will refresh on the next successful matter-server reconnect instead.",
                node_id_to_str(old_node), node_id_to_str(new_node), exc)
            self.logger.exception(exc)

    def _replace_decommission_old(self, old_node: int) -> None:
        """Optionally remove the old node from the fabric (D9). Never fails the replacement.

        Runs only when the REBUILT index holds no device for the old node, which is what
        makes ``_decommission_sync`` (index-driven) unable to delete anything the user
        still needs. A reset device no longer answers RemoveFabric, so failure is usual.
        """
        name = node_id_to_str(old_node)
        remaining = self.device_sync.node_device_refs(old_node)  # pylint: disable=no-member
        if remaining:
            self.logger.warning(
                "Matter: not removing the old node %s from the fabric — Indigo devices are still "
                "using it (%s). Use 'Decommission Matter device…' once they are dealt with.",
                name, ", ".join(ref.name for ref in remaining))
            return
        try:
            outcome = self._decommission_sync(old_node)  # pylint: disable=no-member  # HttpApiMixin
        except (Exception, FuturesCancelledError) as exc:  # pylint: disable=broad-except
            # Absorbing: any failure removing the old node. Safe because it is a warning
            # and the replacement is already complete; a reset device not answering is the
            # expected case; the plugin ignores the leftover entry (retired node).
            self.logger.warning(
                "Matter: the replacement is done, but the old node %s could not be removed from the fabric "
                "(%s). That is normal for a device that was factory-reset — it can't answer any more. "
                "The plugin now ignores it; no Indigo device uses it.", name, exc)
            return
        if outcome and outcome.get("fabricRemoved"):
            self.logger.info("Matter: old node %s removed from the fabric.", name)
            return
        self.logger.warning(
            "Matter: the replacement is done, but the old node %s was not removed from the fabric "
            "(the device did not confirm). That is normal for a device that was factory-reset. "
            "The plugin now ignores it; no Indigo device uses it.", name)
