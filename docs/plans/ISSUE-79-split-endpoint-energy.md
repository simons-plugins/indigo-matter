# Plan: Issue #79 — Energy measurement on a separate endpoint (IKEA GRILLPLATS)

**Status:** REVIEWED — adversarially verified against the codebase 2026-07-05
**Issue:** https://github.com/simons-plugins/indigo-matter/issues/79
**Author:** Claude (sonnet subagent review 2026-07-05)

## Problem

The IKEA GRILLPLATS Thread plug exposes On/Off on endpoint 1 and its energy
clusters — Electrical Power Measurement (0x0090), Electrical Energy
Measurement (0x0091), Power Topology (0x009C) — on a dedicated endpoint 2
(the Matter 1.3 "Electrical Sensor" device type, 0x0510). The plugin only
supports Tapo-style co-located energy, so today:

- Endpoint 2 becomes a stateless `matterUnknown` placeholder (0x9C is the
  only cluster that survives the `_NON_DEVICE_CLUSTERS` filter).
- The endpoint-1 relay is created without `SupportsPowerMeter` /
  `SupportsEnergyMeter` props, so no `curEnergyLevel` / `accumEnergyTotal`
  states exist.
- Even if states existed, `_on_attribute()` routes endpoint-2 reports to the
  endpoint-2 device (the placeholder), never the relay.

## Spec grounding (research brief, 2026-07-05)

- Power Topology (0x009C) features: **NodeTopology** (bit 0, "measures the
  whole node", no endpoint list), TreeTopology (bit 1), **SetTopology**
  (bit 2, gates `AvailableEndpoints` attr 0x0000), DynamicPowerFlow (bit 3,
  gates `ActiveEndpoints` attr 0x0001).
- **GRILLPLATS uses NodeTopology** — there is no endpoint list to read.
  Correct client behaviour is to attribute readings to the node's primary
  controllable endpoint by convention.
- Existing attribute IDs in `electrical.py` (ActivePower 0x0008 mW,
  CumulativeEnergyImported 0x0001 mWh struct) are spec-correct; only
  endpoint attribution changes.
- Electrical Sensor device type 0x0510 requires exactly: Descriptor + 0x90 +
  0x91 + 0x9C — the GRILLPLATS endpoint-2 layout.
- Home Assistant does not yet handle this pattern cleanly (core #149847) —
  no upstream implementation to port.

## Design: a per-node "meter link" map

One new concept: at device-planning time, resolve each standalone
energy-measurement endpoint to the endpoint whose power it measures, and
record the link **in both directions** — forward
`(node_id, source_ep) → target_ep` for live routing, and reverse
`(node_id, target_ep) → [source_eps]` for priming and self-heal (both
`_prime_states` and `_reassert_capability_props` start from the *target*
device and need to find its sources). All the same-endpoint assumptions
(creation props, state priming, live routing, reconcile self-heal) consult
this map. No `node_scoped` fan-out (over-broadcasts on bridges), no new
persistent storage (map is deterministic from the node model, rebuilt on
every reconcile).

### Link resolution algorithm (`device_sync.py`, new helper)

A **candidate source endpoint** is a device-bearing endpoint (≥1) that has
0x90 or 0x91 and would produce no primary handler spec.

For each candidate, resolve the target:

1. **SetTopology** (0x9C FeatureMap 0xFFFC bit 2 set): read
   `AvailableEndpoints` (attr 0x0000; prefer `ActiveEndpoints` 0x0001 when
   DynamicPowerFlow is set) from `node.attributes`. Link to each listed
   endpoint that hosts a `_METER_CAPABLE_TYPES` device. v1 keeps the link
   static after reconcile (DYPF changes picked up on next reconcile — note
   as follow-up).
2. **NodeTopology, no 0x9C at all, or SetTopology attrs unreadable**: find
   endpoints that will host a `_METER_CAPABLE_TYPES` device
   (relay/dimmer/colour). If **exactly one**, link to it. If zero or more
   than one (bridge, power strip without SET), attribution fails →
   fallback device (below). Never guess on multi-actuator nodes.

### Changes by file

**`matter_handlers/electrical.py`**
- Add `CLUSTER_POWER_TOPOLOGY = 0x009C` + FeatureMap/attr constants.

**`device_sync.py`**
1. Add `0x009C` to `_NON_DEVICE_CLUSTERS` (merge-only, like 0x90/0x91) —
   this alone stops the placeholder (verified: `_unknown_spec()` returns
   None once `{29,144,145,156}` minus the set is empty, and the electrical
   handlers never produce a primary spec — two independent guarantees).
2. `create_devices()` becomes **two-pass**: first pass calls
   `handlers_for_endpoint()` for every endpoint and caches the specs (needed
   to know which endpoints will host `_METER_CAPABLE_TYPES` devices);
   link resolution runs between the passes; second pass creates devices
   from the cached specs. Precedent for pre-loop node-wide computation
   already exists: the `SupportsBatteryLevel` any-endpoint scan at
   `device_sync.py:298-300` + `setdefault` injection at `:328`.
3. Creation: inject `SupportsPowerMeter` / `SupportsEnergyMeter` into the
   target endpoint's cached spec based on the *linked source endpoint's*
   clusters (central injection in device_sync; the existing same-endpoint
   checks in on_off/level/color stay for the Tapo case).
4. `_lookup_for_cluster()`: for clusters 0x90/0x91, rewrite `endpoint_id`
   via the forward map **at the top of the function** (the electrical
   handlers have `device_type_id=""`, so execution always falls through to
   the bare `lookup()` — the rewrite must happen before that). After
   rewriting, filter the target endpoint's type-map by
   `_METER_CAPABLE_TYPES` rather than relying on sole/first-device
   semantics, so a multi-device target endpoint can never mis-route.
5. `_prime_states()`: the per-device attribute loop currently skips
   `ep != endpoint_id` (`:630`); extend the condition to also accept
   `ep in reverse_links[(node_id, endpoint_id)]` for the electrical
   handlers.
6. Reconcile self-heal: `_capability_props()` is currently a `@staticmethod`
   that inspects only the endpoint passed in — it must become an instance
   method (or take the links map as a parameter) so it can include linked
   source-endpoint clusters when computing meter props for the target.
   This heals the existing jarvis relay 1794293937 in place via
   `replacePluginPropsOnServer` (self-heal pattern live-verified in the
   #56 work per HANDOVER.md; no delete-and-recreate).
7. Log fix (issue point 3): `_unknown_spec()` log/props list **all** the
   endpoint's non-utility clusters, annotating which are merge-only, e.g.
   `unsupported: 0x009C (endpoint also exposes merge-only 0x0090, 0x0091)`.

**Fallback device (issue point 2)** — when attribution fails:
- New Devices.xml type `matterEnergyMeter` (custom). **State ids must match
  what the electrical handlers already write**, because custom types get no
  auto-derived states from `Supports*Meter` props and the handlers guard on
  key presence: declare `curEnergyLevel` (W, UiDisplayStateId),
  `accumEnergyTotal` (kWh), `reachable`. (An earlier draft said
  `curPowerLevel` — that would silently drop every power update.)
- Built by device_sync directly (a `_energy_meter_spec()` sibling of
  `_unknown_spec()`), **not** via a registered `ClusterHandler` —
  `is_primary_for()` has no access to cross-endpoint link state, and
  `_unknown_spec()` is the established precedent for device_sync-owned
  specs. Electrical handlers then write to it via the normal per-endpoint
  route (no link needed — device lives on the source endpoint).

**Behaviour change to an existing test:** `ELECTRICAL_ONLY_NODE`
(fixture at `test_device_sync.py:1430`, assertion in
`test_merge_only_endpoint_gets_no_placeholder` at `:1471-1473`) currently
asserts "no device at all". Under this plan a node whose *only* device
endpoint is electrical gets the fallback `matterEnergyMeter` instead —
update the test; it is the issue's requested behaviour.

### Tests

- **Zoo** (`test_device_zoo.py`): add a multi-endpoint GRILLPLATS entry
  (ep1: Descriptor/Identify/Groups/OnOff; ep2: Descriptor/0x90/0x91/0x9C —
  expressed as `_raw()` attribute paths, not bare cluster lists). Scope:
  the zoo exercises `HandlerRegistry.handlers_for_endpoint()` directly and
  has no `DeviceSync`, so it can only assert the type mapping — **relay on
  ep1, nothing on ep2**. Meter-prop and link assertions belong in
  `test_device_sync.py`.
- **`test_device_sync.py`** (via the existing `ds`/`indigo_env` fixtures):
  link resolution (NodeTopology; SetTopology with
  `AvailableEndpoints=[1]`; no 0x9C + single actuator; ambiguous
  two-actuator node → fallback; electrical-only node → fallback device);
  creation-time meter props on the ep1 relay; live routing (ep2
  ActivePower report updates ep1 relay's `curEnergyLevel`); priming from
  linked endpoint; reconcile self-heal adds meter props to a pre-existing
  relay without recreation.
- **`test_electrical.py`**: parsing untouched; add a cross-endpoint stub
  case if needed.
- Full suite green (`python3 -m pytest -q`, 743+ tests).

### Rollout

1. PR with version bump (`PluginVersion` in Info.plist). Suggest
   **2026.3.0** (user-visible feature: split-endpoint energy support) —
   confirm with Simon; 2026.2.24 if he prefers patch.
2. Deploy to jarvis (cp + restart), verify:
   - Reconcile heals relay 1794293937 → props + energy states appear.
   - Live wattage/energy from GRILLPLATS (it's on the house fabric).
   - Placeholder 1456954491 triggers the "can be deleted" obsolescence log.
   - Tapo P110M co-located path unchanged.
3. Update issue #79 with results; add GRILLPLATS to the zoo note in
   HANDOVER.md.

### Risks / notes

- **Unverified external assumption:** SetTopology resolution needs the 0x9C
  FeatureMap (`node.attributes[(2, 0x009C, 0xFFFC)]`). `parse_node()` keeps
  every attribute path with no allowlist, so the plugin side is fine, but
  whether matter-server includes global attributes of an *unhandled*
  cluster in its dump must be checked against the live GRILLPLATS before
  coding the SetTopology branch (`/diagnostics?nodeId=0x34` or a raw node
  dump). If absent, the NodeTopology/heuristic path still works — it needs
  no attribute reads.
- Primary real-device path is the NodeTopology heuristic ("sole actuator");
  SetTopology is synthetic-tested only until a power strip shows up.
- Bridges: link resolution must run per *node*, and bridged nodes with
  multiple actuators deliberately fall back rather than guess.
- `attributes_to_subscribe()` is dead code (server streams everything), so
  no subscription changes needed — routing is purely a device_sync concern.
- Follow-up (not in scope): DynamicPowerFlow live re-linking; Voltage /
  ActiveCurrent extra states (0x90 optional attrs — GRILLPLATS has the AC
  feature, could surface later).
