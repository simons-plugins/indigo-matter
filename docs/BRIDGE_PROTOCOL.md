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

`endpoint_map_invalid` covers **every** refuse-to-start reason, not only a bad
map: the code names the *state* (which commands are accepted), and `details`
opens with the *reason*, which is what selects the remedy. The reasons are a
closed set (`RefuseReason` in `bridge-node/src/protocol.ts`, mirrored as
constants in `bridge_protocol.py`), and two of them need opposite advice:

| `details` prefix | Remedy |
|---|---|
| `endpoint map is unreadable` | §3.11 `rebuild_endpoint_map`, user-confirmed — it adopts the live numbers and renumbers nothing |
| `this bridge was commissioned but its Matter fabric storage is gone…` | Restore a backup, or accept the loss via §3.11 |
| `the bridge identity file is present but unreadable` | **§3.11 will not help and the node refuses it.** Restore or repair `identity.json.unreadable-<stamp>` and restart the bridge |

A client that shows the map remedy for all three sends the identity case at the
one door deliberately locked against it. Clients MUST branch on the reason.

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

**The live set is not empty at first attach.** Since bridge-node 0.6.0 the node
rebuilds its last-known endpoint set from `endpoint-map.json` *before* it goes
online (§4.3), so the first `attach` of a session reconciles against restored
endpoints rather than against nothing. Nothing about the command changes — the
plugin remains the source of truth (§6.2), and an endpoint that is restored but
absent from `endpoints` is removed, which is exactly right for a device
un-exported while the plugin was away. Two consequences worth stating:

- restored endpoints are **unreachable** until the attach (or a `set_reachable`,
  §3.5) says otherwise, and carry no state the plugin has not sent;
- the mass-removal guard below is armed from the very first attach.

**Mass-removal guard.** If `endpoints` is empty (or would remove every live
endpoint) while the node currently serves a non-empty set, the node refuses
with `error_code: "mass_removal_refused"` unless the args carry
`"intent": "replace_all"`. Removing every exported accessory from every paired
ecosystem must be deliberate (the §5.1 allow-list being emptied), never the
side effect of a stale or buggy client attaching with a default state.

Because restore arms that guard from the first attach, the plugin now carries
the intent in **two** cases, not one: the un-export debt it already tracked
(XAC7), and an allow-list that is non-empty but whose every entry the classifier
skipped — a deleted Indigo device, a device that stopped being exportable, a
role this build cannot bridge, a `states_for` that raised. That second case sends
`[]` and is genuinely asking for an empty set; without the intent it would meet
an armed guard on an ordinary restart and be refused, and `mass_removal_refused`
halts the client permanently. It is announced in the Indigo log with every
skipped device named. A *genuinely* empty allow-list still carries no intent —
the guard must keep its teeth against exactly the stale client it was written
for.

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
like "the ecosystem shows stale state". Both `matter_client.py` and this
bridge client get this behaviour from the SAME shared transport core
(`ws_json_client._log_unmatched`): it warns on any unmatched error response
and debug-logs unmatched results, for both peers.

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

Result: `{"removed": true, "remaining": 1}`. Emits `fabrics_changed` (§5).

**`removed` is the point of the result.** An index with no fabric behind it
answers `{"removed": false, "remaining": N}` rather than failing — the caller's
index comes from a `get_pairing` readout that is by definition a moment old, and
the way it goes stale is the ecosystem unpairing *itself*, so "it is already
gone" is the request being granted. It is not the same thing as removing a live
fabric, though, and a caller that reports this to a user in the past tense must
be able to tell them apart. `remaining` is the fabric count after the call, or
`null` when server state could not be read (legitimate mid-reset).

`fabrics_changed` is emitted **either way** — with `change: "unchanged"` on the
already-gone path. Nothing changed on the node, but a caller that asked to
remove a fabric that is not there is demonstrably holding a stale list, and this
is the only moment the node knows that.

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

**What `preserveEndpointNumbers: true` preserves is the entries, not their
trustworthiness.** matter.js owns the allocation, keyed on `Endpoint.id` in the
storage context `erase()` wipes — so the numbers themselves are gone either
way, and the endpoints will be re-created at whatever matter.js hands out next.
What survives is the *map*: role/label and every `UniqueID` stay, but the node
also **voids** every entry's number (issue #140, since bridge-node 0.8.0)
rather than leaving the old one standing. A voided number is silently
**adopted** — not reported as drift —
the next time `check` (§4.3) sees that `UniqueID` live, because both the reset
and the last-fabric self-reset are reached with an empty fabric set: matter.js
just erased its own allocation along with every fabric, so there is no paired
ecosystem left that could be holding the old numbers to disagree with, and the
adoption is unobservable outside this node. This is a narrow, deliberate carve-out
of §4.3's "never auto-repaired" rule (below) for the one case where the
renumbering is the reset's own, not a genuine anomaly — an entry that was never
voided still drifts exactly as before. Passing `false` discards the baseline
too — the "explicit rebuild" of PRD §7, for the case where the map itself is
what is corrupt.

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
duplication lost Matter storage implies has already happened in the
ecosystems; what changes is that the node stops refusing and starts telling
the truth about the numbers that now exist. It cannot itself duplicate
accessories — but it discards the only surviving record of what the numbers
used to be (quarantined aside, not deleted), which is why it is a separate,
explicit command that the plugin only issues after the user confirms via a
warning dialog
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

A present-but-unusable map is copied to `endpoint-map.json.corrupt-<stamp>`
before it is replaced — by the plain `persist()` path as well as by the rebuild,
since a corrupt map on a bridge that has never been commissioned is served past
rather than refused (§7) and would otherwise be overwritten with no copy at all.
The name is timestamped for the same reason `identity.json.unreadable-<stamp>`
is: a fixed name means the second quarantine silently destroys the first, and
the first is the one nearest the original truth.

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
- `onOff` is emitted per ecosystem *invocation* (on/off/toggle/timed-on), not
  per attribute change — so a command that matches the accessory's current
  state is still forwarded, the same way `lock`/`unlock` already were.

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
error, never auto-repaired. **One narrow, deliberate carve-out (issue #140,
since bridge-node 0.8.0):** a renumbering caused by §3.10's preserving reset
(or the last-fabric self-reset) is voided at the reset and then silently
**adopted**, not reported here — see §3.10 for the safety argument. Voiding
also drops `driftChecked` back to `false`: a baseline the node just declared
void has not been verified against anything, and the flag comes back once the
first post-reset reconcile checks it against live endpoints. Everything else
about this rule is unchanged: an entry the reset did not touch still drifts
and is still never repaired. Each entry is a `DriftEntry`:

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
  node is started by hand that is a terminal nobody is watching. A client MUST
  therefore **poll** `get_status` while attached — this plugin does it once per
  ~15s watchdog tick — and surface a non-empty `warnings` at warning level, once
  per streak, since these persist until a human acts.

  Polling is not optional decoration: most of these faults happen *after* the
  attach and so appear in no attach response. The commissioning witness is
  written when a fabric first appears, the witness is cleared by a
  `factory_reset`, and the endpoint map is written by the drift check inside
  `upsert_endpoint`/`remove_endpoint`. A client that only reads the `warnings`
  it gets back from `attach` sees none of them, and the user is told nothing at
  all — which is the state this channel was invented to end.

`endpoints[].role` is one of the §4.2 enum; `endpointCount` is
`endpoints.length` (it is sent explicitly so a client can log the size without
walking the list).

#### `endpoint-map.json` — contents, and restore-on-start

The map is a sibling of `identity.json` in the bridge storage directory (outside
matter.js's own storage context, so §3.10's reset cannot wipe it). **Schema
version 2** since bridge-node 0.6.0:

```json
{"version": 2,
 "endpoints": {
   "indigo-123456789": {"number": 2, "role": "onOffLight", "label": "Kitchen Lamp"}
 }}
```

- `number` — the endpoint number matter.js assigned. The witness half; this is
  what drift is reported against, and it is never rewritten by a drift report.
- `role`, `label` — the *restoration* half, added by version 2. Together with
  the `UniqueID` key they are everything `createEndpoint` needs, which is what
  lets the node rebuild an accessory without the plugin. `options` is **not**
  stored: nothing in the node reads it (window-covering polarity is applied
  plugin-side, §4.1).
- `numberVoid` — issue #140, since bridge-node 0.8.0, present (`true`) and
  absent otherwise, never `false`. Set on every entry by §3.10's preserving
  reset and by the last-fabric self-reset; cleared the next time `number` is
  silently adopted
  from a live endpoint rather than compared against it. Still schema version 2
  — an old build reads a file that has it exactly as it would without it,
  because every reader here reaches a field by name rather than validating the
  object as a whole, so the extra key is inert rather than a parse failure.
- `orphaned` — issue #219, present (`true`) and absent otherwise, never
  `false`. Not yet in a published bridge-node release as of 0.8.0 — the
  release commit that ships it should update this clause. Set by an
  un-export (see "Un-exporting" below); cleared the moment the same
  `UniqueID` is live again. Parses with the same inert-extra-key tolerance as
  `numberVoid` — an old build reaches fields by name, so the unknown key is
  simply never looked at — but that is true of PARSING only, not behaviour:
  an older bridge (a plugin rollback reinstalling the pinned, published
  bridge over the same `endpoint-map.json`) drops the unknown key on its next
  write, and its `restorable()` filters only on `role`/`label`, so it
  rebuilds every un-exported device before `server.start()`. That is the
  #141 appear-then-vanish ghost churn, once, self-healing after one attach —
  not the equivalence the schema-version note above claims for `numberVoid`.

**Version 1 files are read, migrated in place, and never treated as corrupt.**
A v1 entry is a bare number; it keeps that number, is simply not restorable
until the next `attach` records a role and label, and the file is rewritten as
v2 by the first drift check. A v2 entry whose `role`/`label` are malformed keeps
its number the same way — the number is the part that cannot be re-derived.

**Restore-on-start.** At startup the node rebuilds every restorable entry —
through the ordinary `createEndpoint` path, at the same `Endpoint.id` and
therefore the same persisted number, with `reachable: false` and the role's own
default state — **before** `server.start()`. A bridge that came online with a
childless aggregator and waited for the plugin told every paired ecosystem that
every accessory had gone; Apple then re-added them as new accessories in the
bridge's own room, destroying the user's room assignments on every restart
(issue #141). The restore is not drift: these are the map's own numbers being
re-used, and `driftChecked` stays `false` until a real comparison runs.

A node in a refuse-to-start state (§1.1 `endpoint_map_invalid`) ends up serving
**nothing** — it cannot trust its own records, and creating endpoints is
precisely what the refusal exists to prevent — but *how* it gets there differs
by reason, and one of the three is observable on the wire:

| Refusal reason | When it is known | What a controller can see |
|---|---|---|
| `identityUnreadable` | before the stack starts | nothing is ever built; `PartsList` is empty from the first read |
| `mapUnreadable` | before the stack starts | as above — an unreadable map restores nothing in the first place |
| `fabricStorageLost` | only **after** `server.start()`, because it needs the fabric table | the restored set exists briefly and is then **withdrawn** |

`fabricStorageLost` is therefore not silent. The node goes online carrying its
restored endpoints, reads the fabric table, refuses, and takes them back out —
so a reader would see a transient non-zero `endpointCount`, a
`ConfigurationVersion` bump from the withdrawal, and the extra startup latency
of building and closing the set. That is accepted rather than avoided: by
definition this node has **no fabrics**, so there is no commissioned controller
subscribed to see any of it, and the alternative (deferring the restore until
after the refusal decision) reopens the online-and-empty window for every
healthy start, which is the bug itself.

**Un-exporting marks an entry orphaned, and (since issue #219) keeps its
restoration half.** When a reconcile or a `remove_endpoint` actually takes an
endpoint away, its entry is marked `orphaned` (see above) while `role`,
`label` **and** `number` are all kept. Before #219, `role`/`label` were
dropped instead — which also excluded the entry from restore-on-start, but
threw away the only evidence a future re-adopt UI could match a *recreated*
device (a factory-reset accessory that comes back with a new `UniqueID`)
against the endpoint it used to occupy. Without either mechanism, `check`'s
add-and-refresh-only behaviour would leave an un-exported device restorable
for ever: it would be rebuilt before every `server.start()` and removed again
by the next attach — the same appear-then-vanish churn, aimed at devices the
user had already removed — and each ghost would also cost a removal-pacing
slot on every attach. Keeping the number is §3.3's retained allocation, so a
re-export returns the *same* accessory rather than a new one; keeping
`role`/`label` costs nothing extra, since `orphaned` alone already keeps the
entry out of restore-on-start. `orphaned` is cleared the moment the same
`UniqueID` is live again, so a plain re-export is unaffected. Only a measured
removal marks an entry: absence from the live set proves nothing (a node that
never attached has an empty one), so a factory reset, a seed and an entry this
build cannot rebuild all leave the map alone.

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
