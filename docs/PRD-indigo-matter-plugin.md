# PRD — Indigo Matter Plugin (`indigo-matter`)

> **HISTORICAL (2026-06-10).** This PRD is the original build spec, preserved
> as-written; it is no longer maintained. **Everything in it shipped**, and so
> did most of its own §14 "v2+ candidates" (door lock, window covering,
> smoke/CO, air quality, energy metering, fabric backup/restore — all live).
> A real Wi-Fi energy plug has been validated end-to-end via Domio.
>
> Where this document disagrees with reality, the living docs win:
> [`API.md`](./API.md) for the wire contract (the §5.1 endpoint shapes here
> are stale — e.g. decommission is now `POST …?nodeId=`, not `DELETE`),
> [`MATTER.md`](./MATTER.md) for architecture/landscape (incl. the corrected
> Thread position), and [`INSTALL.md`](./INSTALL.md) for setup. The architecture decision
> (the share model) is summarised in [`MATTER.md`](./MATTER.md), not the 0001 path below.
>
> Still genuinely pending from this PRD: **M11 — Plugin Store submission**,
> and OQ4's "Indigo admin removed externally" detection beyond basic
> availability tracking.

**Status:** Historical — shipped (see banner)
**Owner:** Simon
**Related ADR:** maintained privately; the decision is summarised in [`MATTER.md`](./MATTER.md)
**Companion PRD:** Domio Matter Commissioning — also shipped
**Last updated:** 2026-05-15 (content); 2026-06-10 (status banner)

## 1. Summary

A new Indigo plugin that brings Matter device support to the Indigo Domotics smart home server. The plugin manages a `matterjs-server` instance, holds the Indigo fabric's identity, exposes an HTTP endpoint for Domio to deliver newly commissioned devices, and translates between Matter clusters and Indigo device types so Matter devices behave as first-class Indigo devices for triggers, schedules, action groups, control pages, and external API consumers.

This is the bulk of the Matter work. Domio handles commissioning UX (one-shot per device); this plugin handles everything else for the lifetime of every Matter device on the system.

## 2. Goals

- **G1.** Any Matter device commissioned via Domio is automatically created as one or more Indigo devices with appropriate types and states, no manual config needed beyond naming.
- **G2.** State changes on the device (a button press, a temperature reading, an occupancy event) propagate to Indigo within 500ms under normal conditions.
- **G3.** Indigo commands (turn on, set level, set thermostat setpoint) reach the device within 500ms under normal conditions and reflect in the Indigo device state on confirmation.
- **G4.** v1 cluster scope: **Lighting** (OnOff, LevelControl, ColorControl), **Sensors** (TemperatureMeasurement, RelativeHumidityMeasurement, OccupancySensing, ContactSensor / BooleanState, IlluminanceMeasurement), and **Thermostats** (Thermostat cluster + FanControl when present).
  - **\*Scope-at-risk: any sensor or thermostat that ships only as Matter-over-Thread.** Wi-Fi variants of these clusters work in v1; Thread variants require matter-server Thread support which is non-functional in v0.6.2. See IMPLEMENTATION.md §2.7 for contingency options.
- **G5.** The plugin survives `matterjs-server` crashes, Mac sleep/wake cycles, and LAN outages without losing devices or fabric identity.
- **G6.** Adding new clusters in future versions is a mechanical, isolated change — the cluster mapping layer is the only place that needs editing.

## 3. Non-Goals

- **NG1.** No commissioning UX in this plugin. Domio owns that. (The plugin does expose a developer-only `/matter/commission` endpoint that takes a setup code, but no user-facing UI.)
- **NG2.** No Thread Border Router functionality. Apple TV provides that.
- **NG3.** No Zigbee involvement. The Sonoff Dongle Max continues to serve Zigbee separately.
- **NG4.** No support for clusters outside G4's scope in v1. Lock, Window Covering, Smoke/CO Alarm, Air Quality, Energy reporting are explicit v2 candidates.
- **NG5.** No Indigo-fabric backup/restore UX in v1. Manual `matterjs-server` storage backup only.
- **NG6.** No multi-instance support — one plugin instance per Indigo Server.

## 4. Architecture

### 4.1 Process topology

```
┌─────────────────────────────────────────────────────────────────┐
│                       Indigo Server (macOS)                      │
│                                                                  │
│  ┌──────────────────┐                                            │
│  │ indigo-matter    │   WebSocket    ┌─────────────────────┐    │
│  │ plugin (Python)  │ ◄────────────► │ matterjs-server      │    │
│  │                  │  (JSON-RPC)    │ (Node.js)            │    │
│  │ - HTTP server    │                │                      │    │
│  │   (for Domio)    │                │ - Indigo fabric CA   │    │
│  │ - WS client      │                │ - mDNS discovery     │    │
│  │ - Cluster mapper │                │ - CASE sessions      │    │
│  │ - Indigo dev     │                │ - Storage @          │    │
│  │   state sync     │                │   ~/Library/Application│  │
│  └────────┬─────────┘                │   Support/.../matter │    │
│           │                          └──────────┬───────────┘    │
│           │ indigo.server (Python API)          │ IPv6 / mDNS    │
│           ▼                                     ▼                │
│      Indigo Database                     LAN ─► Apple TV (TBR)   │
│                                                 │                │
└─────────────────────────────────────────────────┼────────────────┘
                                                  ▼
                                       Thread mesh / Wi-Fi devices
```

### 4.2 Process management — open question

The plugin and `matterjs-server` are two processes that must stay in sync. Three plausible approaches:

- **PM-A.** Plugin spawns and supervises `matterjs-server` as a child process. Pro: single lifecycle, simple deploy. Con: plugin reloads kill the server unnecessarily; Node failures harder to debug.
- **PM-B.** `matterjs-server` runs as a separate `launchd` LaunchAgent. Pro: independent lifecycle, survives plugin reloads, standard macOS pattern. Con: installation more involved (plist deploy + load); plugin can't directly tail Node logs.
- **PM-C.** Hybrid — `launchd` for the long-running server, plugin uses `launchctl` to start/stop/reload during plugin lifecycle events.

**Decision:** make this a focused implementation-phase decision and document as a follow-up ADR. Recommended starting point is **PM-B** because it survives Indigo plugin reloads (frequent during development) without restarting the Matter server (slow to start, holds device sessions). If the launchd installation friction proves too high, fall back to PM-A.

### 4.3 Storage

- **`matterjs-server`** owns its own storage directory (containing fabric private keys, certificates, node operational data). Path: `~/Library/Application Support/com.simons-plugins.indigo-matter/matter-server/`.
- **Plugin** owns Indigo device pluginProps mapping `(nodeId, endpointId, clusterId) → indigoDeviceId`. Stored in Indigo's normal plugin storage; backed up with Indigo's database.
- Storage must survive plugin upgrades. Treat the matter-server data dir as sacred — losing it loses the fabric and all device pairings.

## 5. Components

### 5.1 HTTP API (for Domio)

Served by the **Indigo Web Server (IWS)** as hidden-action handlers (not a standalone
server — `aiohttp` is absent from the Indigo framework Python; IWS is the idiomatic
mechanism and rides the Reflector for remote access). Authentication is Indigo's existing
Reflector/API-key auth, enforced before the handler runs. See `API.md` v1.1 (authoritative
for the wire shape) and `IMPLEMENTATION.md` §4.

**Endpoints** (logical; actual paths are `…/message/com.simons-plugins.indigo-matter/{handler}` — see `API.md`):

```
GET /matter/status
  → 200 { ready: bool, controllerVersion: string, fabricId: string,
          nodeCount: int, matterServerReachable: bool }

POST /matter/commission
  Body: {
    setupCode: string,    // 11-digit numeric or "MT:..." QR payload
    discriminator: int,
    suggestedName: string,
    suggestedRoom: string?,
    domioNodeId: string?  // for logging/correlation
  }
  → 202 { jobId: string }
  → 409 { error: "duplicate", existingJobId: string } if same setupCode in-flight

GET /matter/commission/{jobId}
  → 200 {
      status: "pending"|"commissioning"|"reading_descriptors"|
              "creating_devices"|"success"|"failed",
      progress: float,    // 0.0–1.0
      result?: {
        nodeId: string,
        indigoDeviceIds: int[],
        primaryDeviceId: int  // the one to navigate to in Domio
      },
      error?: { code: string, message: string }
    }

DELETE /matter/devices/{nodeId}
  → 204    decommission and remove from Indigo fabric, delete Indigo devices

GET /matter/devices/{nodeId}/diagnostics
  → 200 { reachable, lastSeen, vendorId, productId, swVersion,
          fabrics: [...], thread?: {...}, wifi?: {...} }
```

### 5.2 WebSocket client (to matterjs-server)

Uses the documented `matterjs-server` / `python-matter-server` WebSocket API (JSON-RPC-ish). Responsibilities:

- Maintain persistent connection with automatic reconnect (exponential backoff, max 30s).
- On connect: enumerate all nodes, subscribe to attribute changes for every cluster the plugin understands (G4 scope).
- Handle incoming push messages (`attribute_updated`, `node_added`, `node_removed`, `node_unreachable`).
- Send `commission_with_code`, `interview_node`, `invoke_command`, `remove_node`, `open_commissioning_window` requests.

### 5.3 Cluster mapping layer

The most important and most testable component. A registry of cluster handlers, each implementing:

```python
class ClusterHandler(ABC):
    cluster_id: int
    cluster_name: str

    @abstractmethod
    def create_indigo_devices(self, node, endpoint) -> list[IndigoDeviceSpec]:
        """Build Indigo device(s) for an endpoint exposing this cluster."""

    @abstractmethod
    def attributes_to_subscribe(self) -> list[int]:
        """Attribute IDs to subscribe to on this cluster."""

    @abstractmethod
    def on_attribute_update(self, indigo_dev, attribute_id, value) -> dict:
        """Translate a Matter attribute change to Indigo state updates."""

    @abstractmethod
    def handle_indigo_action(self, indigo_dev, action) -> MatterCommand | None:
        """Translate an Indigo device action to a Matter command."""
```

This isolates Matter-spec knowledge into one file per cluster, making future cluster additions a contained change. Composition handles devices exposing multiple clusters on one endpoint (e.g. OnOff + LevelControl + ColorControl → one Indigo dimmer with color states).

### 5.4 Cluster → Indigo device-type mapping (v1)

| Matter cluster(s) on endpoint | Indigo device type | Indigo states |
|---|---|---|
| OnOff only | Relay | onState |
| OnOff + LevelControl | Dimmer | onState, brightnessLevel |
| OnOff + LevelControl + ColorControl | Dimmer (color) | + hue, saturation, colorTemperature, colorMode |
| TemperatureMeasurement | Sensor (temperature) | sensorValue (°C), batteryLevel if present |
| RelativeHumidityMeasurement | Sensor (humidity) | sensorValue (%RH) |
| OccupancySensing | Sensor (motion / occupancy) | onOffState (boolean) |
| ContactSensor / BooleanState | Sensor (contact) | onOffState (boolean), contactSensorClosed |
| IlluminanceMeasurement | Sensor (illuminance) | sensorValue (lux) |
| Thermostat | Thermostat | hvacMode, hvacFanMode, temperatureInputs, setpoints |
| FanControl (on thermostat endpoint) | merged into Thermostat | fanMode, fanSpeedSetpoint |
| Multi-cluster endpoints not matching above | "Unknown Matter device" placeholder relay | onOffState if OnOff present, otherwise stub |

Bridged devices: each bridged endpoint advertised by a Matter bridge node becomes its own Indigo device, applying the above mapping recursively.

### 5.5 Lifecycle hooks (Indigo plugin API)

- `startup` — start (or attach to) matterjs-server, open WebSocket, sync devices.
- `shutdown` — close WebSocket cleanly. Whether to stop matterjs-server depends on PM-A/B/C decision.
- `deviceStartComm` / `deviceStopComm` — track which Indigo devices are active, gate state updates to active devices only.
- `actionControlDevice`, `actionControlSensor`, `actionControlThermostat` — dispatch to cluster mapper.
- `validateDeviceConfigUi`, `validatePrefsConfigUi` — config validation.
- `runConcurrentThread` — periodic health check, reconnect if WebSocket has dropped, prune stale subscriptions.

### 5.6 Configuration UI

Plugin config dialog exposes:

- Path to matterjs-server data dir (default: per §4.3).
- matterjs-server port (default: 5580, matching python-matter-server convention).
- Plugin HTTP server port for Domio API (default: TBD; needs to coexist with other Indigo plugins).
- Authentication settings for the HTTP API (re-use Indigo API token).
- Diagnostic toggles: verbose Matter logging, WebSocket message tracing.

Per-device config dialog exposes:

- Read-only: node ID, endpoint ID, vendor, product, software version, last-seen timestamp.
- Editable: friendly name (synced to Matter NodeLabel attribute).
- Action button: "Refresh device interview" — re-reads descriptors, useful if device firmware adds capabilities.
- Action button: "Decommission" — removes from Indigo fabric and deletes Indigo devices.

## 6. State Synchronisation

### 6.1 On startup

1. Connect to matterjs-server, wait for `ready`.
2. List all nodes.
3. For each node, list endpoints and clusters. Reconcile against pluginProps:
   - Node known → ensure Indigo devices exist, re-create any that were deleted out-of-band, update vendor/product fields.
   - Node unknown → log warning; happens if a device was commissioned outside this plugin (e.g. via matter.js CLI for testing). Auto-create Indigo devices using cluster mapper.
   - Indigo device known but node missing → mark device unreachable, do not delete (user may have device temporarily offline).
4. Subscribe to all relevant attributes for all known nodes.
5. Start HTTP server.

### 6.2 Attribute push from device

```
matterjs-server WS push → plugin parses → look up Indigo device by
(nodeId, endpointId) → dispatch to cluster handler → handler returns
{state: value, ...} dict → indigo.dev.updateStatesOnServer([...])
```

Idempotent: pushing the same value twice is a no-op.

### 6.3 Indigo command

```
Indigo trigger / Action Group / Domio → plugin.actionControlDevice(action, dev)
→ look up cluster handler from dev.deviceTypeId → handler builds MatterCommand
→ send invoke_command over WS → await ack (5s timeout) → update Indigo state
on confirmation, or revert on failure.
```

Optimistic update optional per cluster: on/off optimistic is safe, brightness probably not.

### 6.4 Device unreachable

- After 3 consecutive failed pings or 60s without push from a subscribed device, mark Indigo device state `errorState = "unreachable"`.
- Continue retrying with exponential backoff.
- When the device returns, clear errorState and re-sync attributes.

## 7. Commissioning (server-side, post-Domio)

When `POST /matter/commission` arrives:

1. Validate setup code format.
2. Generate jobId, return 202.
3. Async:
   a. Call `commission_with_code` on matterjs-server.
   b. Update job state through phases (commissioning → reading_descriptors → creating_devices).
   c. On matterjs-server completion, run cluster mapper to create Indigo devices.
   d. Apply suggestedName: Indigo device name = suggestedName, or suggestedName + " (endpoint N)" for multi-endpoint devices.
   e. Set room if Indigo has rooms configured.
   f. Persist (nodeId → indigoDeviceIds) mapping in pluginProps.
   g. Mark job `success`, return result on next poll.
4. On any failure, attempt to remove the node from matterjs-server (best-effort), mark job `failed` with structured error code.

## 8. Failure Modes & Recovery

| Failure | Detection | Behaviour | User-visible state |
|---|---|---|---|
| matterjs-server not running at startup | Connect timeout | Retry with backoff. Log clearly. | Plugin status: "Matter server not running" |
| matterjs-server crashes mid-run | WS disconnect | Reconnect (PM-A: respawn; PM-B: rely on launchd KeepAlive). | Devices briefly marked unreachable. |
| Apple TV (TBR) offline | Thread devices stop reporting | Mark affected devices unreachable after timeout. | Sensor states show "unreachable". |
| Mac sleeps | OS event | On wake, force WS reconnect and resubscribe. | Brief unreachability, auto-recovers. |
| LAN IP change | Device pings fail | matterjs-server's mDNS resolves new address. Plugin retries. | Brief unreachability. |
| Indigo plugin reloaded | shutdown → startup | If PM-B: matter-server keeps running, plugin reconnects without losing fabric. | No user-visible state loss. |
| Fabric corruption | matter-server fails to start | Plugin surfaces critical error. Manual restore from backup required. | All Matter devices unreachable until fixed. |

## 9. Logging & Diagnostics

- Indigo plugin logs to its standard Indigo log file.
- Distinct log levels: error, warning, info, debug, trace (WS message dump).
- Default level info; trace gated by per-plugin debug toggle.
- Matter-specific log file `~/Library/Logs/indigo-matter/matter.log`, rotated daily, retain 7 days.
- `GET /matter/devices/{nodeId}/diagnostics` returns a JSON snapshot for Domio's future device-detail diagnostics view.

## 10. Acceptance Criteria

- **AC1.** Plugin installs from a release zip into Indigo's Plugins folder following standard process.
- **AC2.** First-launch creates Indigo fabric, persists keys, ready state reported to Domio within 30s.
- **AC3.** Commissioning a Tapo Wi-Fi plug via Domio results in an Indigo Relay device created with correct name/room within 60s of `POST /matter/commission` returning.
- **AC4.** Plug can be turned on/off from Indigo Action Group; physical plug responds within 500ms; Indigo state reflects within 500ms of physical change.
- **AC5.** A Matter bulb (OnOff + LevelControl + ColorControl) creates one Indigo Dimmer device with brightness, hue/saturation states. All three controllable independently.
- **AC6.** A Thread-based motion sensor (OccupancySensing) creates an Indigo motion sensor device. Motion events trigger Indigo Triggers within 1s of physical event.
- **AC7.** A Thread-based temp/humidity sensor (TemperatureMeasurement + RelativeHumidityMeasurement on one endpoint) creates **two** Indigo sensor devices, both updating.
- **AC8.** A Matter thermostat creates an Indigo Thermostat device. Setpoint changes from Indigo reach the device; reported temperature is reflected.
- **AC9.** Killing matterjs-server during operation causes Indigo devices to be marked unreachable; restart restores state without manual intervention or data loss.
- **AC10.** Reloading the Indigo plugin (PM-B configuration) does not interrupt device control for more than 5s.
- **AC11.** Decommission via `DELETE /matter/devices/{nodeId}` removes the node from the fabric and deletes Indigo devices cleanly. The device can be re-commissioned afterwards.

## 11. Milestones

| # | Milestone | Gating criterion |
|---|---|---|
| M0 | Repo + plugin skeleton | Empty plugin loads in Indigo, version visible in plugin list |
| M1 | matterjs-server running standalone | Server starts on Mac, accepts WS connections, persists data |
| M2 | WS client + fabric init | Plugin creates Indigo fabric, status endpoint returns ready=true |
| M3 | HTTP server + commission endpoint (manual code) | Can paste a setup code into a curl call and commission a real device |
| M4 | OnOff cluster end-to-end | Tapo plug controllable from Indigo Action Group (AC4) |
| M5 | LevelControl + ColorControl | Color bulb works (AC5) |
| M6 | Sensors cluster pack *(Thread-dependent — see IMPLEMENTATION.md §2.7. Use Wi-Fi sensor variants for v1 if Thread blocked in matter-server.)* | TemperatureMeasurement, RelativeHumidityMeasurement, OccupancySensing, ContactSensor, IlluminanceMeasurement (AC6, AC7) |
| M7 | Thermostat cluster *(Wi-Fi thermostats only for v1 unless Thread support lands.)* | Setpoint/mode round-trip (AC8) |
| M8 | Failure recovery & lifecycle | AC9, AC10, AC11 |
| M9 | Domio integration test | Full path Domio → commission → control (gates Domio M5) |
| M10 | Process management ADR + launchd integration | PM-B installed cleanly, ADR written |
| M11 | Plugin Store submission | Documented install, license, README, screenshots |

M1–M4 form the architectural validation loop from the ADR (Confirmation section). Until M4 lands, the architecture is unproven; once it lands, the rest is mechanical.

## 12. Open Questions

- **OQ1.** Process management (PM-A vs PM-B vs PM-C). Defer to implementation, resolve before M10. Follow-up ADR expected.
- **OQ2.** Plugin's HTTP server port — does it conflict with anything on this Mac? Audit existing listeners pre-M3.
- **OQ3.** How matterjs-server reports thermostat capabilities — single endpoint vs split heat/cool/fan endpoints — needs verification with a real device before M7.
- **OQ4.** Multi-fabric devices: if a device has its Indigo admin removed externally (e.g. user fat-fingers in Apple Home), how does the plugin detect and surface this? Likely: matterjs-server reports node unreachable on next CASE attempt; plugin marks errorState and offers re-commission UI in device config.
- **OQ5.** Whether to support energy/power reporting from smart plugs (ElectricalMeasurement / ElectricalEnergyMeasurement clusters) in v1 or defer to v2. Currently leaning v2 per G4 scope; revisit if it falls out of OnOff plug implementation easily.

## 13. Dependencies

- **matter-server**: external Node.js project (npm package `matter-server`, GitHub `matter-js/matterjs-server`), **Alpha status** (v0.6.2 as of latest). Pin to a specific version. Thread commissioning currently non-functional; Wi-Fi unaffected. The plugin's WebSocket client should be designed to be portable to python-matter-server as a Thread fallback contingency.
- **Node.js**: macOS install via Homebrew or bundled binary — decision per process-management resolution.
- **Apple TV**: must remain online for Thread devices (architecture-level dependency, not plugin-specific).
- **Indigo Server**: 2024.1+ (verify Python version compatibility).
- **Domio**: companion PRD, but plugin must function standalone via curl/HTTP for development before Domio integration is complete.

## 14. Out of Scope for v1 (v2+ Candidates)

- Lock cluster (DoorLock).
- Window Covering cluster.
- Smoke / CO Alarm cluster.
- Air Quality clusters.
- ElectricalMeasurement / EnergyReporting clusters.
- Fabric backup/restore UX.
- Multi-instance plugin support.
- Web dashboard for diagnostics (current plan: Domio device-detail view consumes the diagnostics endpoint).
- OTA firmware updates to devices via the OTA Provider cluster.
- Matter device groups (Group cluster).
