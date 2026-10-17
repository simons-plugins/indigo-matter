# Plan: "Replace a Matter node…" (issue #364)

Working plan, to be deleted once #364 is resolved (same as the #79 plan).

## The problem, simply

Each Indigo device this plugin owns finds its hardware through two props:
`nodeId` (which Matter node) and `endpointId` (which part of it). When a
device is factory-reset and paired again, Matter gives it a new node id. The
plugin sees a node it doesn't recognise and builds a new set of Indigo devices
for it. The old devices still point at the old node id, which no longer
answers. Every schedule, trigger, zone, export and history row is attached to
those old devices by their Indigo id.

Only this plugin is allowed to edit those props, so the user can't fix it
from a script.

## The idea, simply

Repoint the old devices at the new node and throw away the new duplicates.
The Indigo ids never change, so nothing that references them notices.

1. Pair each old device with its new twin, by endpoint and device type.
2. Delete the new twins first.
3. Rewrite `nodeId` and `address` on the old devices.
4. Rebuild the plugin's lookup table and reconcile, so the old devices go live
   on the new node at once.

Two traps decide the order:

- **Rebind last, delete first.** Rewriting a device's props makes Indigo
  restart that device, and the restart re-registers it in the lookup table
  under the new node. If the table were then used to "delete node 67's
  devices", it would find and delete the devices we just rescued. So the
  duplicates are deleted first, by ids captured before anything changes.
- **Hold off auto-creation.** matter-server sends updates for node 67 all the
  time. If one arrives between deleting the twins and rebinding, the plugin
  would rebuild them. So device creation for the new node is paused for the
  duration.

If anything fails midway, delete-first means the worst case is "back where we
started": the next reconcile recreates node 67's devices.

## Decisions (and where they differ from the issue's wording)

| # | Decision | Why |
|---|----------|-----|
| D1 | Delete duplicates **before** rebinding, from ids captured up front. Never call `delete_node(new)` after a rebind. | See trap 1. `deviceStartComm` → `note_device` overwrites `_index[(new, ep)][type]` with the old device. |
| D2 | `DeviceSync` pauses `create_devices` for the new node while the replacement runs. | See trap 2. `node_updated` fires on every availability change. |
| D3 | A pair is rebound **only if its duplicate was actually deleted.** | A failed delete plus a rebind would leave two devices claiming one `(node, ep, type)`. |
| D4 | The preview is a read-only `list` with `dynamicReload`, refreshed by the pickers' `CallbackMethod`. Rows are short (role and outcome, e.g. "Switch → moves to 0x43"); refusals are one row, with the full reasons on the Replace click. The reference-check blind-spot note is a static label under the list. | ConfigUI has no per-field change callback, so a label can't update (see the `MenuItems.xml` diagnostics comment). `exportList` and `exploreNodeChanged` already do this. |
| D5 | The new-node picker lists **every** other commissioned node: matching shapes first, the rest labelled "(differs)". | Filtering out mismatches would make "replace anyway" unreachable. The refusal happens on confirm. The pickers read `0x34 · <names>`, and a mismatch is marked " (differs)". The "Replace anyway" checkbox is hidden unless the shapes differ (hidden `replaceShapeDiffers`, set by `replaceNodeChanged`; Replace re-plans and never trusts it). |
| D6 | "Referenced" means any of the six lists in `indigo.device.getDependencies(id)` is non-empty (`actionGroups`, `controlPages`, `devices`, `schedules`, `triggers`, `variables`; each entry `{"ID", "Name"}`, and referrer names come from `Name`). The dict itself is never empty, so `if deps:` is a bug. A grouped device also lists its own **group siblings** under `devices` (seen live on node 67, 2026-10-17). Those are the new node's own devices, so they're ignored; any other dependent still refuses. **If the call raises, refuse.** The preview says the check can't see references inside plugin-action props, such as Lamplighter zones. | Degradation-path rule: a failed check isn't a clean bill of health. |
| D7 | `softwareVersion` comes from the new node **device's** props. Nothing is fetched from matter-server. | Rebinding works offline, and the reconcile in step 4 refreshes it anyway. |
| D8 | If the old node was tombstoned (#204, the user deleted its node device), the new node's matterNode device is deleted too, **paired or not**, and the new node is tombstoned. That honours "I don't want a node device" on the hardware that replaces it. All matterNode deletes go through `_self_deleted_ids`, so none tombstones by accident; the carry is explicit. | Otherwise the next reconcile grows a node device the user had removed, or refuses to recreate one they didn't. |
| D9 | Optional old-node decommission runs only if the **rebuilt** index holds no devices for the old node. It reuses `_decommission_sync`. If it fails (the usual case for a reset device, which no longer answers RemoveFabric), it warns and doesn't fail the replacement. | `delete_node(old)` is index-driven, so the empty-index check is what makes it unable to touch anything. |
| D11 | Exported ids come from `self._exported_ids`, Plugin's cached frozenset, the same source `deviceDeleted` uses. |
| D12 | `_delete_devices` prunes each id from `_index` **as it is deleted**, not in one pass at the end. An event that lands mid-delete then never sees a stale id. |
| D10 | Resync uses the same `runtime.submit(self._resync()).result(timeout=RECONCILE_TIMEOUT)` as `menuRecreateNodeDevices`. A failure there is non-fatal: the devices are bound and go live on the next reconnect. | Same honesty rule as the recreate menu: claim "requested", not "done". |

No ADR is needed: this introduces no new rule, only an operation that respects
ADR-0008, ADR-0009 and the #204 tombstone. If review disagrees, it would be
repo-local ADR-0018, "a re-paired node is rebound onto existing devices".

## Sections

Each section is one subagent task, with its tests written first. Waves:
**S1 ∥ S2 → S3 → S4**. S1 and S2 touch disjoint files.

### S1. Pure planner (`node_replace.py`, new)

There's no Indigo in this module, so it's trivially testable.

```python
@dataclass(frozen=True)
class DeviceRef:
    dev_id: int
    endpoint_id: int
    type_id: str
    name: str

@dataclass(frozen=True)
class ReplacementPlan:
    old_node: int
    new_node: int
    pairs: tuple[tuple[DeviceRef, DeviceRef], ...]   # (old, new), sorted by (endpoint_id, type_id)
    unmatched_old: tuple[DeviceRef, ...]
    unmatched_new: tuple[DeviceRef, ...]

    @property
    def shape_matches(self) -> bool: ...   # no unmatched on either side

def plan_replacement(old_node, old_refs, new_node, new_refs) -> ReplacementPlan
def refusal_reasons(plan, *, allow_mismatch: bool, exported_ids: set[int],
                    references: dict[int, list[str]], reference_errors: dict[int, str]) -> list[str]
def preview_rows(plan, refusals: list[str]) -> list[tuple[str, str]]
def rebound_props(old_props: dict, new_node: int, *, software_version: str | None) -> dict
```

- `refusal_reasons` order, so the most fundamental reason comes first:
  1. same node
  2. no pairs at all
  3. shape mismatch (only when `allow_mismatch` is False)
  4. an exported **paired new** device (message names "Migrate an exported accessory…")
  5. a referenced paired new device (names the referrers)
  6. a reference lookup that failed

  Only **paired** new devices are checked. Unmatched new devices are kept, so
  their exports and references don't matter.
- `rebound_props` returns a copy with `nodeId=str(new)` and
  `address=node_id_to_str(new)`. It sets `softwareVersion` only when given,
  and only on a matterNode (the caller decides). Every other key is kept
  as is.
- `preview_rows`: one row per pair (`"<old name>  ←  <new name> (deleted)"`),
  one per unmatched device, then the refusals, then the getDependencies
  blind-spot note. Values are unique strings, which a list needs.

### S2. DeviceSync primitives (`device_sync.py`)

- `node_device_refs(node_id) -> list[DeviceRef]`: a snapshot from `_index`,
  with names resolved outside the lock (the `list_nodes` discipline). A missing
  device is skipped.
- `_delete_devices(dev_ids, node_dev_id) -> list[int]`: the ungroup-all-then-delete
  body **extracted** from `delete_node`, including the `_self_deleted_ids`
  marking. `delete_node` calls it, and its existing tests must pass unchanged.
- `creation_suspended(node_id)`: a context manager that adds the node to
  `_creation_suspended`. `create_devices` for a suspended node logs at debug
  level and returns without creating anything, keeping its normal return shape
  with empty ids. It's re-entrant-safe (counter, or set plus try/finally) and
  releases on exception.
- `node_unreachable(node_id) -> bool | None`: the node device's `reachable`
  state, inverted. Returns None when there's no node device. Used only to sort
  the old-node picker.

### S3. The operation (`DeviceSync.replace_node`)

```python
@dataclass
class ReplaceResult:
    rebound: list[int]
    deleted: list[int]
    skipped: list[tuple[int, str]]   # (old dev id, reason): duplicate not deleted / props write refused

def replace_node(self, plan: ReplacementPlan) -> ReplaceResult
```

Inside `creation_suspended(plan.new_node)`:

1. Capture `dups = [new.dev_id for _, new in plan.pairs]` and the new matterNode
   id (if paired).
2. `deleted = self._delete_devices(dups, new_node_dev_id)`.
3. For each pair whose new device is in `deleted`, read the old device fresh
   from `indigo.devices`. Then call
   `replacePluginPropsOnServer(rebound_props(...))`, passing `software_version`
   only for matterNode, taken from the new node device's props **captured
   before step 2**. Each pair is guarded on its own: a refusal goes to
   `skipped`, and the other pairs go ahead.
4. D8 tombstone carry.
5. `self._known_nodes.add(new)`; `rebuild_index()`.
6. INFO line per rebound device (`'"%s" (id %s) rebound node %s -> %s'`) and
   per deleted duplicate.

The menu (S4) does the resync and the optional decommission, not `DeviceSync`,
because they need the runtime and the matter client.

### S4. Menu, wiring, docs (`node_replace_menu_mixin.py` new, `MenuItems.xml`, `plugin.py`, docs)

- `NodeReplaceMenuMixin`, added to `Plugin`'s bases (issue #146: callbacks must
  resolve on the class).
- Callbacks:
  - `getReplaceOldNodes`: nodes that have devices, unreachable first.
  - `getReplaceNewNodes`: excludes the selected old node; matches first (D5).
  - `replaceNodeChanged`: a no-op that forces the reload.
  - `getReplacePreview`.
  - `menuReplaceNode`.

  List callbacks degrade to an empty list and log the exception (the
  `getMatterNodes` pattern).
- `menuReplaceNode` order:
  1. selections present and integer
  2. build the plan
  3. gather references with `indigo.device.getDependencies`, per paired new device, guarded
  4. `refusal_reasons`; any refusal → field error, **nothing changed**
  5. confirm ticked
  6. `replace_node`
  7. resync (D10)
  8. optional decommission (D9)

  Return `(True, valuesDict)` only when every pair was rebound. A partial
  result is a dialog error that lists what was skipped.
- `tests/conftest.py`: add `"node_replace"` and `"node_replace_menu_mixin"` to `_PLUGIN_MODULES`.
- `MenuItems.xml`: `replaceMatterNode`, placed after `decommissionDevice`. Fields:
  - `replaceIntro` label
  - `replaceOld` menu with callback
  - `replaceNew` menu with callback
  - `replacePreview` read-only list
  - `replaceAnyway` checkbox
  - `replaceDecommissionOld` checkbox (default false)
  - `confirm` checkbox
- **Wording follows Indigo's Z-Wave/Insteon replace flow** (user guide, "Replacing a Z-Wave Device"), so it reads as familiar:
  - `replaceOld` label "Indigo devices to keep:"
  - `replaceNew` label "Sync using node:", options under the heading "Available nodes"
  - `replaceIntro` explains, in Indigo's words, that the Indigo device and everything referring to it are independent of the physical device, and this points the Indigo devices at the replacement node
  - refusal for referenced new devices mirrors Indigo's: resolve those references first, or delete the devices by hand
- `docs/MATTER.md`: a short "Replacing a re-paired device" section.
  `CHANGELOG.md`. Minor bump of `PluginVersion` (user-visible feature).

## Tests

Files: `tests/test_node_replace.py` (S1), `tests/test_device_sync_replace.py`
(S2, S3), `tests/test_node_replace_menu.py` (S4). They reuse `indigo_env` and
`ds` from `test_device_sync.py`'s shape, and `_with_node_evidence`.

**Hazard simulation (required in S3 tests):** wrap the fake
`replacePluginPropsOnServer` so it also calls `ds.note_device(dev)`, the way
Indigo's comm restart does. Without it, the trap 1 test can't fail.

### S1
- Same shape → two pairs, nothing unmatched, `shape_matches`.
- Extra endpoint on the new node → `unmatched_new`. A type differing on ep1 →
  both sides unmatched.
- Each refusal fires alone, and the order holds when several apply.
- Mismatch plus `allow_mismatch=True` → no mismatch refusal. An exported
  **unmatched** new device → no refusal.
- A reference-lookup error → refusal, never silence.
- `rebound_props` keeps `startUpOnOff`, `onTime`, `nodeBaseName`,
  `createdTypeId` and `endpointId`, and doesn't mutate its input.

### S2
- `delete_node`'s existing suite still passes. `_delete_devices` dissolves the
  group before any delete (use a fatal delete that raises if a grouped root
  goes first).
- `create_devices` under `creation_suspended(67)` creates nothing for 67 and
  still creates for 42. The suspension lifts after an exception in the body.
- `node_device_refs` skips a device deleted out of band.

### S3 (the issue's acceptance, at the DeviceSync level)
- Coffee fixture: old 52 (node device and relay), new 67 (same shape). After
  `replace_node`:
  - old devices have `nodeId` "67" and `address` "0x43"
  - other props and **names** are unchanged
  - duplicates are gone
  - `lookup(67, 1, "matterRelay")` is the old relay
  - 67 is not tombstoned
  - `knows_node(67)`
- **Trap 1:** with the note_device hazard wired in, the old devices survive and
  only the duplicates are deleted.
- **Trap 2:** an `EVT_NODE_UPDATED` for 67 delivered from inside a delete (the
  fake `delete` triggers `ds.handle_event`) doesn't recreate anything.
- `build_command` for the old relay targets node 67. An attribute event from
  `(67, 1)` updates the old relay.
- `reconcile_all([node 67])` afterwards (the restart path) creates nothing new
  and doesn't rename the old devices.
- The duplicate delete for ep1 is refused → the ep1 old device isn't rebound,
  and is reported in `skipped`. The node-device pair still rebinds.
- A props write refused for one device → that one is in `skipped`, and the
  others are rebound.
- Old node tombstoned → new node tombstoned after; no node device recreated on
  reconcile.
- `deviceDeleted` for the deleted new matterNode (via `note_node_device_deleted`)
  doesn't tombstone 67.
- Afterwards, `delete_node(52)` deletes nothing.

### S4
- Refusals leave every device untouched. Use a fake device whose
  `replacePluginPropsOnServer` raises if called, and a delete that raises.
- Unticked confirm → no change.
- Exported paired new device → error mentions "Migrate an exported accessory".
- `getDependencies` raises → refused.
- Happy path → `_resync` submitted once and `(True, valuesDict)` returned.
- Resync failing → still `True`, with an error logged that names the next reconnect.
- Decommission ticked: `_decommission_sync(52)` called once, and no Indigo
  device deleted. When it raises or returns `fabricRemoved: False` → warning,
  still `True`.
- Decommission ticked but an unmatched old device remains on 52 →
  `_decommission_sync` **not** called, with a warning.
- Every `CallbackMethod` and list `method` in `MenuItems.xml` resolves on
  `Plugin` (extend the existing XML-callback test if there is one).

## Out of scope

- Moving references from the new devices onto the old ones. That's the
  opposite direction; the refusal covers it.
- Force-removing a dead node from matter-server's fabric.
