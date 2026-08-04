# BRIDGE_PROTOCOL.md — plugin ⇄ bridge-node local protocol

**Version:** 1 (`protocolVersion: 1`)
**Transport:** WebSocket, JSON text frames, loopback only
**Peers:** the Indigo plugin (client) and the `indigo-matter-bridge` node
(server). We author both ends and ship them together.
**Governing docs:** [`PRD-indigo-matter-export.md`](./PRD-indigo-matter-export.md) §4.4,
ADR-0006.

This is the outbound twin of the controller protocol that `protocol.py` /
`matter_client.py` speak. It reuses the same envelope grammar so the client
machinery and test doubles generalise, but it is **not** matter-server's
protocol and has no rename firewall: a field rename here is a coordinated edit
to both peers in one release. What protects us instead is the **version
handshake** — launchd deliberately keeps an old bridge node running across
plugin reloads (PM-B), so plugin/node version skew is the failure mode that
will actually occur.

## 1. Envelope grammar

Identical shapes to the controller protocol:

| Kind | Direction | Shape |
|---|---|---|
| Request | plugin → node | `{"message_id": "<str>", "command": "<str>", "args": {…}}` |
| Success response | node → plugin | `{"message_id": "<str>", "result": …}` |
| Error response | node → plugin | `{"message_id": "<str>", "error_code": "<str>", "details": "<str>"}` |
| Event | node → plugin | `{"event": "<str>", "data": {…}}` (no `message_id`) |

`message_id` is an opaque string chosen by the plugin, echoed verbatim.
Unknown *fields* are ignored by both peers (forward compatibility inside a
protocol version). Unknown *commands* get `error_code: "unknown_command"`.
Unknown *events* are logged and dropped by the plugin.

## 2. Handshake

On every new connection, before anything else:

1. Node sends a bare frame (not an event, mirroring `server_info`):
   `{"protocolVersion": 1, "bridgeVersion": "<npm semver>", "matterJsVersion": "<semver>"}`
2. Plugin sends `attach` (§3.1). If `protocolVersion` differs from the
   plugin's own, the plugin does **not** attach: it surfaces an error telling
   the user to restart/update the bridge agent (typically the plugin was
   updated while launchd kept the old node alive). The node accepts exactly
   one attached client at a time; a second `attach` supersedes the first
   (the old socket is closed) — the plugin's reconnect loop relies on this.

## 3. Commands (plugin → node)

### 3.1 `attach`

Declares the client and delivers the desired endpoint set in one shot.

```json
{"command": "attach", "args": {
  "protocolVersion": 1,
  "pluginVersion": "2026.8.1",
  "endpoints": [ <EndpointSpec>, … ]
}}
```

Result: `{"status": <StatusReport>}` (§5.1). The node reconciles its live
endpoint set against `endpoints` — creating, updating and removing as needed —
so a fresh connection is always a full reconcile (PRD §5.4 `startup`).

### 3.2 `upsert_endpoint`

```json
{"command": "upsert_endpoint", "args": {"endpoint": <EndpointSpec>}}
```

Creates the endpoint if absent, updates label/reachable/state if present.
Idempotent. Result: `{"endpointNumber": <int>}`.

### 3.3 `remove_endpoint`

```json
{"command": "remove_endpoint", "args": {"indigoDeviceId": 123456789}}
```

Removes the child endpoint (`endpoint.close()`); the persisted endpoint-number
allocation is **retained** so re-adding the same device restores the same
number. Idempotent — removing an absent endpoint succeeds. Bulk removals are
paced ~100ms apart by the node.

### 3.4 `set_state`

```json
{"command": "set_state", "args": {"indigoDeviceId": 123456789, "states": {"onOff": true}}}
```

Pushes Indigo-originated state outward. `states` keys are role-specific (§4.2).
The node applies them as **local** (offline-context) writes so they are not
echoed back as commands. The plugin sends this fire-and-forget (it must never
block Indigo's device thread on the result).

### 3.5 `set_reachable`

```json
{"command": "set_reachable", "args": {"indigoDeviceId": 123456789, "reachable": false}}
```

Split from `set_state` because it maps to Bridged Device Basic Information,
not the functional cluster, and is driven by device enable/disable rather than
state change.

### 3.6 `get_status`

Result: `<StatusReport>` (§5.1). Used by the watchdog tick and the §5.5
config readout.

### 3.7 `get_pairing`

Result:
```json
{"commissioned": false,
 "manualPairingCode": "34970112332",
 "qrPairingCode": "MT:Y.K90IRV01KA0648G00",
 "fabrics": [ <FabricInfo>, … ]}
```

### 3.8 `remove_fabric`

```json
{"command": "remove_fabric", "args": {"fabricIndex": 2}}
```

### 3.9 `factory_reset`

Wipes commissioning credentials and starts advertising fresh (PRD §6 "reset
all pairings"). The endpoint-number map is **preserved** — a reset must not
scramble identities if the user re-pairs the same ecosystems.

## 4. Shapes

### 4.1 `EndpointSpec`

```json
{"indigoDeviceId": 123456789,
 "role": "onOffLight",
 "label": "Kitchen Lamp",
 "reachable": true,
 "states": {"onOff": true},
 "options": {}}
```

- `indigoDeviceId` — the immutable Indigo device ID. The node derives
  `Endpoint.id` and `UniqueID` from it (PRD §4.3); it is the identity key
  everywhere in this protocol.
- `role` — one of the v1 role enum (§4.2). A role change for an existing
  endpoint is **rejected** (`error_code: "role_change"`); the plugin must
  remove and re-add, because ecosystems cache device types per endpoint.
- `label` — Bridged Device Basic Information `NodeLabel`.
- `options` — role-specific extras (e.g. window-covering polarity).

### 4.2 Roles and their state keys (v1)

| `role` | Matter device type | `states` keys |
|---|---|---|
| `onOffPlugInUnit` | On/Off Plug-in Unit | `onOff: bool` |
| `onOffLight` | On/Off Light | `onOff: bool` |
| `dimmableLight` | Dimmable Light | `onOff: bool`, `level: 0-100` |
| `colorTemperatureLight` | Color Temperature Light | + `colorTempMireds: int` |
| `extendedColorLight` | Extended Color Light | + `hue: 0-360`, `saturation: 0-100` |
| `windowCovering` | Window Covering | `position: 0-100` (100 = open, inbound convention; polarity in `options`) |
| `doorLock` | Door Lock | `locked: bool` |
| `occupancySensor` | Occupancy Sensor | `occupied: bool` |
| `contactSensor` | Contact Sensor | `contact: bool` (true = closed) |
| `temperatureSensor` | Temperature Sensor | `temperatureC: float` |
| `humiditySensor` | Humidity Sensor | `humidityPct: float` |
| `lightSensor` | Light Sensor | `lux: float` |
| `pressureSensor` | Pressure Sensor | `pressureKPa: float` |
| `flowSensor` | Flow Sensor | `flowM3h: float` |
| `thermostat` | Thermostat | `localTemperatureC`, `heatingSetpointC`, `coolingSetpointC`, `systemMode` |

Units are Indigo-natural at the protocol boundary (°C, %, lux, 0–100 levels);
the node owns the conversion to Matter wire units (0.01°C, mireds, the
illuminance log scale, 0–254 levels). Exactly one converter per role, in the
node, next to the cluster it feeds.

### 4.3 `StatusReport` (§5.1) and `FabricInfo`

```json
{"commissioned": true,
 "fabrics": [{"fabricIndex": 1, "label": "Apple Home", "vendorId": 4937}],
 "endpointCount": 12,
 "endpoints": [{"indigoDeviceId": 123456789, "endpointNumber": 2, "role": "onOffLight"}],
 "drift": []}
```

`drift` lists any `UniqueID → endpointNumber` mappings that changed since last
persist (PRD §4.3 drift detection); non-empty drift is surfaced as a plugin
error, never auto-repaired.

## 5. Events (node → plugin)

| Event | `data` | Meaning |
|---|---|---|
| `command` | `{"indigoDeviceId", "command", "args"}` | Ecosystem-originated action, e.g. `{"command": "onOff", "args": {"value": true}}`, `{"command": "moveToLevel", "args": {"level": 40}}`, `{"command": "lock"}` |
| `fabrics_changed` | `{"fabrics": […], "change": "added"\|"deleted"\|"updated"}` | Pairing/unpairing activity |
| `commissioned` / `decommissioned` | `{}` | First fabric added / last removed |
| `drift_detected` | `{"drift": […]}` | Endpoint-number drift found at startup |

`command` events carry the same role-relative vocabulary as `set_state` keys.
The plugin resolves `indigoDeviceId` through the allow-list before acting; a
command for a device no longer exported gets no action and a warning (PRD §7
race row — the node responds to the ecosystem with failure when the endpoint
is already gone).

## 6. Invariants

1. **Loopback only.** The node binds `127.0.0.1`; there is no auth on the
   socket because there is no remote surface.
2. **The plugin is the source of truth for the export set**; the node is the
   source of truth for commissioning state and endpoint numbers. Neither peer
   caches the other's domain across reconnects — `attach` reconciles.
3. **Identity flows one way:** `indigoDeviceId` → `Endpoint.id` → persisted
   endpoint number. Nothing is ever keyed on list position or label.
4. **State pushes are echo-guarded** in the node (`ctx.offline`); the plugin
   never receives a `command` event for a change it pushed.
5. **Version skew fails closed:** mismatched `protocolVersion` means no
   attach, an error in the Indigo log, and untouched pairings.

## 7. Testing contract

Golden frames for every command/response/event pair live beside the Python
tests (the `test_golden_real.py` pattern) and are the cross-language contract:
the TypeScript node's protocol tests consume the same fixtures, so a frame
change that only updates one side fails that side's suite.
