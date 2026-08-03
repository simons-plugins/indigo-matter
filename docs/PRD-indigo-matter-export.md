# PRD — Indigo Matter Export (Indigo as a Matter bridge)

**Status:** Draft — scoping
**Owner:** Simon
**Governing ADR:** [`../../docs/adr/0006-indigo-as-matter-bridge.md`](../../docs/adr/0006-indigo-as-matter-bridge.md) (accepted 2026-08-03)
**Companion PRD:** [`PRD-indigo-matter-plugin.md`](./PRD-indigo-matter-plugin.md) (historical — the inbound/controller build)
**Last updated:** 2026-08-03

## 1. Summary

A second, outbound role for `indigo-matter`: export a user-selected set of
Indigo devices as a Matter **bridge**, so Apple Home, Alexa, Google Home and
SmartThings can see and control them locally, with no cloud relay and no
per-ecosystem bridge plugin.

Per ADR-0006 this is a **plugin-owned matter.js bridge node** (a second Node
process, authored by us) plus an **explicit allow-list** (nothing is exported
until the user says so). The inbound controller (`matter-server`) is untouched;
the two processes share a Node runtime and nothing else.

The substantive design work is §5.2 (the Indigo → Matter mapping and its
non-exportable set) and §5.1 (how a user picks devices). Everything else is
lifecycle plumbing that closely mirrors what `server_process.py` already does
for the controller.

## 2. Goals

- **XG1.** A user can select any supported Indigo device and have it appear as a
  native accessory in a paired ecosystem within 30s, without editing files.
- **XG2.** Commands from an ecosystem reach the Indigo device within 500ms; Indigo
  state changes propagate outward within 500ms.
- **XG3.** One bridge node, one pairing operation, one uncertified-accessory
  prompt — regardless of how many devices are exported.
- **XG4.** Endpoint identity is stable across plugin reloads, bridge-node
  restarts and Mac reboots. Paired ecosystems never re-map or duplicate
  accessories.
- **XG5.** Default posture is inert: a fresh install exports nothing, pairs
  nothing, and starts no bridge process.
- **XG6.** The Python plugin never imports matter.js (ADR-0006 binding constraint).
- **XG7.** Adding a device-type mapping in future is an isolated, mechanical
  change — one table, one handler, mirroring inbound's §5.3 design.

## 3. Non-Goals

- **XNG1.** No CSA certification. Test attestation certificate, uncertified
  warning accepted (ADR-0006 "Attestation cuts both ways").
- **XNG2.** No export of action groups, schedules, triggers or variables.
- **XNG3.** No re-export of devices `indigo-matter` imported over Matter.
- **XNG4.** Not a Home Assistant importer. HA → Indigo is the *inbound*
  controller commissioning an HA-side bridge; separate decision, separate work.
- **XNG5.** No replacement of native Indigo plugins with Matter round-trips.
- **XNG6.** No second bridge node / multi-bridge topology in v1.

## 4. Architecture

### 4.1 Process topology

```
┌────────────────────────── Indigo Server (macOS) ────────────────────────────┐
│                                                                              │
│                         ┌──────────────────────┐                             │
│         WebSocket       │  indigo-matter       │       WebSocket             │
│  ┌──────────────────────┤  plugin (Python)     ├──────────────────────┐      │
│  │  (inbound, existing) │                      │  (outbound, NEW)     │      │
│  ▼                      │  - allow-list        │                      ▼      │
│ ┌──────────────────┐    │  - export mapper     │    ┌──────────────────────┐ │
│ │ matter-server    │    │  - cluster mapper    │    │ bridge node          │ │
│ │ (controller)     │    └──────────┬───────────┘    │ (matter.js, device)  │ │
│ │ - Indigo fabric  │               │                │ - Aggregator ep      │ │
│ └────────┬─────────┘               ▼                │ - one child ep per   │ │
│          │                  Indigo Database         │   exported device    │ │
└──────────┼───────────────────────────────────────────────────┬──────────────┘
           ▼                                                    ▼
   Matter devices (in)                        Apple Home / Alexa / Google /
                                              SmartThings  (out)
```

Two processes, opposite roles, no shared state. The bridge node holds **no
fabric of its own** — it is commissioned *into* other ecosystems' fabrics.

### 4.2 Process management

Reuse the settled pattern. ADR/PRD history chose **PM-B** (launchd LaunchAgent)
for the controller and it shipped; `server_process.py` already carries install,
version-pinning, launchd plist management, restart and health-check machinery.

**Decision:** the bridge node is a **second LaunchAgent**, managed by
generalising `server_process.py` rather than duplicating it. Two differences
from the controller agent:

- It is **not started at all** while the allow-list is empty (XG5).
- It has no data directory to treat as sacred in the controller's sense, but it
  *does* hold commissioned-fabric credentials for paired ecosystems — losing
  them un-pairs every ecosystem and forces re-pairing. Back it up alongside the
  controller's storage.

### 4.3 Storage

- **Bridge node** — its own storage dir, sibling to the controller's:
  `~/Library/Application Support/com.simons-plugins.indigo-matter/bridge-node/`.
  Contains the node's operational credentials for each ecosystem fabric it has
  joined, plus the endpoint-ID allocation map.
- **Plugin** — owns the allow-list and per-export metadata (role, name override,
  polarity) in plugin prefs, backed up with Indigo's database.
- **Endpoint stability (XG4)** is a hard requirement. Endpoint IDs and Bridged
  Device Basic Information `UniqueID` values MUST be allocated once, persisted,
  and derived from the Indigo device ID — never from list position or iteration
  order. A monotonic persisted allocator, mirroring `MatterFabricStore.nextNodeID()`
  in the Domio work, is the known-good shape.

### 4.4 Local protocol

Bidirectional, because both directions carry traffic: the plugin pushes Indigo
state changes outward, and the bridge node pushes ecosystem commands inward.

**Decision:** mirror the controller's WebSocket + JSON message shape. It costs
nothing new to learn, `matter_client.py` is a working reference for the client
half, and the existing test doubles generalise. The bridge node listens on
loopback only, on a configurable port defaulting adjacent to the controller's.

## 5. Components

### 5.1 Export allow-list and selection UI

Policy is fixed by ADR-0006 E2: **explicit allow-list, default empty.** What
remains is the mechanism. Export needs per-device metadata beyond a boolean —
role (§5.2), display name override, and polarity for covering/lock-like
devices — which rules out a plain multi-select.

| Option | Shape | Assessment |
|---|---|---|
| **UI-A** | Multi-select list in PluginConfig | Rejected — carries no per-export metadata; unusable at 200 devices |
| **UI-B** | One "Exported Device" plugin device per export | Fallback — free per-device config dialogs, but doubles Indigo device count and puts a shadow device beside every real one |
| **UI-D** | `Plugins ▸ Matter ▸ Manage Matter Exports…` dialog | **Chosen** — filterable device picker plus a per-export detail pane; no device clutter; allow-list lives in plugin prefs |

**Decision: UI-D**, falling back to **UI-B** if Indigo's XML dialog list
controls prove unworkable at scale — the same "recommended start, documented
fallback" treatment PM-B/PM-A got in the original PRD.

The candidate list MUST be filtered by plugin ID to exclude `indigo-matter`'s
own devices, making the loop guard (XNG3) structural rather than a runtime check.

### 5.2 Indigo → Matter device-type mapping (v1)

This is the inverse of the inbound §5.4 table, and it is **not symmetric**.
Inbound, Matter tells us what a device is — the device type and cluster set are
explicit. Outbound, Indigo does not: a `relay` device may be a lamp, a plug, a
lock, a valve, a fan or a garage door, and nothing in the Indigo device model
distinguishes them.

**Consequence: export requires a per-device *role* that Indigo cannot supply.**
In v1 the role is user-declared in the §5.1 detail pane, defaulting to the
safest interpretation (plug/light) rather than guessing.

> **Cross-repo note.** This is the same classification problem `domio-code` and
> `indigo-device-catalog` already solve for Domio's device rendering. The catalog
> is the natural long-term home for role (and polarity), and role/polarity fields
> are already a known catalog gap. **v2 candidate:** default the export role from
> the catalog, keeping the user override authoritative. Do not build a third
> independent classifier.

| Indigo device | Declared role | Matter device type | Notes |
|---|---|---|---|
| Relay | Plug *(default)* | On/Off Plug-in Unit | Safest default |
| Relay | Light | On/Off Light | |
| Relay | Lock | Door Lock | Requires ecosystem PIN/confirm semantics; see §7 |
| Relay | Valve | Water Valve | Inherit inbound's flood-safe toggle behaviour |
| Relay | Garage door | *Not exportable in v1* | Polarity + safety; see below |
| Dimmer | Light | Dimmable Light | |
| Dimmer (colour) | Light | Extended Color Light | Colour-temp-only devices → Color Temperature Light |
| Dimmer | Window covering | Window Covering | Polarity declared per export (100% = open, inbound convention) |
| Dimmer | Fan | Fan | Where the Indigo device is a fan modelled as a dimmer |
| Sensor (binary, motion) | — | Occupancy Sensor | |
| Sensor (binary, contact) | — | Contact Sensor | |
| Sensor (numeric, °C) | — | Temperature Sensor | |
| Sensor (numeric, %RH) | — | Humidity Sensor | |
| Sensor (numeric, lux) | — | Light Sensor | |
| Sensor (numeric, pressure) | — | Pressure Sensor | |
| Sensor (numeric, flow) | — | Flow Sensor | |
| Thermostat | — | Thermostat | Setpoints, modes; fan merged if present |
| SpeedControl | Fan | Fan | Map speed index → percent |

Matter device-type IDs are deliberately omitted here; take them from the
matter.js device-type catalogue at implementation rather than transcribing
them into a PRD where they can rot.

**Explicitly not exportable in v1:**

| Excluded | Why |
|---|---|
| Any device created by `indigo-matter` | Loop guard (XNG3), enforced at §5.1 |
| Sprinkler devices | Matter has no irrigation-controller type; per-zone Water Valve is a lossy fit. v2 candidate |
| MultiIO devices | No coherent single-accessory representation |
| `custom` devices with no resolvable role | Includes the plugin's own energy-meter type |
| Garage doors | Needs the polarity handling the catalog doesn't yet carry (`onState` true = closed, turnOn = close), and mis-mapping is a physical-safety issue. Blocked on the catalog role/polarity work |
| Sensors with units outside the table | No faithful Matter sensor type |

Excluded devices must be **absent from the picker with a reason shown**, not
silently missing.

### 5.3 Bridge node

One Matter node: root endpoint, an **Aggregator** endpoint, and one **Bridged
Node** child endpoint per exported device carrying Bridged Device Basic
Information (`NodeLabel`, `Reachable`, `UniqueID`) — the standard bridge
topology the plugin already consumes inbound (`MATTER.md:238-244`).

- **Max fabrics:** set to allow at least 5 concurrent ecosystems.
- **Reachable** must track the Indigo device's enabled/available state so
  ecosystems grey out unavailable accessories instead of timing out.
- **Removal:** dropping a device from the allow-list removes its endpoint and
  updates the aggregator's `PartsList`; ecosystems remove the accessory.
- **Endpoint count:** no hard cap in v1, but log a warning past ~100 exports —
  ecosystem per-home accessory limits (Apple's in particular) will bite first.

### 5.4 Lifecycle hooks

- `startup` — if the allow-list is non-empty, ensure the bridge agent is
  installed and running; connect; reconcile endpoints against the allow-list.
- `shutdown` — close the socket; leave the agent running (PM-B rationale:
  plugin reloads must not un-pair ecosystems).
- `deviceUpdated` — diff relevant states, push outward.
- `deviceDeleted` — remove from allow-list, drop the endpoint.
- `runConcurrentThread` — health check, reconnect, reconcile drift.

### 5.5 Configuration UI

Plugin config gains an **Export** section: enable/disable export wholesale,
bridge-node port, and a pairing-status readout (which ecosystems are paired,
fabric slots used/remaining). Per-export settings live in the §5.1 dialog.

## 6. Pairing and fabric management

- **Pair:** a menu action surfaces the bridge node's setup code and QR payload.
  The user adds it in each ecosystem's app as they would any Matter accessory.
- **Uncertified prompt:** expect an uncertified-accessory warning in every
  ecosystem (ADR-0006). Document it as *expected* in `INSTALL.md`, with the
  Homebridge parallel, so it doesn't read as a fault.
- **Unpair:** a per-fabric removal action, plus a "reset all pairings" that
  wipes bridge-node credentials and starts clean.
- **Slot exhaustion:** surface used/remaining fabrics before the user hits the
  limit, matching how ADR-0005 treats slots as budgeted.

## 7. Failure modes

| Mode | Behaviour |
|---|---|
| Bridge node down | Plugin marks export status degraded; Indigo devices unaffected; agent restarted by launchd; reconcile on reconnect |
| Ecosystem sends a command for a deleted Indigo device | Endpoint already removed; if racing, return failure rather than silently dropping |
| Indigo device disabled | `Reachable` = false; accessory greys out |
| Allow-list emptied | Endpoints removed; agent stopped; pairings retained unless explicitly reset |
| Lock/valve command | Never auto-confirm destructive state changes; honour the inbound flood-safe/lock conventions |
| Endpoint map lost/corrupt | Refuse to auto-reallocate; surface an error and require an explicit rebuild, since silent reallocation duplicates accessories in every paired ecosystem |

## 8. Acceptance criteria

- **XAC1.** Fresh install: no bridge process running, nothing paired, nothing exported.
- **XAC2.** Exporting one relay starts the bridge node and yields a pairing code.
- **XAC3.** Bridge pairs into **Apple Home and at least one non-Apple ecosystem**
  from the same code, both controlling the device.
- **XAC4.** Command round-trip both directions within 500ms (XG2).
- **XAC5.** Endpoint IDs and `UniqueID`s survive plugin reload, bridge-node
  restart and Mac reboot with no accessory duplication (XG4).
- **XAC6.** An `indigo-matter`-created device is absent from the picker, verified
  by unit test as well as live (XNG3).
- **XAC7.** Removing a device from the allow-list removes the accessory cleanly
  from all paired ecosystems.
- **XAC8.** Disabling an Indigo device greys the accessory out rather than
  timing out.
- **XAC9.** Excluded device types appear in the picker as excluded, with reasons.
- **XAC10.** No matter.js import exists anywhere in Python — enforced by a test
  that greps the plugin source (XG6).

## 9. Milestones

| # | Milestone | Gating criterion |
|---|---|---|
| E0 | Bridge node skeleton | Node process starts, exposes an aggregator with one hard-coded endpoint, pairs into Apple Home |
| E1 | **Google Home pairing spike** | Determines whether an uncertified bridge pairs at all. **Runs before E2** — it gates what v1 can advertise |
| E2 | Local protocol + plugin client | Plugin drives endpoint create/remove over WS |
| E3 | Allow-list + UI-D dialog | Devices selectable with role; loop guard live (XAC6, XAC9) |
| E4 | Relay + dimmer export | XAC2, XAC3, XAC4 |
| E5 | Sensors + thermostat export | Mapping table complete for v1 |
| E6 | Endpoint persistence | XAC5 — the highest-risk correctness requirement |
| E7 | Pairing/unpairing UX + fabric readout | §6 complete |
| E8 | launchd agent + failure recovery | §7, XAC7, XAC8 |
| E9 | Docs | `INSTALL.md` export section, uncertified-prompt explanation, `MATTER.md` outbound architecture |

E0 and E1 are the validation loop. Until E1 resolves, v1's ecosystem claims are
unknown; everything after E2 is mechanical.

## 10. Open questions

- **XOQ1.** Does Google Home pair an uncertified test-VID bridge, and does it
  require Developer Console registration? **Blocking on scope claims** — E1.
- **XOQ2.** Alexa and SmartThings tolerance — assumed permissive, unverified.
- **XOQ3.** Does `server_process.py` generalise cleanly to a second agent, or
  does it need extracting first? Audit before E8.
- **XOQ4.** Which DAC/VID the bridge node presents, and whether matter.js's
  default test credentials are used as-is. Distinct from ADR-0005's *fabric*
  VID (ADR-0006, "Two different vendor IDs, same number").
- **XOQ5.** Whether role belongs in `indigo-device-catalog` now rather than as
  plugin-local metadata later — cross-repo, and it would unblock garage doors.
- **XOQ6.** Bridge-node memory footprint at 100+ endpoints on an 8GB jarvis,
  given the workspace's existing memory-pressure history.

## 11. Dependencies

- **matter.js** device-role API — new coupling, per ADR-0006's accepted cost.
- **Node.js ≥ 22.13.0** — already required.
- **Ecosystem tolerance of uncertified accessories** — external, unowned, XOQ1.
- **`indigo-device-catalog`** — soft dependency for v2 role defaults (XOQ5).

## 12. Out of scope for v1 (v2+ candidates)

- Role/polarity defaults sourced from `indigo-device-catalog`.
- Garage doors (blocked on the above).
- Sprinkler export as per-zone Water Valves.
- Power/energy export via Matter's Electrical Sensor type.
- CSA certification and a real vendor ID.
- Multiple bridge nodes / per-ecosystem export sets.
