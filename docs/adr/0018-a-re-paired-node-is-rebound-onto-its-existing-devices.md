---
parent: Decisions
nav_order: 18
title: "ADR-0018: A re-paired node is rebound onto its existing Indigo devices"

status: "accepted"
date: 2026-10-17
decision-makers: solo (Simon)
consulted: none
informed: none
---
# ADR-0018: A re-paired node is rebound onto its existing Indigo devices

## Context and Problem Statement

Each Indigo device this plugin owns finds its hardware through two props:
`nodeId` and `endpointId`. When a device is factory-reset and paired again,
Matter gives it a new node id. The plugin then builds a fresh set of Indigo
devices for that node, while the old devices keep pointing at a node that
never answers again. Every schedule, trigger, zone, export and history row is
attached to the old devices by their Indigo id. Only the owning plugin may
change those props, so the user cannot fix this from a script (issue #364).

How should a re-paired device get its old Indigo devices back?

## Decision Drivers

* The Indigo ids must not change, because everything else refers to them.
* A failure part-way must never leave two devices claiming one
  `(node, endpoint, type)`, and never delete the devices being rescued.
* It follows Indigo's own Z-Wave and Insteon "replace a device" model: the
  Indigo device is independent of the hardware, and a sync points it at
  another node.

## Considered Options

* **Rebind:** rewrite the old devices' `nodeId`/`address` to the new node and
  delete the new node's duplicates ("Replace a Matter node…").
* **Repoint the automations** from the old devices to the new ones. This was
  rejected: plugin-action props (Lamplighter zones, for example) and SQL Logger
  history are keyed by device id, and cannot all be found or moved.

## Decision Outcome

Chosen option: **rebind**, implemented by `node_replace.py` (pure planner),
`DeviceSync.replace_node` and `NodeReplaceMenuMixin`. The numbered decisions
below are cited from the code and tests.

Two traps decide the order of operations:

1. **Delete first, rebind last.** A props write restarts the device, and
   `deviceStartComm` → `note_device` re-registers it under the new node. An
   index-driven `delete_node(new)` after that would delete the rescued
   devices.
2. **Pause creation.** `node_updated` arrives all the time. If one lands
   between the delete and the rebind, it would rebuild the duplicates.

| # | Decision | Why |
|---|----------|-----|
| D1 | Delete the duplicates **before** rebinding, from ids captured up front. Never call `delete_node(new)` after a rebind. | Trap 1. |
| D2 | `creation_suspended(new)` pauses `create_devices` for the new node for the whole operation. The check sits inside the same locked region that creates devices. | Trap 2. A check outside that lock left a window. |
| D3 | A pair is rebound only if its duplicate was actually deleted. | Otherwise two devices claim one `(node, ep, type)`. |
| D4 | The preview is a read-only `list` with `dynamicReload`, refreshed by the pickers' `CallbackMethod`. Rows are short ("Switch → 0x43"), and refusals show as one row, with the reasons on the Replace click. The blind-spot note is a static label. | A ConfigUI label cannot change while the dialog is open, and the list sits in the control column. |
| D5 | The new-node picker lists every other commissioned node, matches first and the rest marked " (differs)". "Matching only" (`replaceAnyway`) is hidden unless the shapes differ, via the hidden `replaceShapeDiffers` set by `replaceNodeChanged`. Replace re-plans and never trusts that field. | Hiding mismatched nodes would make "replace anyway" unreachable. |
| D6 | "Referenced" means any of `getDependencies`'s six lists is non-empty. A reply with none of the six keys counts as a failed lookup. The new node's own devices (group siblings) are ignored. A raise or a failed lookup **refuses**. | The dict is never empty, and a grouped device lists its siblings under `devices` (seen live). A failed check is not a clean bill of health. |
| D7 | `softwareVersion` comes from the new node device's props, captured before the deletes. | Rebinding works offline, and the reconcile refreshes it. |
| D8 | If the old node is tombstoned (#204), the planner moves the new node's matterNode into `extra_deletes`. It is previewed as deleted, checked for exports and references like a paired duplicate, and not counted as a shape mismatch. The new node is tombstoned only if that delete succeeds. | It honours "no node device" on the replacement hardware, visibly and checked. |
| D9 | The optional old-node decommission runs only if the rebuilt index holds no devices for the old node, and reuses `_decommission_sync`. If it fails, it warns and does not fail the replacement. | `delete_node(old)` is index-driven, and a reset device may not answer RemoveFabric. |
| D10 | The resync uses `runtime.submit(self._resync())` like `menuRecreateNodeDevices`. A failure there is non-fatal and reported as "requested", not "done". | The devices are bound either way. |
| D11 | Exported ids come from `self._exported_ids`. | The same source `deviceDeleted` uses. |
| D12 | `_delete_devices` prunes each id from `_index` as it is deleted. It also ignores a `node_dev_id` it was not asked to delete. | A mid-delete event never sees a stale id, and a kept device is never left marked self-deleted. |
| D13 | After a replacement that rebound anything, the old node is **retired** (a persisted set, like the tombstones). `create_devices` makes nothing for it. The retirement is cleared by `node_removed`, by `delete_node(forget=True)`, and by commissioning onto that id. | The old node usually stays commissioned, and every reconcile would otherwise rebuild a duplicate set for it. |

### Consequences

* Good: ids, names, folders, settings and every reference survive. Verified
  live on two GRILLPLATS plugs (52 → 67, 63 → 68).
* Neutral: device-held state starts again from the new hardware. Cumulative
  energy restarts from the new plug's counter, and Matter settings such as
  StartUpOnOff are the reset device's defaults until set again in Edit Device.
* Bad: the reference check cannot see references inside other plugins' action
  props. The dialog says so.

### Confirmation

`tests/test_node_replace.py`, `tests/test_device_sync_replace.py` and
`tests/test_node_replace_menu.py`. Trap 1 and trap 2 each have a test that
fails against the obvious wrong implementation.
