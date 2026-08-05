# BRIDGE_PROTOCOL.md — plugin ⇄ bridge-node local protocol

**Version:** 1 (`protocolVersion: 1`)
**Transport:** WebSocket, JSON text frames, loopback only, default port **5581**
(pref-configurable; the controller's WS is 5580)
**Peers:** the Indigo plugin (client) and the `indigo-matter-bridge` node
(server). We author both ends and ship them together.
**Governing docs:** [`PRD-indigo-matter-export.md`](./PRD-indigo-matter-export.md) §4.4,
ADR-0006 (workspace-level).

Section references: a bare `§N` refers to this document; PRD sections are
always written `PRD §N`.

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
**Every request gets exactly one response**, including ones the plugin chooses
not to await (§3.4). Frames on one connection are processed strictly in
receipt order, so a `set_state` sent after an `upsert_endpoint` on the same
socket is applied after it.

Unknown *fields* are ignored by both peers (forward compatibility inside a
protocol version). Unknown *commands* get `error_code: "unknown_command"`.
Unknown *events* are logged and dropped by the plugin.

### 1.1 Error codes

The complete `error_code` domain for protocol version 1. Anything else is a
bug in the node.

| `error_code` | Meaning |
|---|---|
| `unknown_command` | Command name not in §3 |
| `malformed_args` | Args missing/mistyped for the command |
| `version_mismatch` | `attach` carried a different `protocolVersion`; node refuses and closes the socket after responding |
| `not_attached` | Any command other than `attach` before a successful `attach` on this connection |
| `unknown_device` | `indigoDeviceId` has no live endpoint |
| `unknown_role` | `role` not in the §4.2 enum |
| `role_change` | `upsert_endpoint` tried to change an existing endpoint's role (§4.1) |
| `mass_removal_refused` | `attach` would remove every live endpoint without `"intent": "replace_all"` (§3.1) |
| `endpoint_map_invalid` | Node is in the refuse-to-start state (PRD §7); only `get_status`, `get_pairing` and `rebuild_endpoint_map` are accepted |
| `commissioning_window_failed` | Matter stack refused to open the enhanced commissioning window |
| `internal` | Unexpected node-side failure; `details` carries the message |

## 2. Handshake

On every new connection, before anything else:

1. Node sends a bare frame (not an event, mirroring `server_info`):
   `{"protocolVersion": 1, "bridgeVersion": "<npm semver>", "matterJsVersion": "<semver>"}`
2. Plugin sends `attach` (§3.1). If `protocolVersion` differs from the
   plugin's own, the plugin does **not** attach: it surfaces an error telling
   the user to restart/update the bridge agent (typically the plugin was
   updated while launchd kept the old node alive). Node-side, a mismatched
   `attach` gets `error_code: "version_mismatch"` and the socket is closed —
   skew fails closed from both directions.

The node accepts exactly one attached client at a time; a new successful
`attach` supersedes the incumbent (the old socket is closed). This exists to
recover from half-open sockets: if the plugin's TCP connection dies silently
(plugin crash, reload), the node would otherwise hold a dead "attached" client
forever and refuse the reconnecting plugin. A connection that completes the
handshake but has not attached within **10 seconds** is closed by the node.

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

Result: `<StatusReport>` (§4.3). The node reconciles its live endpoint set
against `endpoints` — creating, updating and removing as needed — so a fresh
connection is always a full reconcile (PRD §5.4 `startup`).

**Mass-removal guard.** If `endpoints` is empty (or would remove every live
endpoint) while the node currently serves a non-empty set, the node refuses
with `error_code: "mass_removal_refused"` unless the args carry
`"intent": "replace_all"`. Removing every exported accessory from every paired
ecosystem must be deliberate (the §5.1 allow-list being emptied), never the
side effect of a stale or buggy client attaching with a default state.

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
number. Idempotent — removing an absent endpoint succeeds with
`{"removed": false}`; a live removal returns `{"removed": true}`. Bulk
removals are paced ~100ms apart by the node.

### 3.4 `set_state`

```json
{"command": "set_state", "args": {"indigoDeviceId": 123456789, "states": {"onOff": true}}}
```

Pushes Indigo-originated state outward. `states` keys are role-specific (§4.2).
The node applies them as **local** (offline-context) writes so they are not
echoed back as `command` events. Result: `{}` on success, or a normal error
response (`unknown_device`, `malformed_args`).

The plugin sends this **without awaiting the result** — it must never block
Indigo's device thread. The response still arrives; the client's frame loop
MUST log any error response it cannot match to a waiting future (rather than
dropping it silently), because an unnoticed `set_state` failure looks exactly
like "the ecosystem shows stale state". This is a required behaviour change
from `matter_client.py`, which drops unmatched responses without a log line.

### 3.5 `set_reachable`

```json
{"command": "set_reachable", "args": {"indigoDeviceId": 123456789, "reachable": false}}
```

Split from `set_state` because it maps to Bridged Device Basic Information,
not the functional cluster, and is driven by device enable/disable rather than
state change. `reachable` is also present on `EndpointSpec`; both paths write
the same attribute and last-write-wins — there is no precedence rule.
Result: `{}`.

### 3.6 `get_status`

Result: `<StatusReport>` (§4.3) — the same shape `attach` returns. Used by the
watchdog tick and the PRD §5.5 config readout.

### 3.7 `get_pairing`

Reports pairing state. A Matter commissioning passcode is **not durable**:
once the first fabric commissions, the basic window closes and the original
code stops working; each additional admin needs an *enhanced* commissioning
window with a freshly derived code (§3.8).

Result:
```json
{"commissioned": true,
 "windowOpen": false,
 "windowExpiresAt": null,
 "manualPairingCode": null,
 "qrPairingCode": null,
 "fabrics": [ <FabricInfo>, … ]}
```

`manualPairingCode`/`qrPairingCode` are non-null only while a window is open
(always true for the never-commissioned initial state, whose codes are the
persisted originals).

### 3.8 `open_commissioning_window`

```json
{"command": "open_commissioning_window", "args": {"durationSeconds": 900}}
```

Opens an enhanced commissioning window so another ecosystem can be added.
Result: `{"manualPairingCode": "...", "qrPairingCode": "MT:...",
"windowExpiresAt": "<ISO 8601 UTC>"}`. Fails with
`commissioning_window_failed` if the stack refuses (e.g. a window is already
open in a conflicting state). Emits `window_closed` (§5) when it expires or a
commissioner completes.

### 3.9 `remove_fabric`

```json
{"command": "remove_fabric", "args": {"fabricIndex": 2}}
```

Result: `{}`. Emits `fabrics_changed` (§5).

**Removing the last fabric is a factory reset the node did not ask for.**
matter.js watches its own `commissioned` state and, when the fabric set
empties, factory-resets itself (`doFactoryReset()` → `erase()`). The node
therefore clears its commissioning witness whenever the fabric count reaches
zero — by this command, by §3.10, or by an ecosystem removing *us* from its
side — and emits `decommissioned`. Without that, the next start would see
"witness says paired, stack says no fabrics", which is the `fabricStorageLost`
refusal signature, and refuse to serve anything on the grounds of a storage
loss that was actually a deliberate unpairing.

### 3.10 `factory_reset`

Wipes commissioning credentials and starts advertising fresh (PRD §6 "reset
all pairings"). The endpoint-number map is **preserved by default**: it is
persisted **outside** the matter.js storage context that the reset wipes, in
the node's own `endpoint-map.json` in the bridge storage dir.

```json
{"command": "factory_reset", "args": {"preserveEndpointNumbers": true}}
```

**What `preserveEndpointNumbers: true` preserves is the ability to NOTICE, not
the numbers.** matter.js owns the allocation, keyed on `Endpoint.id` in the
storage context `erase()` wipes — so the numbers themselves are gone either
way, and the endpoints will be re-created at whatever matter.js hands out next.
What survives is the *baseline*: the map still says what each `UniqueID` used
to be, so the renumbering is reported as drift (§4.3) instead of happening
silently. Passing `false` discards the baseline too — the "explicit rebuild" of
PRD §7, for the case where the map itself is what is corrupt. The node re-runs
the drift check at the end of a preserving reset and says what it found, so the
consequence is in the log rather than waiting to surprise the next attach.

The commissioning witness is cleared either way, and the node **verifies** it
by reading `identity.json` back before reporting completion: a witness that
survived the erase is the exact `fabricStorageLost` signature, so failing to
clear it would make the next start refuse and blame lost storage for the reset
the user asked for. A failure to clear it appears in `StatusReport.warnings`.

Emits `decommissioned` and `fabrics_changed` (§5) when it takes the fabric
count to zero.

### 3.11 `rebuild_endpoint_map`

The recovery path out of the `endpoint_map_invalid` refuse-to-start state
(PRD §7 "Endpoint map lost/corrupt"). Discards the persisted baseline and
**adopts the numbers the live endpoints currently have** as the new one. It
reallocates nothing — by the time a user is asked to confirm it, whatever
duplication a lost map implies has already happened in the ecosystems; what
changes is that the node stops refusing and starts telling the truth about the
numbers that now exist. This **will** duplicate accessories in paired
ecosystems, which is exactly why it is a separate, explicit command that the
plugin only issues after the user confirms via a warning dialog
("Rebuild Matter Endpoint Map…"). Result: `<StatusReport>`.

Two refusals of its own:

- if the new map **cannot be written**, the command fails with `internal` and
  the refuse-to-start state stays in force. Answering with a `StatusReport` over
  a map that never reached disk would tell the user the rebuild they confirmed
  had worked, and then refuse again on the next start for the same reason;
- if the refusal is an unusable `identity.json` rather than an unusable map, it
  fails with `endpoint_map_invalid` and does nothing. That is a different loss
  with a different remedy (restore the file — the unusable one is moved aside as
  `identity.json.unreadable-<stamp>`), and clearing the refusal for it would
  serve endpoints under a `SerialNumber` no ecosystem has ever seen.

A present-but-unusable map is copied to `endpoint-map.json.corrupt` before it is
replaced.

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
- `reachable` — Bridged Device Basic Information `Reachable`. **Omitting it
  means `true`.** An absent flag says nothing about availability, and the other
  reading — an accessory that greys itself out in every ecosystem because the
  plugin left a field off — is the worse default by far.
- `options` — role-specific extras (e.g. window-covering polarity).

### 4.2 Roles: state keys and commands (v1)

Each role defines two vocabularies: the **state keys** the plugin pushes via
`set_state`, and the **commands** the node emits as `command` events when an
ecosystem acts. Both are enumerated here in full; there is no other source.

| `role` | Matter device type | `set_state` keys | `command` names (args) |
|---|---|---|---|
| `onOffPlugInUnit` | On/Off Plug-in Unit | `onOff: bool` | `onOff {"value": bool}` |
| `onOffLight` | On/Off Light | `onOff: bool` | `onOff {"value": bool}` |
| `dimmableLight` | Dimmable Light | `onOff: bool`, `level: 0-100` | `onOff`, `setLevel {"level": 0-100}` |
| `colorTemperatureLight` | Color Temperature Light | + `colorTempMireds: 153-500` | + `setColorTemp {"colorTempMireds": int}` |
| `extendedColorLight` | Extended Color Light | + `hue: 0-360`, `saturation: 0-100` | + `setColor {"hue": 0-360, "saturation": 0-100}` |
| `windowCovering` | Window Covering | `position: 0-100` (100 = open, inbound convention; polarity in `options`) | `goToPosition {"position": 0-100}`, `stopMotion {}` |
| `doorLock` | Door Lock | `locked: bool` | `lock {}`, `unlock {}` |
| `occupancySensor` | Occupancy Sensor | `occupied: bool` | — (sensors emit no commands) |
| `contactSensor` | Contact Sensor | `contact: bool` (true = closed) | — |
| `temperatureSensor` | Temperature Sensor | `temperatureC: float` | — |
| `humiditySensor` | Humidity Sensor | `humidityPct: float` | — |
| `lightSensor` | Light Sensor | `lux: float` | — |
| `pressureSensor` | Pressure Sensor | `pressureKPa: float` | — |
| `flowSensor` | Flow Sensor | `flowM3h: float` | — |
| `thermostat` | Thermostat | `localTemperatureC: float`, `heatingSetpointC: float`, `coolingSetpointC: float`, `systemMode: str` | `setHeatingSetpoint {"valueC": float}`, `setCoolingSetpoint {"valueC": float}`, `setSystemMode {"mode": str}` |

- `systemMode` domain (both directions): `"off" | "heat" | "cool" | "auto"`.
  The node owns the mapping to/from Matter's `SystemModeEnum` integers.
- `level`/`position` are integers 0–100 in both directions; the node owns the
  0–254 Matter LevelControl conversion and its rounding (round-half-up, with
  0 ↔ off preserved exactly).
- Thermostat **fan is not part of v1** (the Fan descope, PRD §5.2); there are
  no fan state keys or commands. v2 candidate.
- Units are Indigo-natural at the protocol boundary (°C, %, lux, 0–100);
  the node owns all Matter wire conversions (0.01°C, mireds bounds-clamping,
  the illuminance log scale). Exactly one converter per role, in the node,
  next to the cluster it feeds.

### 4.3 `StatusReport` and `FabricInfo`

```json
{"commissioned": true,
 "fabrics": [{"fabricIndex": 1, "label": "Apple Home", "vendorId": 4937}],
 "endpointCount": 12,
 "endpoints": [{"indigoDeviceId": 123456789, "endpointNumber": 2, "role": "onOffLight"}],
 "drift": [],
 "driftChecked": false,
 "warnings": []}
```

`drift` lists any `UniqueID → endpointNumber` mappings that changed since last
persist (PRD §4.3 drift detection); non-empty drift is surfaced as a plugin
error, never auto-repaired. Each entry is a `DriftEntry`:

```json
{"uniqueId": "indigo-123456789", "expected": 2, "actual": 5}
```

- `uniqueId` — the node's `UniqueID` for the endpoint, derived from
  `indigoDeviceId` (§4.1). It is the *persisted* identity, which is why drift is
  reported against it rather than against the device id.
- `expected` — the endpoint number the persisted map says this `uniqueId` has.
- `actual` — the endpoint number it currently has.

- `driftChecked` — whether `drift` is an answer or an absence. `drift: []` on
  its own is ambiguous, and the two readings are opposites: "checked, nothing
  moved" versus "there is nothing to check against yet". The node sends `false`
  until it persists the endpoint-number map, so a client must not treat an empty
  `drift` as an all-clear unless `driftChecked` is `true`. It is also `false`
  while the map is held **in memory only** because a write failed — a baseline
  the next restart cannot find has not verified anything durable.
- `warnings` — persistence failures the node has hit and cannot fix on its own:
  the endpoint map could not be written or deleted, the commissioning witness
  could not be cleared, `identity.json` could not be saved. Empty is the normal
  state, and entries are **current, not historical** — a warning disappears the
  moment the operation it describes succeeds.

  This exists because the node's only other channel is stdout, and while the
  node is started by hand that is a terminal nobody is watching. `get_status` is
  polled by the plugin's watchdog, so a client SHOULD surface a non-empty
  `warnings` at warning level — once per streak, since these persist until a
  human acts — and show it wherever it summarises export health.

`endpoints[].role` is one of the §4.2 enum; `endpointCount` is
`endpoints.length` (it is sent explicitly so a client can log the size without
walking the list).

`drift` is populated by every operation that can change the live endpoint set —
`attach`/`reconcile`, `upsert_endpoint` and `remove_endpoint` — and **on the
first such operation**, not "at startup": before one has run there are no live
endpoints to compare, which is precisely what `driftChecked: false` says. A
node that starts commissioned with no `endpoint-map.json` at all (every install
that predates the map) **bootstraps** a baseline from matter.js's own persisted
allocation and serves normally; it does not refuse. Refusing would take working
exports offline on upgrade, and would buy nothing — matter.js owns the numbers,
the map only witnesses them, and a missing witness renumbers nothing. A map that
is present and unreadable is a different matter and still refuses (§1.1).

## 5. Events (node → plugin)

| Event | `data` | Meaning |
|---|---|---|
| `command` | `{"indigoDeviceId", "command", "args"}` | Ecosystem-originated action; names and args exactly as enumerated per role in §4.2 |
| `fabrics_changed` | `{"fabrics": […], "change": "added"\|"deleted"\|"updated"}` | Pairing/unpairing activity, including the changes §3.9/§3.10 cause themselves. `fabrics` is the set *after* the change |
| `commissioned` / `decommissioned` | `{}` | The fabric count went zero → non-zero / non-zero → zero. Transitions, not repeats: a second ecosystem pairing raises `fabrics_changed` only |
| `window_closed` | `{"reason": "expired"\|"commissioned"}` | The enhanced commissioning window ended |
| `drift_detected` | `{"drift": […]}` | Endpoint-number drift found by a reconcile, upsert or remove |

The plugin resolves `indigoDeviceId` through the allow-list before acting; a
command for a device no longer exported gets no action and a warning (PRD §7
race row — the node responds to the ecosystem with failure when the endpoint
is already gone).

## 6. Invariants

1. **Loopback only.** The node binds `127.0.0.1`; there is no auth on the
   socket because there is no remote surface. The mass-removal guard (§3.1)
   exists because "local process" still includes stale plugin instances.
2. **The plugin is the source of truth for the export set**; the node is the
   source of truth for commissioning state and endpoint numbers. Neither peer
   caches the other's domain across reconnects — `attach` reconciles.
3. **Identity flows one way:** `indigoDeviceId` → `Endpoint.id` → persisted
   endpoint number. Nothing is ever keyed on list position or label.
4. **State pushes are echo-guarded** in the node (`ctx.offline`); the plugin
   never receives a `command` event for a change it pushed.
5. **Version skew fails closed:** mismatched `protocolVersion` means no
   attach (both peers enforce it), an error in the Indigo log, and untouched
   pairings.
6. **Destructive operations are explicit:** emptying the endpoint set needs
   `intent: "replace_all"`; discarding endpoint identity needs
   `preserveEndpointNumbers: false` or `rebuild_endpoint_map`. Neither can
   happen as a default.

## 7. Testing contract

Golden frames for every command/response/event pair live as **JSON fixture
files** under `tests/fixtures/bridge_protocol/`, consumed by both the Python
suite and the TypeScript node's protocol tests — a deliberate departure from
`test_golden_real.py`'s inline-dict pattern, which a TS suite cannot import.
A frame change that only updates one side fails that side's suite.
