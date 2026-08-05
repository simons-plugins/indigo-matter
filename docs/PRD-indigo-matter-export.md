# PRD — Indigo Matter Export (Indigo as a Matter bridge)

**Status:** Accepted — build in progress
**Owner:** Simon
**Governing ADR:** [`../../docs/adr/0006-indigo-as-matter-bridge.md`](../../docs/adr/0006-indigo-as-matter-bridge.md) (accepted 2026-08-03; workspace-level — the path resolves in the multi-repo workspace checkout, not on GitHub), as amended by ADR-0007 (validation-evidence criterion)
**Companion PRD:** [`PRD-indigo-matter-plugin.md`](./PRD-indigo-matter-plugin.md) (historical — the inbound/controller build)
**Local protocol spec:** [`BRIDGE_PROTOCOL.md`](./BRIDGE_PROTOCOL.md)
**Last updated:** 2026-08-04 (research pass: matter.js 0.17.8 verified by execution; scope and
packaging decisions taken — see §5.2 exclusions, §10, §11)

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
- Its data directory is **every bit as sacred as the controller's**, for
  different reasons: it holds the commissioned-fabric credentials for every
  paired ecosystem (losing them un-pairs everything) *and* the endpoint-ID
  allocation map (losing that duplicates every accessory in every ecosystem —
  §4.3). Back it up alongside the controller's storage.

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
  order. matter.js keys its persisted endpoint numbers solely on the string
  `Endpoint.id` (verified at 0.17.8): a stable `id` gives stable numbers across
  restarts, reorderings and removals, while an omitted `id` falls back to
  positional `part0/part1/…` and silently swaps identities when a device is
  removed. Therefore: `id` = the immutable Indigo device ID (sanitised),
  supplied explicitly, never reused, never mutated.
- **Drift detection.** The bridge node persists a `UniqueID → endpoint number`
  map and warns loudly if any mapping changes (matterbridge's
  `checkEndpointNumbers()` pattern) — the only way to make "is it us or the
  controller?" falsifiable in the field. The check runs **wherever a number can
  first be wrong**, not at startup: at startup there are no endpoints yet (the
  live set is created by the first `attach`), so a startup-only check would
  compare an empty set against the baseline and report a clean bridge every
  time. It therefore runs at the end of every operation that reshapes the live
  set — the first attach's reconcile, and every `upsert_endpoint` /
  `remove_endpoint` after it. Drift is **reported, never repaired**: an
  auto-repair would bless the storage loss that caused it and make the next
  occurrence invisible too.
- **Storage loss is the #1 real-world accessory-duplication cause** (not logic
  bugs): a missing/relocated storage dir reallocates every endpoint number and
  every ecosystem re-creates every accessory, losing names, rooms and
  automations. The storage path must survive plugin and Indigo upgrades, is
  backed up alongside the controller's, and "storage missing but previously
  commissioned" is a loud refuse-to-start (§7), never a silent re-init.

### 4.4 Local protocol

Bidirectional, because both directions carry traffic: the plugin pushes Indigo
state changes outward, and the bridge node pushes ecosystem commands inward.

**Decision:** mirror the controller's WebSocket + JSON message shape. It costs
nothing new to learn, `matter_client.py` is a working reference for the client
half, and the existing test doubles generalise. The bridge node listens on
loopback only, on a configurable port defaulting to **5581** (the controller's
WS is 5580).
Unlike `protocol.py` there is **no rename firewall** — we own both ends and ship
them together — but the handshake carries a `protocolVersion`, because launchd
deliberately keeps the old node running across plugin reloads and version skew
is the failure that will actually happen. Full spec: [`BRIDGE_PROTOCOL.md`](./BRIDGE_PROTOCOL.md).

The **Matter side** of the node binds UDP **5540** (the Matter default, also
pref-configurable) with the Aggregator at **Endpoint 1**. matter.js's
ECOSYSTEMS.md documents both as Alexa's hard requirement (it discovers nothing
on any other port and needs EP1 beside the root); we have not verified this
(XOQ1), but the constraint costs nothing now and is painful to retrofit. No
conflict with the controller: `matter-server` listens on TCP 5580 plus
ephemeral UDP, not 5540. But 5540 *is* contended by any other Matter device
stack on the same Mac — Homebridge 2.x, matterbridge, an HA container in host
mode — so a bind failure is a first-class §7 failure mode, surfaced with the
holder named, and the pref is the escape hatch (moving off 5540 forfeits the
documented Alexa behaviour). mDNS is pinned to the primary interface (reusing
the `primaryInterface` pref) — matter.js's own mDNS stack defaults to all
interfaces and breaks on Macs with VPN/utun interfaces.

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
fallback" treatment PM-B/PM-A got in the original PRD. (UI-C, a hybrid
variant, was folded into UI-D during drafting; the lettering gap is
deliberate.)

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
| Relay | Lock | Door Lock | Requires ecosystem PIN/confirm semantics; see §7. matter.js's DoorLock implementation is its most complete (users/credentials/schedules, encrypted at rest) — the risk is ecosystem UX, not the library |
| Relay | Valve | *Not exportable in v1* | Descoped 2026-08-04; see below |
| Relay | Garage door | *Not exportable in v1* | Polarity + safety; see below |
| Dimmer | Light | Dimmable Light | |
| Dimmer (colour) | Light | Extended Color Light | Colour-temp-only devices → Color Temperature Light |
| Dimmer | Window covering | Window Covering | Polarity declared per export (100% = open, inbound convention). Must implement `handleMovement()` — matter.js's default snaps to target instantly |
| Dimmer | Fan | *Not exportable in v1* | Descoped 2026-08-04; see below |
| Sensor (binary, motion) | — | Occupancy Sensor | |
| Sensor (binary, contact) | — | Contact Sensor | |
| Sensor (numeric, °C) | — | Temperature Sensor | |
| Sensor (numeric, %RH) | — | Humidity Sensor | |
| Sensor (numeric, lux) | — | Light Sensor | |
| Sensor (numeric, pressure) | — | Pressure Sensor | Apple Home ignores this type (Google supports it); exported anyway, documented |
| Sensor (numeric, flow) | — | Flow Sensor | Apple Home ignores this type (Google supports it); exported anyway, documented |
| Thermostat | — | Thermostat | Setpoints, modes. No fan in v1 (the FanControl descope applies here too); v2 candidate. matter.js provides the cluster machinery; the HVAC logic is ours |
| SpeedControl | Fan | *Not exportable in v1* | Descoped 2026-08-04; see below |

Matter device-type IDs are deliberately omitted here; take them from the
matter.js device-type catalogue at implementation rather than transcribing
them into a PRD where they can rot.

**Explicitly not exportable in v1:**

| Excluded | Why |
|---|---|
| Any device created by `indigo-matter` | Loop guard (XNG3), enforced at §5.1 |
| Valve role | **Descoped 2026-08-04.** matter.js's `ValveConfigurationAndControlServer` is an empty stub — the whole command surface would be ours to implement — and ecosystem support is poor (Apple unresolved, Alexa ignores the type). v2 candidate |
| Fan role (Dimmer- or SpeedControl-backed) | **Descoped 2026-08-04.** matter.js's `FanControlServer` only seeds a default `fanMode`; all fan behaviour would be ours to implement. v2 candidate |
| Sprinkler devices | Matter has no irrigation-controller type; per-zone Water Valve is a lossy fit (and Water Valve itself is descoped). v2 candidate |
| MultiIO devices | No coherent single-accessory representation |
| `custom` devices with no resolvable role | Includes the plugin's own energy-meter type |
| Garage doors | Needs the polarity handling the catalog doesn't yet carry (`onState` true = closed, turnOn = close), and mis-mapping is a physical-safety issue. Blocked on the catalog role/polarity work |
| Sensors with units outside the table | No faithful Matter sensor type |

Excluded devices must **appear in the picker as excluded, with reasons** (XAC9),
not silently missing.

### 5.3 Bridge node

One Matter node: root endpoint, an **Aggregator** endpoint, and one **Bridged
Node** child endpoint per exported device carrying Bridged Device Basic
Information (`NodeLabel`, `Reachable`, `UniqueID`) — the standard bridge
topology the plugin already consumes inbound (the "Bridges" section of
`MATTER.md`; in code, `matter_model.py`'s BridgedDeviceBasicInformation
handling and `device_sync.py`'s `DEVICE_TYPE_AGGREGATOR`).

- **Distribution:** the bridge node is a **published npm package**
  (`indigo-matter-bridge`, TypeScript, decided 2026-08-04), exact-pinned by the
  plugin the same way `matter-server@1.2.2` is — the existing
  `npm install --prefix` machinery works unchanged. matter.js itself is
  **exact-pinned** (no caret): patch releases have changed what Apple Home
  renders with zero code change on the bridge side.
- **Max fabrics:** matter.js defaults `supportedFabrics` to 254, so ≥5
  concurrent ecosystems needs no action. Note Apple consumes **two** slots
  (iCloud Keychain sync).
- **Reachable** must track the Indigo device's enabled/available state so
  ecosystems grey out unavailable accessories instead of timing out. Prefer
  `Reachable = false` over endpoint removal for anything temporary.
- **Removal:** dropping a device from the allow-list removes its endpoint and
  updates the aggregator's `PartsList` (automatic in matter.js); ecosystems
  remove the accessory. Bulk removals are rate-limited (~100ms apart,
  matterbridge's pattern) so controllers see one subscription update each.
- **Echo guard:** matter.js attribute-change events fire for our own
  Indigo-originated writes as well as controller commands; the bridge node
  discriminates on the event context (`ctx.offline`) or the Indigo↔ecosystem
  loop is infinite.
- **Endpoint count:** no hard cap in v1, but log a warning past ~100 exports —
  ecosystem per-home accessory limits will bite first (Alexa hard-caps at 50
  bridged devices; Apple degrades past ~200). Memory is not the constraint:
  measured ~145MB RSS floor + ~0.3MB per endpoint at 0.17.8 (XOQ6 answered).

### 5.4 Lifecycle hooks

- `startup` — if the allow-list is non-empty, ensure the bridge agent is
  installed and running; connect; reconcile endpoints against the allow-list.
- `shutdown` — close the socket; leave the agent running (PM-B rationale:
  plugin reloads must not un-pair ecosystems).
- `deviceUpdated` — diff relevant states, push outward.
- `deviceDeleted` — remove from allow-list, drop the endpoint.
- `runConcurrentThread` — health check, reconnect, reconcile drift, and
  periodic bridge-node RSS logging (the XOQ6 watchdog).

### 5.5 Configuration UI

Plugin config gains an **Export** section: enable/disable export wholesale,
the two bridge-node ports (local-protocol WS, default 5581; Matter UDP,
default 5540), and a pairing-status readout (which ecosystems are paired,
fabric slots used/remaining). The readout earns its place even though
matter.js defaults to 254 fabric slots: what users actually need to see is
*which* ecosystems hold a fabric and whether a commissioning window is open,
not slot arithmetic. Per-export settings live in the §5.1 dialog.

## 6. Pairing and fabric management

- **Pair:** a menu action surfaces a pairing code and QR payload. The user
  adds the bridge in each ecosystem's app as they would any Matter accessory.
  A commissioning passcode is **not durable**: once the first ecosystem
  commissions, the original code stops working, and each further admin needs
  an *enhanced commissioning window* with a freshly derived code — so the
  menu action is "open a pairing window" (`open_commissioning_window`,
  BRIDGE_PROTOCOL §3.8), not "show the code". **Display mechanism** (Indigo
  dialogs have no dynamic labels and no image fields): the manual code is
  written to the event log — the plugin's established pattern for runtime
  strings — and the QR is rendered on an IWS-served page, reachable from the
  same menu action. Passcode and discriminator are **randomised per install**
  (identical passcodes produce identical pairing codes across installs —
  verified) and persisted by the bridge node.
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
| Allow-list emptied | Endpoints removed (the deliberate `intent: "replace_all"` path, BRIDGE_PROTOCOL §3.1); agent stopped; pairings retained unless explicitly reset |
| Lock command | Never auto-confirm destructive state changes; honour the inbound lock conventions |
| Matter UDP port (5540) already bound | Another Matter device stack (Homebridge 2.x, matterbridge, HA) holds it; surface the error naming the holder; the port pref is the escape hatch (§4.4) |
| Endpoint map **corrupt** (present, unreadable) on a commissioned bridge | Refuse to serve endpoints; surface an error and require an explicit rebuild (`rebuild_endpoint_map`, BRIDGE_PROTOCOL §3.11), since silent reallocation duplicates accessories in every paired ecosystem. The unusable file is copied aside first — it is the only surviving record of the old numbers |
| Endpoint map **absent** on a commissioned bridge | **Bootstrap a baseline and serve** — do *not* refuse. matter.js owns the numbers (keyed on `Endpoint.id` in its own store) and this map is only the independent witness, so a missing witness renumbers nothing; it means we cannot yet *check*. Every install commissioned before the map existed is in exactly this state, and refusing there would take working exports offline on upgrade to fix a file that never existed |
| Endpoint map missing/corrupt on a bridge that has **never** been commissioned | Serve normally. Endpoint numbers only matter to somebody who is paired |
| Bridge `identity.json` present but unreadable | Refuse to serve endpoints, and **a rebuild cannot fix it** — the node refuses one. Minting a replacement would change the `SerialNumber`/`UniqueID` every paired ecosystem remembers, silently un-pairing the lot. The file is moved aside as `identity.json.unreadable-<stamp>`; the refusal is sticky across restarts until it is restored, repaired, or deliberately deleted |

## 8. Acceptance criteria

- **XAC1.** Fresh install: no bridge process running, nothing paired, nothing exported.
- **XAC2.** Exporting one relay starts the bridge node and yields a pairing
  code, within XG1's 30 seconds. (Lands at E7 — the start-on-export wiring.)
- **XAC3.** Bridge pairs into **Apple Home** from the displayed code and
  controls the device. Multi-fabric capability is proven by adding a **second
  admin we already own** — Domio's own fabric (ADR-0005) or a matter-server
  controller — not by requiring a third-party ecosystem we cannot test (XOQ1).
  This is a deliberate narrowing of ADR-0006's confirmation criterion,
  recorded in **ADR-0007**.
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
| E0 | Bridge node skeleton — **the validation gate** | Node process starts, exposes an aggregator with one hard-coded endpoint, and **pairs into Apple Home**. If an uncertified bridge will not pair here, the design is dead and nothing after this matters |
| E1 | Local protocol + plugin client | Plugin drives endpoint create/remove over WS |
| E2 | Allow-list + UI-D dialog | Devices selectable with role; loop guard live (XAC6, XAC9) |
| E3 | Relay + dimmer export | XAC4 both directions, and XAC3's Apple Home control, against a manually started bridge node (start-on-export is E7's; the code display is E6's) |
| E4 | Sensors + thermostat export | Mapping table complete for v1 |
| E5 | Endpoint persistence | XAC5 — the highest-risk correctness requirement |
| E6 | Pairing/unpairing UX + fabric readout — **BUILT** (2026-08-05, plugin `2026.8.1`) | §6 complete, including XAC3's displayed-code pairing flow. "Pair Matter Bridge…" (§3.8, duration 180–900s), the QR page over IWS, "Unpair an Ecosystem…" (§3.9, two gates), the §5.5 Export config section, and the §5 pairing events surfaced in the log. **XAC3's live pairing is unverified** — it needs jarvis and a real ecosystem |
| E7 | launchd agent + failure recovery — **BUILT** (2026-08-05, plugin `2026.8.1`, bridge `0.5.0`) | §7, XAC1, XAC2, XAC7, XAC8. `bridge_agent.BridgeProcess` is the second `AgentSpec`; it is installed and started by the empty→non-empty allow-list transition and stopped after the un-export lands. `remove_package` is per-package. **`indigo-matter-bridge` is not yet on the npm registry** — publishing is Simon's action (`docs/HANDOVER.md` → "Publishing the bridge node"), and until then the install action cannot resolve the pinned spec |
| E8 | Docs | `INSTALL.md` export section, uncertified-prompt explanation, ecosystems-untested note (§10), `MATTER.md` outbound architecture |

**E0 is the whole validation loop.** It answers the only question that can kill
the feature — *will any ecosystem pair an uncertified bridge?* — on hardware
already present for the TBR. E0 runs **on jarvis** (decided 2026-08-04): the
real deployment host, same L2 as the Apple hub. Everything after E1 is
well-understood, though not all of it is easy — E5 remains the highest-risk
correctness milestone (§4.3).

**Sequencing note (XOQ3 outcome):** the `AgentSpec` extraction of
`server_process.py` lands as its own behaviour-preserving PR between E0 and
E1, so E7 is wiring, not refactoring.

There is deliberately **no per-ecosystem spike**. A test earns its place by
changing a decision, and a Google Home or Alexa result changes none: we ship
to whatever pairs, and the remedies for a refusal — a real vendor ID, or
per-ecosystem developer-console registration — are weighed and declined in
**ADR-0007**. Those ecosystems are therefore untested-and-unclaimed (§10),
not blockers.

## 10. Open questions

- **XOQ1.** Whether Google Home pairs an uncertified test-VID bridge (it may
  require Developer Console registration), and likewise Alexa and SmartThings.
  **Not blocking, and not scheduled.** No hardware to test them on, no decision
  hangs on the answer, and the only fix for a refusal — a real vendor ID — is
  out of scope by §12. Treat as **untested and unclaimed**: promise Apple Home
  in the docs, say nothing about the rest, and let the first user report settle
  it. Revisit only if one of them becomes a hard requirement. The scope
  narrowing relative to ADR-0006's confirmation criterion is recorded in
  ADR-0007. (XOQ2, Alexa/SmartThings tolerance, was folded into this question
  in the 2026-08-04 revision; the numbering gap is deliberate.)
- **XOQ3. Answered (2026-08-04 audit).** It needs extracting first: identity
  (launchd label, package name, stamp files, log names, argv, port) is
  module-global, and two agents sharing the applied-plist stamp would trigger
  spurious bootout cycles. But ~700 of its 1092 lines parameterise cleanly
  behind a frozen `AgentSpec`; the extraction is behaviour-preserving and is
  **pulled forward to before E1** so the hard-won recovery machinery (plist
  digest, loaded-but-dead recovery, orphan reaper) is never duplicated.
- **XOQ4. Answered (2026-08-04 research).** matter.js hardcodes no VID: it
  generates a fresh self-signed PAA→PAI→DAC chain at runtime for whatever
  `vendorId`/`productId` is configured, with a Certification Declaration
  signed by the CHIP *development* CD key (`certificationType: Test`). The
  0xFFF1/0x8000 values come from its examples, not the library. v1 uses the
  test-range VID 0xFFF1 deliberately (uncertified is the honest posture,
  ADR-0006); still distinct from ADR-0005's *fabric* VID.
- **XOQ5.** Whether role belongs in `indigo-device-catalog` now rather than as
  plugin-local metadata later — cross-repo, and it would unblock garage doors.
- **XOQ6. Answered (2026-08-04, measured at 0.17.8):** ~145MB RSS floor,
  ~0.3MB per additional endpoint, ~177MB at 100 endpoints, startup <0.5s.
  Budget ~200MB on jarvis — comparable to one more mid-sized plugin, and far
  below the MQTT-leak class of problem. Watchdog still tracks it (§5.4).

## 11. Dependencies

- **matter.js** device-role API — new coupling, per ADR-0006's accepted cost.
  Concretely: `@matter/main` + `@matter/nodejs` (0.17.8 at time of writing),
  **exact-pinned**. Upstream repo is now the `matter-js` GitHub org (Open Home
  Foundation). Do **not** depend on `@matter/examples` — stale on npm; the
  live examples are in the repo's `examples/` tree.
- **Node.js ≥ 22.13.0** — already required.
- **Ecosystem tolerance of uncertified accessories** — external, unowned, XOQ1.
  Apple Home's "Add Anyway" flow is the documented, working path (Homebridge
  2.0 and Homey ship on it); Apple DTS's position on uncertified bridges is
  "behaviour is undefined", so it can tighten under us — a risk we accept.
- **`indigo-device-catalog`** — soft dependency for v2 role defaults (XOQ5).

## 12. Out of scope for v1 (v2+ candidates)

- Role/polarity defaults sourced from `indigo-device-catalog`.
- Garage doors (blocked on the above).
- Valve export (matter.js cluster is an empty stub; ecosystem support poor).
- Fan export (matter.js cluster is a stub; includes SpeedControl-backed fans).
- Sprinkler export as per-zone Water Valves.
- Power/energy export via Matter's Electrical Sensor type (Apple ignores it in UI).
- CSA certification and a real vendor ID.
- Multiple bridge nodes / per-ecosystem export sets. (If a device class ever
  destabilises a whole bridge in Apple Home — the matterbridge RVC precedent —
  per-device isolation onto its own server node is the known fix; design the
  endpoint model so that promotion doesn't require re-architecture.)
